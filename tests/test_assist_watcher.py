import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core import assist_watcher
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


class FakePoller:
    """Stands in for the browser: per-tab control state, driven through AssistWatcher.tick()."""

    def __init__(self, tabs, states):
        self.tabs_now = dict(tabs)  # key -> url
        self.states = states  # key -> control snapshot
        self.assisted: list[str] = []
        self.flags: dict[str, dict] = {}
        self.assist_gate = asyncio.Event()
        self.assist_gate.set()
        self.connects = 0
        self.closes = 0
        self.fail_tabs = False
        self.connect_gate = None

    async def connect(self):
        self.connects += 1
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        return True

    async def tabs(self):
        if self.fail_tabs:
            raise RuntimeError("connection closed")
        out = []
        for key, url in self.tabs_now.items():
            state = self.states.setdefault(key, {"installed": True, "paused": False, "userChanged": False})
            out.append((key, url, dict(state)))
        return out

    async def set_paused(self, key, paused):
        self.states.setdefault(key, {"installed": True})
        self.states[key].update(paused=paused, userChanged=False)

    async def assist(self, key, url, cancel_flag):
        self.assisted.append(key)
        self.flags[key] = cancel_flag
        await self.assist_gate.wait()

    async def close(self):
        self.closes += 1


class Harness:
    def __init__(self, tabs, states, busy=False):
        self.poller = FakePoller(tabs, states)
        self.busy = busy

    def watcher(self):
        return AssistWatcher(is_busy=lambda: self.busy, poller=self.poller)


class AssistWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_http_tab_gets_the_control_and_starts_paused_once_the_run_is_over(self):
        h = Harness({"A": "https://x.wd5.myworkdayjobs.com/apply", "B": "https://www.fapply.ai/p"}, {})

        await h.watcher().tick()

        self.assertEqual(set(h.poller.states), {"A", "B"})
        self.assertTrue(all(state["paused"] for state in h.poller.states.values()))

    async def test_pressing_resume_runs_autofill_on_that_tab_only_and_only_once(self):
        h = Harness({"A": "https://a.example/apply", "B": "https://b.example/apply"},
                    {"A": {"installed": True, "paused": True, "userChanged": False},
                     "B": {"installed": True, "paused": False, "userChanged": True}})
        h.poller.assist_gate.clear()
        watcher = h.watcher()

        await watcher.tick()
        await watcher.tick()  # still running: must not start a second pass
        await asyncio.sleep(0)
        self.assertEqual(h.poller.assisted, ["B"])

        h.poller.assist_gate.set()
        await asyncio.sleep(0.05)
        self.assertTrue(h.poller.states["B"]["paused"], "the control returns to Resume AI when autofill finishes")

    async def test_nothing_is_touched_while_a_run_owns_the_browser(self):
        h = Harness({"A": "https://a.example/apply"}, {}, busy=True)

        await h.watcher().tick()

        self.assertEqual((h.poller.connects, h.poller.states, h.poller.assisted), (0, {}, []))

    async def test_no_browser_means_nothing_to_do(self):
        h = Harness({}, {})
        h.poller.connect = AsyncMock(return_value=False)

        await h.watcher().tick()

        self.assertEqual(h.poller.assisted, [])

    async def test_a_dropped_browser_connection_is_re_established_next_tick(self):
        h = Harness({"A": "https://a.example/apply"}, {})
        watcher = h.watcher()
        await watcher.tick()
        h.poller.fail_tabs = True
        await watcher.tick()
        h.poller.fail_tabs = False
        await watcher.tick()

        self.assertEqual(h.poller.connects, 2)

    async def test_closing_the_tab_being_filled_cancels_that_run_instead_of_wandering_off(self):
        # Live: the tab closed mid-run, browser-use moved its focus to "another tab", and the AI
        # helper carried on in the candidate's other applications.
        h = Harness({"A": "https://a.example/apply", "B": "https://b.example/apply"},
                    {"A": {"installed": True, "paused": False, "userChanged": True}})
        h.poller.assist_gate.clear()
        watcher = h.watcher()
        await watcher.tick()
        await asyncio.sleep(0)
        self.assertFalse(h.poller.flags["A"]["cancel_requested"])

        del h.poller.tabs_now["A"]
        await watcher.tick()

        self.assertTrue(h.poller.flags["A"]["cancel_requested"])
        h.poller.assist_gate.set()
        await asyncio.sleep(0.05)

    async def test_a_hung_browser_call_cannot_freeze_the_watcher_forever(self):
        h = Harness({"A": "https://a.example/apply"}, {})
        watcher = h.watcher()
        watcher.interval = 0.01
        watcher.tick_timeout = 0.05
        h.poller.connect_gate = asyncio.Event()  # never set: connect hangs
        task = asyncio.create_task(watcher.run_forever())
        await asyncio.sleep(0.4)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertGreater(h.poller.connects, 1)


class DefaultAssistTests(unittest.IsolatedAsyncioTestCase):
    """What Resume AI actually does on a tab once the run is over."""

    async def run_assist(self, url):
        self.flag = {"cancel_requested": False}
        browser = SimpleNamespace(get_tabs=AsyncMock(return_value=[
            _tab("MINE", url), _tab("OTHER1", "https://other.example/a"), _tab("OTHER2", "https://wexinc.wd5.myworkdayjobs.com/b"),
        ]))
        with (
            patch.object(assist_watcher, "_focus_pause_target", new=AsyncMock()) as focus,
            patch.object(assist_watcher, "load_profile", return_value={"p": 1}),
            patch.object(assist_watcher, "load_autofill_facts", return_value={"f": 1}),
            patch.object(assist_watcher, "run_workday_deterministic", new=AsyncMock(return_value={})) as wizard,
            patch.object(assist_watcher, "fill_current_page", new=AsyncMock()) as one_page,
            patch.object(assist_watcher, "release_review_handoff", new=AsyncMock()) as release,
        ):
            await assist_watcher._assist(browser, "MINE", url, self.flag)
        return focus, wizard, one_page, release

    async def test_on_a_workday_application_it_fills_and_moves_through_the_pages_like_a_run(self):
        # It filled one page and stopped, so the candidate had to press Next on every page.
        focus, wizard, one_page, release = await self.run_assist("https://wexinc.wd5.myworkdayjobs.com/en-US/wexinc/job/x/apply")

        wizard.assert_awaited_once()
        kwargs = wizard.await_args.kwargs
        self.assertEqual((kwargs["passes"], kwargs["llm_cleanup"]), (12, True))
        self.assertEqual(kwargs["profile"], {"p": 1})
        one_page.assert_not_awaited()
        focus.assert_awaited_once()
        release.assert_awaited_once()

    async def test_the_ai_helper_is_kept_to_the_tab_the_candidate_pressed_resume_on(self):
        _focus, wizard, _one_page, _release = await self.run_assist("https://wexinc.wd5.myworkdayjobs.com/en-US/wexinc/job/x/apply")

        self.assertEqual(wizard.await_args.kwargs["ignored_target_ids"], {"OTHER1", "OTHER2"})
        self.assertIs(wizard.await_args.kwargs["cancel_flag"], self.flag)

    async def test_on_any_other_site_it_only_fills_the_page_in_front_of_the_candidate(self):
        _focus, wizard, one_page, release = await self.run_assist("https://jobs.example.com/apply/123")

        one_page.assert_awaited_once()
        wizard.assert_not_awaited()
        release.assert_awaited_once()


class FakeCdpTab:
    def __init__(self, target_id, log, fail=False):
        self.target_id, self.log, self.fail = target_id, log, fail
        self.alive = True
        self.state = {"installed": True, "paused": False, "userChanged": False}

    async def call(self, method, params=None, timeout=5.0):
        self.log.append((self.target_id, method))
        if self.fail:
            self.alive = False
            raise RuntimeError("stalled")
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": dict(self.state)}}
        return {}

    async def close(self):
        self.alive = False


class RawCdpPollerTests(unittest.IsolatedAsyncioTestCase):
    def poller(self, targets, tabs=None):
        self.log = []
        self.tabs = tabs if tabs is not None else {}
        self.targets = targets

        async def list_targets(_endpoint):
            return self.targets

        async def open_tab(ws_url):
            target_id = ws_url.rsplit("/", 1)[-1]
            tab = self.tabs.get(target_id) or FakeCdpTab(target_id, self.log)
            self.tabs[target_id] = tab
            return tab

        poller = assist_watcher.RawCdpPoller(list_targets=list_targets, open_tab=open_tab)
        poller._endpoint = "http://127.0.0.1:1"
        return poller

    @staticmethod
    def target(target_id, url, kind="page"):
        return {"id": target_id, "type": kind, "url": url, "webSocketDebuggerUrl": f"ws://x/devtools/page/{target_id}"}

    async def test_only_http_pages_are_tracked_and_each_gets_the_init_script_once(self):
        poller = self.poller([
            self.target("A", "https://a.example/apply"), self.target("B", "chrome://settings"),
            self.target("C", "https://c.example/", kind="service_worker"),
        ])

        first = await poller.tabs()
        second = await poller.tabs()

        self.assertEqual([(key, url) for key, url, _ in first], [("A", "https://a.example/apply")])
        self.assertEqual(first[0][2], {"installed": True, "paused": False, "userChanged": False})
        self.assertEqual(len(second), 1)
        self.assertEqual([m for t, m in self.log if m == "Page.addScriptToEvaluateOnNewDocument"], ["Page.addScriptToEvaluateOnNewDocument"])

    async def test_a_stalled_tab_is_dropped_without_hiding_the_others(self):
        bad = FakeCdpTab("BAD", [], fail=True)
        poller = self.poller([self.target("BAD", "https://bad.example/"), self.target("OK", "https://ok.example/")], {"BAD": bad})

        tabs = await poller.tabs()

        by_key = {key: state for key, _url, state in tabs}
        self.assertIsNone(by_key["BAD"])
        self.assertTrue(by_key["OK"]["installed"])
        self.assertNotIn("BAD", poller._tabs)

    async def test_a_tab_that_closes_is_forgotten(self):
        poller = self.poller([self.target("A", "https://a.example/"), self.target("B", "https://b.example/")])
        await poller.tabs()
        gone = self.tabs["B"]

        self.targets = [self.target("A", "https://a.example/")]
        tabs = await poller.tabs()

        self.assertEqual([key for key, _u, _s in tabs], ["A"])
        self.assertFalse(gone.alive)

    async def test_set_paused_evaluates_the_control_script_on_that_tab(self):
        poller = self.poller([self.target("A", "https://a.example/")])
        await poller.tabs()

        await poller.set_paused("A", True)

        self.assertEqual(self.log[-1], ("A", "Runtime.evaluate"))

    async def test_connect_is_false_when_the_automation_browser_is_not_running(self):
        poller = self.poller([])
        with patch.object(assist_watcher, "automation_browser_endpoint", return_value=None):
            self.assertFalse(await poller.connect())
        with patch.object(assist_watcher, "automation_browser_endpoint", return_value="http://127.0.0.1:9"):
            self.assertTrue(await poller.connect())
