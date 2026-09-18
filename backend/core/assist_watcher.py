"""Keep LangHire's Pause/Resume AI control on every tab of the automation browser, and run
autofill on a tab when the candidate presses Resume, even after the run that opened it is over.

Runs finish and leave their tabs open for the candidate. Without this the control disappeared
with the run, so a page the candidate wanted the AI to fill again had no way to ask for it.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from browser_use import BrowserSession

try:
    from core.autofill_facts import (
        _automation_browser_pid,
        _focus_pause_target,
        load_autofill_facts,
        release_review_handoff,
        set_ai_pause_controls_for_tabs,
    )
    from core.config import load_profile
    from core.shared_config import RESUME_PATH
    from core.workday_flow import fill_current_page
except ImportError:
    from backend.core.autofill_facts import (
        _automation_browser_pid,
        _focus_pause_target,
        load_autofill_facts,
        release_review_handoff,
        set_ai_pause_controls_for_tabs,
    )
    from backend.core.config import load_profile
    from backend.core.shared_config import RESUME_PATH
    from backend.core.workday_flow import fill_current_page


def plan_tab_action(state: dict[str, Any] | None) -> str:
    """What to do with one tab given its pause-control snapshot: none, idle or assist."""
    if not state or not state.get("installed"):
        return "none"
    if state.get("paused"):
        return "none"
    # Unpaused and untouched: no run owns this tab any more, so the AI is not really working.
    # Unpaused by the candidate: they pressed Resume AI and want the page filled.
    return "assist" if state.get("userChanged") else "idle"


def automation_browser_endpoint() -> str | None:
    """CDP URL of the automation browser LangHire started, or None if it is not running."""
    pid = _automation_browser_pid()
    if not pid:
        return None
    try:
        import psutil

        for part in psutil.Process(pid).cmdline():
            match = re.fullmatch(r"--remote-debugging-port=(\d+)", part)
            if match and int(match.group(1)) > 0:
                return f"http://127.0.0.1:{match.group(1)}"
    except Exception:
        pass
    return None


async def _connect() -> BrowserSession | None:
    endpoint = automation_browser_endpoint()
    if not endpoint:
        return None
    browser = BrowserSession(cdp_url=endpoint, keep_alive=True)
    await asyncio.wait_for(browser.start(), 20)
    return browser


async def _assist(browser: BrowserSession, target_id: str, url: str) -> None:
    """Fill the tab where Resume AI was pressed. It fills; the candidate keeps control of navigation."""
    await _focus_pause_target(browser, target_id)
    profile = load_profile()
    facts = load_autofill_facts(profile, RESUME_PATH)
    await fill_current_page(browser, facts, RESUME_PATH, profile)
    await release_review_handoff(browser)


class AssistWatcher:
    def __init__(
        self,
        *,
        is_busy: Callable[[], bool],
        connect: Callable[[], Awaitable[Any]] = _connect,
        install_controls: Callable[..., Awaitable[list[dict[str, Any]]]] = set_ai_pause_controls_for_tabs,
        assist: Callable[[Any, str, str], Awaitable[None]] = _assist,
        interval: float = 1.5,
        tick_timeout: float = 60.0,
    ) -> None:
        self.is_busy = is_busy
        self.connect = connect
        self.install_controls = install_controls
        self.assist = assist
        self.interval = interval
        self.tick_timeout = tick_timeout
        self.browser: Any = None
        self.assisting: set[str] = set()

    async def _disconnect(self) -> None:
        browser, self.browser = self.browser, None
        if browser is not None:
            try:
                await asyncio.wait_for(browser.stop(), 5)  # keep_alive: leaves the browser running
            except Exception:
                pass

    async def _run_assist(self, target_id: str, url: str) -> None:
        browser = self.browser
        try:
            await self.assist(browser, target_id, url)
        except Exception as exc:
            print(f"  ⚠️  Resume AI autofill stopped: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            self.assisting.discard(target_id)
            try:
                await self.install_controls(browser, [target_id], True)
            except Exception:
                pass

    async def tick(self) -> None:
        if self.is_busy():
            # A run drives the browser and its own controls; stay out of its way.
            if not self.assisting:
                await self._disconnect()
            return
        if self.browser is None:
            self.browser = await self.connect()
            if self.browser is None:
                return
        try:
            tabs = await self.browser.get_tabs()
            urls = {
                str(tab.target_id): str(tab.url)
                for tab in tabs
                if str(tab.url).startswith(("http://", "https://"))
            }
            results = await self.install_controls(self.browser, list(urls), None)
        except Exception:
            await self._disconnect()
            return
        for state in results:
            target_id = str(state.get("target_id") or "")
            action = plan_tab_action(state)
            if action == "idle":
                await self.install_controls(self.browser, [target_id], True)
            elif action == "assist" and target_id not in self.assisting:
                self.assisting.add(target_id)
                asyncio.create_task(self._run_assist(target_id, urls.get(target_id, "")))

    async def run_forever(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.tick(), self.tick_timeout)
            except asyncio.TimeoutError:
                # A browser call hung (a dropped CDP connection can do that): start over next tick.
                await self._disconnect()
            except asyncio.CancelledError:
                await self._disconnect()
                raise
            except Exception as exc:
                print(f"  ⚠️  Assist watcher error: {type(exc).__name__}: {str(exc)[:160]}")
            await asyncio.sleep(self.interval)
