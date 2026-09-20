"""Keep LangHire's Pause/Resume AI control on every tab of the automation browser, and run
autofill on a tab when the candidate presses Resume, even after the run that opened it is over.

Runs finish and leave their tabs open for the candidate. Without this the control disappeared
with the run, so a page the candidate wanted the AI to fill again had no way to ask for it.

Watching is done over one small DevTools connection per tab, every call with a deadline, so a
stalled tab (or a stalled connection) is dropped on its own instead of freezing the watcher. A
browser-use session manages tabs for its agent (it recreates "missing" ones and reuses blank
ones), so it is only opened for the few minutes an assist takes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from typing import Any, Callable

from browser_use import BrowserSession

try:
    from core.cdp_tab import CdpTab
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
    from backend.core.cdp_tab import CdpTab
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


_log = logging.getLogger("langhire.assist")


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


async def _list_targets(endpoint: str) -> list[dict[str, Any]]:
    def fetch() -> list[dict[str, Any]]:
        with urllib.request.urlopen(f"{endpoint}/json/list", timeout=4) as response:  # noqa: S310 (loopback)
            return json.loads(response.read().decode())

    return await asyncio.wait_for(asyncio.to_thread(fetch), 6)


def _value(reply: Any) -> Any:
    return ((reply or {}).get("result") or {}).get("value")


class RawCdpPoller:
    """Reads and sets the control on every http(s) tab, one DevTools connection per tab."""

    def __init__(self, list_targets: Callable[..., Any] = _list_targets, open_tab: Callable[..., Any] = CdpTab.open) -> None:
        self._list_targets = list_targets
        self._open_tab = open_tab
        self._endpoint: str | None = None
        self._tabs: dict[str, tuple[Any, str]] = {}

    async def connect(self) -> bool:
        endpoint = automation_browser_endpoint()
        if not endpoint:
            return False
        self._endpoint = endpoint
        return True

    async def _drop(self, key: str) -> None:
        entry = self._tabs.pop(key, None)
        if entry is not None:
            await entry[0].close()

    async def _poll(self, key: str, target: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
        url = str(target.get("url") or "")
        try:
            entry = self._tabs.get(key)
            if entry is None or not entry[0].alive:
                tab = await self._open_tab(target["webSocketDebuggerUrl"])
                # Runs the control's script on every navigation and on the page's first load.
                await tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": _pause_control_overlay_script()})
                self._tabs[key] = (tab, url)
            tab = self._tabs[key][0]
            # Idempotent: installs, or re-mounts a removed panel, and reports the state.
            state = _value(await tab.call(
                "Runtime.evaluate", {"expression": _pause_control_overlay_script(), "returnByValue": True}
            ))
            return key, url, state if isinstance(state, dict) else None
        except Exception:
            await self._drop(key)
            return key, url, None

    async def tabs(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        targets = await self._list_targets(self._endpoint)
        wanted = {
            str(t["id"]): t
            for t in targets
            if t.get("type") == "page"
            and str(t.get("url") or "").startswith(("http://", "https://"))
            and t.get("webSocketDebuggerUrl")
        }
        for key in [key for key in self._tabs if key not in wanted]:
            await self._drop(key)
        return list(await asyncio.gather(*(self._poll(key, target) for key, target in wanted.items())))

    async def set_paused(self, key: str, paused: bool) -> None:
        entry = self._tabs.get(key)
        if entry is not None and entry[0].alive:
            await entry[0].call(
                "Runtime.evaluate", {"expression": _pause_control_overlay_script(paused), "returnByValue": True}
            )

    async def assist(self, key: str, url: str, cancel_flag: dict) -> None:
        if self._endpoint is None:
            return
        browser = BrowserSession(cdp_url=self._endpoint, keep_alive=True)
        await asyncio.wait_for(browser.start(), 20)
        try:
            await _assist(browser, key, url, cancel_flag)  # the target id is the /json/list id
        finally:
            try:
                await asyncio.wait_for(browser.stop(), 5)  # keep_alive: leaves the browser running
            except Exception:
                pass

    async def close(self) -> None:
        for key in list(self._tabs):
            await self._drop(key)


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
        self.poller = poller or RawCdpPoller()
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
            _log.warning("Resume AI autofill stopped: %s: %s", type(exc).__name__, str(exc)[:160])
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
            if key in self.assisting:
                continue  # its fill owns the control now; the engine re-arms it as "AI is working"
            action = plan_tab_action(state)
            if action == "idle":
                await self.poller.set_paused(key, True)
            elif action == "assist" and key not in self.assisting:
                self.assisting.add(key)
                asyncio.create_task(self._run_assist(key, url))

    async def run_forever(self) -> None:
        beat = 0.0
        while True:
            task = asyncio.ensure_future(self.tick())
            try:
                done, _pending = await asyncio.wait({task}, timeout=self.tick_timeout)
                if not done:
                    # Do not await the cancelled tick: something that ignores cancellation must
                    # not be able to stall the loop (that is how the last watcher froze).
                    task.cancel()
                    _log.warning("assist tick timed out; reconnecting")
                    self.connected = False
                    asyncio.ensure_future(self._safe_close())
                else:
                    task.result()
            except asyncio.CancelledError:
                task.cancel()
                await self._safe_close()
                raise
            except Exception as exc:
                _log.warning("assist watcher error: %s: %s", type(exc).__name__, str(exc)[:160])
                self.connected = False
                asyncio.ensure_future(self._safe_close())
            now = asyncio.get_running_loop().time()
            if now - beat >= 300:
                beat = now
                _log.info("assist watcher alive: connected=%s assisting=%d", self.connected, len(self.assisting))
            await asyncio.sleep(self.interval)

    async def _safe_close(self) -> None:
        try:
            await asyncio.wait_for(self.poller.close(), 5)
        except BaseException:
            pass
