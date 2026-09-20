"""Keep LangHire's Pause/Resume AI control on every tab of the automation browser, and run
autofill on a tab when the candidate presses Resume, even after the run that opened it is over.

Runs finish and leave their tabs open for the candidate. Without this the control disappeared
with the run, so a page the candidate wanted the AI to fill again had no way to ask for it.

Watching is done with a plain Playwright connection. A browser-use session manages tabs for its
agent (it recreates "missing" ones and reuses blank ones), so it is only opened for the few
minutes an assist takes and never held while the candidate is just browsing.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from browser_use import BrowserSession

try:
    from core.autofill_facts import (
        _automation_browser_pid,
        _focus_pause_target,
        _pause_control_overlay_script,
        load_autofill_facts,
        release_review_handoff,
    )
    from core.config import load_profile
    from core.shared_config import RESUME_PATH
    from core.workday_flow import fill_current_page, is_workday_url, run_workday_deterministic
except ImportError:
    from backend.core.autofill_facts import (
        _automation_browser_pid,
        _focus_pause_target,
        _pause_control_overlay_script,
        load_autofill_facts,
        release_review_handoff,
    )
    from backend.core.config import load_profile
    from backend.core.shared_config import RESUME_PATH
    from backend.core.workday_flow import fill_current_page, is_workday_url, run_workday_deterministic


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


async def _assist(browser: BrowserSession, target_id: str, url: str, cancel_flag: dict) -> None:
    """Resume AI on one tab, after the run that opened it is over.

    A Workday application gets what a run gives it: fill each page, press Save and Continue and
    carry on until Review (the final Submit stays guarded). Any other site only has the page in
    front of the candidate filled, since there is no wizard to walk.
    """
    await _focus_pause_target(browser, target_id)
    profile = load_profile()
    facts = load_autofill_facts(profile, RESUME_PATH)
    if is_workday_url(url):
        # Keep the AI helper on this application; the candidate's other tabs are not its business.
        others = {str(tab.target_id) for tab in await browser.get_tabs() if str(tab.target_id) != target_id}
        await run_workday_deterministic(
            browser, facts=facts, resume_path=RESUME_PATH, worker_id=0,
            passes=12, llm_cleanup=True, llm_steps=60, llm_timeout=300.0,
            profile=profile, ignored_target_ids=others, cancel_flag=cancel_flag,
        )
    else:
        await fill_current_page(browser, facts, RESUME_PATH, profile)
    await release_review_handoff(browser)


class PlaywrightPoller:
    """Reads and sets the control on every http(s) tab over a light Playwright connection."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._endpoint: str | None = None
        self._init_scripted: set[int] = set()
        self._pages: dict[str, Any] = {}

    async def connect(self) -> bool:
        endpoint = automation_browser_endpoint()
        if not endpoint:
            return False
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await asyncio.wait_for(self._pw.chromium.connect_over_cdp(endpoint), 20)
        except Exception:
            await self.close()
            raise
        self._endpoint = endpoint
        self._init_scripted.clear()
        self._pages.clear()
        return True

    async def tabs(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        out: list[tuple[str, str, dict[str, Any] | None]] = []
        self._pages = {}
        for context in self._browser.contexts:
            if id(context) not in self._init_scripted:
                # Re-installs the control after every navigation and on every tab opened later.
                await context.add_init_script(_pause_control_overlay_script())
                self._init_scripted.add(id(context))
            for page in context.pages:
                if not page.url.startswith(("http://", "https://")):
                    continue
                key = str(id(page))
                self._pages[key] = page
                # Idempotent: installs, or re-mounts a removed panel, and reports the state.
                state = await page.evaluate(_pause_control_overlay_script())
                out.append((key, page.url, state if isinstance(state, dict) else None))
        return out

    async def set_paused(self, key: str, paused: bool) -> None:
        page = self._pages.get(key)
        if page is not None and not page.is_closed():
            await page.evaluate(_pause_control_overlay_script(paused))

    async def assist(self, key: str, url: str, cancel_flag: dict) -> None:
        page = self._pages.get(key)
        if page is None or self._endpoint is None:
            return
        cdp = await page.context.new_cdp_session(page)
        try:
            info = await cdp.send("Target.getTargetInfo")
        finally:
            await cdp.detach()
        target_id = str(info["targetInfo"]["targetId"])
        browser = BrowserSession(cdp_url=self._endpoint, keep_alive=True)
        await asyncio.wait_for(browser.start(), 20)
        try:
            await _assist(browser, target_id, url, cancel_flag)
        finally:
            try:
                await asyncio.wait_for(browser.stop(), 5)  # keep_alive: leaves the browser running
            except Exception:
                pass

    async def close(self) -> None:
        browser, pw = self._browser, self._pw
        self._browser = self._pw = None
        self._pages = {}
        try:
            if browser is not None:
                await asyncio.wait_for(browser.close(), 5)  # connected over CDP: only disconnects
        except Exception:
            pass
        try:
            if pw is not None:
                await asyncio.wait_for(pw.stop(), 5)
        except Exception:
            pass


class AssistWatcher:
    def __init__(
        self,
        *,
        is_busy: Callable[[], bool],
        poller: Any = None,
        interval: float = 1.5,
        tick_timeout: float = 60.0,
    ) -> None:
        self.is_busy = is_busy
        self.poller = poller or PlaywrightPoller()
        self.interval = interval
        self.tick_timeout = tick_timeout
        self.connected = False
        self.assisting: set[str] = set()
        self.cancel_flags: dict[str, dict] = {}

    async def _disconnect(self) -> None:
        self.connected = False
        await self.poller.close()

    async def _run_assist(self, key: str, url: str) -> None:
        flag = self.cancel_flags[key] = {"cancel_requested": False}
        try:
            await self.poller.assist(key, url, flag)
        except Exception as exc:
            print(f"  ⚠️  Resume AI autofill stopped: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            self.assisting.discard(key)
            self.cancel_flags.pop(key, None)
            try:
                await self.poller.set_paused(key, True)
            except Exception:
                pass

    async def tick(self) -> None:
        if self.is_busy():
            # A run drives the browser and its own controls; stay out of its way.
            if self.connected and not self.assisting:
                await self._disconnect()
            return
        if not self.connected:
            self.connected = bool(await self.poller.connect())
            if not self.connected:
                return
        try:
            tabs = await self.poller.tabs()
        except Exception:
            await self._disconnect()
            return
        open_keys = {key for key, _url, _state in tabs}
        # A tab that closes mid-fill ends its run: browser-use would otherwise move the AI helper's
        # focus to whichever tab is left, which is another of the candidate's applications.
        for key in list(self.assisting):
            if key not in open_keys and key in self.cancel_flags:
                self.cancel_flags[key]["cancel_requested"] = True
        for key, url, state in tabs:
            action = plan_tab_action(state)
            if action == "idle":
                await self.poller.set_paused(key, True)
            elif action == "assist" and key not in self.assisting:
                self.assisting.add(key)
                asyncio.create_task(self._run_assist(key, url))

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
                await self._disconnect()
            await asyncio.sleep(self.interval)
