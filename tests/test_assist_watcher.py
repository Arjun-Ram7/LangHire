import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.core.assist_watcher import AssistWatcher, plan_tab_action


def test_plan_covers_every_state_of_the_pause_control():
    assert plan_tab_action({"installed": True, "paused": True, "userChanged": False}) == "none"
    assert plan_tab_action({"installed": True, "paused": True, "userChanged": True}) == "none"
    # A control nobody has touched ("AI is working") means no run owns this tab: make it idle.
    assert plan_tab_action({"installed": True, "paused": False, "userChanged": False}) == "idle"
    # The candidate pressed Resume AI.
    assert plan_tab_action({"installed": True, "paused": False, "userChanged": True}) == "assist"
    assert plan_tab_action({"installed": False}) == "none"
    assert plan_tab_action(None) == "none"


def _tab(target_id, url):
    return SimpleNamespace(target_id=target_id, url=url)


class Harness:
    """A fake browser with per-tab control state, driving AssistWatcher.tick()."""

    def __init__(self, tabs, states, busy=False):
        self.tabs = tabs
        self.states = states  # target_id -> control snapshot
        self.busy = busy
        self.assisted: list[str] = []
        self.assist_gate = asyncio.Event()
        self.assist_gate.set()
        self.browser = SimpleNamespace(get_tabs=AsyncMock(side_effect=lambda: self.tabs), stop=AsyncMock())
        self.connects = 0

    async def connect(self):
        self.connects += 1
        return self.browser

    async def install_controls(self, _browser, ids, paused):
        results = []
        for target_id in ids:
            state = self.states.setdefault(target_id, {"installed": True, "paused": False, "userChanged": False})
            if paused is not None:
                state.update(paused=paused, userChanged=False)
            results.append({**state, "target_id": target_id})
        return results

    async def assist(self, _browser, target_id, _url):
        self.assisted.append(target_id)
        await self.assist_gate.wait()

    def watcher(self):
        return AssistWatcher(
            is_busy=lambda: self.busy,
            connect=self.connect,
            install_controls=self.install_controls,
            assist=self.assist,
        )


class AssistWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_http_tab_gets_the_control_and_starts_paused_once_the_run_is_over(self):
        h = Harness([_tab("A", "https://x.wd5.myworkdayjobs.com/apply"), _tab("B", "https://www.fapply.ai/p"),
                     _tab("C", "chrome-extension://abc/page.html"), _tab("D", "about:blank")], {})

        await h.watcher().tick()

        self.assertEqual(set(h.states), {"A", "B"})
        self.assertTrue(all(state["paused"] for state in h.states.values()))

    async def test_pressing_resume_runs_autofill_on_that_tab_only_and_only_once(self):
        h = Harness([_tab("A", "https://a.example/apply"), _tab("B", "https://b.example/apply")],
                    {"A": {"installed": True, "paused": True, "userChanged": False},
                     "B": {"installed": True, "paused": False, "userChanged": True}})
        h.assist_gate.clear()
        watcher = h.watcher()

        await watcher.tick()
        await watcher.tick()  # still running: must not start a second pass
        await asyncio.sleep(0)
        self.assertEqual(h.assisted, ["B"])

        h.assist_gate.set()
        await asyncio.sleep(0.05)
        self.assertTrue(h.states["B"]["paused"], "the control returns to Resume AI when autofill finishes")

    async def test_nothing_is_touched_while_a_run_owns_the_browser(self):
        h = Harness([_tab("A", "https://a.example/apply")], {}, busy=True)

        await h.watcher().tick()

        self.assertEqual((h.connects, h.states, h.assisted), (0, {}, []))

    async def test_no_browser_means_nothing_to_do(self):
        h = Harness([], {})
        watcher = h.watcher()
        watcher.connect = AsyncMock(return_value=None)

        await watcher.tick()

        self.assertEqual(h.assisted, [])

    async def test_a_dropped_browser_connection_is_re_established_next_tick(self):
        h = Harness([_tab("A", "https://a.example/apply")], {})
        watcher = h.watcher()
        await watcher.tick()
        h.browser.get_tabs.side_effect = RuntimeError("connection closed")
        await watcher.tick()
        h.browser.get_tabs.side_effect = lambda: h.tabs
        await watcher.tick()

        self.assertEqual(h.connects, 2)

    async def test_a_hung_browser_call_cannot_freeze_the_watcher_forever(self):
        h = Harness([_tab("A", "https://a.example/apply")], {})
        watcher = h.watcher()
        watcher.interval = 0.01
        watcher.tick_timeout = 0.05
        hang = asyncio.Event()

        async def hanging_connect():
            h.connects += 1
            await hang.wait()

        watcher.connect = hanging_connect
        task = asyncio.create_task(watcher.run_forever())
        await asyncio.sleep(0.4)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertGreater(h.connects, 1)
