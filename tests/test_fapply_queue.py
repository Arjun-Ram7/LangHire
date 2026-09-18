import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.sync_api import sync_playwright

from backend.models import ApplyRequest
from cli.apply_jobs import _click_apply_button_script
from cli.fapply_queue import (
    FAPPLY_EXTENSION_ID,
    _account_only_facts,
    _baseline_tab_ids,
    _run_job_with_deadline,
    assess_fill_evidence,
    classify_application_surface,
    fapply_browser_kwargs,
    find_fapply_extension_path,
    first_job_per_company,
    is_account_surface,
    open_with_fapply,
    order_jobs_by_company,
    sign_in_sweep,
    workday_sweep_candidates,
)


class FapplyDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_job_times_out_and_is_marked_failed(self):
        async def stalled(*_args, **_kwargs):
            await asyncio.sleep(10)

        job = {"url": "https://example.com/job", "title": "Engineer", "company": "Acme"}
        with (
            patch("cli.fapply_queue.open_with_fapply", side_effect=stalled),
            patch("cli.fapply_queue._page_url", new=AsyncMock(return_value="https://example.com/apply")),
            patch("cli.fapply_queue.update_job") as update,
        ):
            result = await _run_job_with_deadline(
                object(), job, {}, 1, timeout_seconds=0.01
            )

        self.assertEqual(result, "timed_out")
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["status"], "failed")
        self.assertTrue(
            update.call_args.kwargs["manual_review_summary"]["fapply"]["timed_out"]
        )


class WorkdayFapplyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, url, result):
        browser = AsyncMock()
        browser.get_tabs.return_value = []
        deadline = MagicMock()
        with (
            patch('cli.fapply_queue.claim_job', return_value=True),
            patch('cli.fapply_queue.update_job') as update,
            patch('cli.fapply_queue._run_apply_preflight', new=AsyncMock(return_value={})),
            patch('cli.fapply_queue._wait_for_page_settle', new=AsyncMock()),
            patch('cli.fapply_queue._select_application_tab', new=AsyncMock(return_value=(
                {'url': url, 'controlCount': 5}, {'is_application': True}, ''
            ))),
            patch('cli.fapply_queue._page_url', new=AsyncMock(return_value=url)),
            patch('cli.fapply_queue.load_autofill_facts', return_value={}),
            patch('cli.fapply_queue.run_workday_deterministic', new=AsyncMock(return_value={'summary': {}})) as pages,
            patch('cli.fapply_queue._current_page', new=AsyncMock(return_value=object())),
            patch('cli.fapply_queue._probe', new=AsyncMock(return_value={
                'review': result.get('reached_review', False), 'blockers': [result['reason']] if result.get('reason') else [],
            })),
            patch('cli.fapply_queue.release_review_handoff', new=AsyncMock()) as release,
            patch('cli.fapply_queue.run_fapply_and_verify', new=AsyncMock(return_value=result)) as single,
        ):
            status = await open_with_fapply(browser, {'url': url}, {}, 1, deadline=deadline)
            if pages.await_count:
                self.assertTrue(pages.call_args.kwargs['llm_cleanup'])
                self.assertEqual(pages.call_args.kwargs['passes'], 12)
                release.assert_awaited_once()
            return status, update.call_args.kwargs, pages.await_count, single.await_count, deadline

    async def test_workday_uses_multi_page_budget_and_records_review(self):
        status, saved, pages, single, deadline = await self.run_job(
            'https://amgen.wd1.myworkdayjobs.com/job/1/apply',
            {'verified': True, 'reached_review': True, 'pages': [{'step': 'My Information'}]},
        )
        self.assertEqual(status, 'workday_review_ready')
        self.assertEqual(saved['status'], 'manual_review')
        self.assertIsNone(saved['error'])
        self.assertEqual((pages, single), (1, 0))
        self.assertEqual(saved['manual_review_summary']['workday']['engine'], 'langhire')
        self.assertTrue(saved['manual_review_summary']['workday']['reached_review'])
        deadline.reschedule.assert_called_once()

    async def test_unanswered_workday_page_is_manual_review_not_failed_or_applied(self):
        status, saved, *_ = await self.run_job(
            'https://amgen.wd1.myworkdayjobs.com/job/1/apply',
            {'verified': False, 'reached_review': False, 'reason': 'Country required'},
        )
        self.assertEqual(status, 'workday_needs_input')
        self.assertEqual(saved['status'], 'manual_review')
        self.assertEqual(saved['error'], 'Country required')

    async def test_other_ats_keeps_single_page_flow_and_original_budget(self):
        status, saved, pages, single, deadline = await self.run_job(
            'https://jobs.ashbyhq.com/company/job/application', {'verified': True},
        )
        self.assertEqual(status, 'fapply_verified')
        self.assertEqual((pages, single), (0, 1))
        deadline.reschedule.assert_not_called()


WEX_CREATE_ACCOUNT = {
    "url": "https://wexinc.wd5.myworkdayjobs.com/en-US/wexinc/job/US---Remote/Intern_R1/apply/applyManually",
    "title": "Create Account",
    "bodyText": "Create Account Password Requirements Email Address Password Verify New Password "
                "Create Account Already have an account? Sign In Forgot your password?",
    "controlCount": 3,
    "identityCount": 1,
    "fileInputs": 0,
    "passwordInputs": 2,
    "buttonLabels": ["Create Account"],
}


def _tab(target_id, url):
    return MagicMock(target_id=target_id, url=url)


def test_baseline_ignores_blank_tabs_that_navigation_will_reuse():
    tabs = [
        _tab("BLANK", "about:blank"),
        _tab("NTP", "chrome://newtab/"),
        _tab("FAPPLY", "https://www.fapply.ai/profile_htmls/summary.html"),
    ]

    assert _baseline_tab_ids(tabs) == {"FAPPLY"}


class FreshBrowserWorkdayTests(unittest.IsolatedAsyncioTestCase):
    """A just-launched Brave hands its blank tab to the first navigation."""

    async def run_job(self, surface, *, tab_id="BLANK", baseline_url="about:blank"):
        state = {"opened": False}
        url = surface["url"]

        async def get_tabs():
            if not state["opened"]:
                return [_tab("BLANK", baseline_url)]
            tabs = [_tab(tab_id, url)]
            if tab_id != "BLANK":
                tabs.insert(0, _tab("BLANK", baseline_url))
            return tabs

        async def preflight(*_args, **_kwargs):
            state["opened"] = True
            return {}

        browser = AsyncMock()
        browser.get_tabs = get_tabs
        with (
            patch("cli.fapply_queue.claim_job", return_value=True),
            patch("cli.fapply_queue.update_job") as update,
            patch("cli.fapply_queue._run_apply_preflight", new=preflight),
            patch("cli.fapply_queue._wait_for_page_settle", new=AsyncMock()),
            patch("cli.fapply_queue._switch_to_tab", new=AsyncMock()),
            patch("cli.fapply_queue.probe_application_surface", new=AsyncMock(
                return_value=(surface, classify_application_surface(surface))
            )),
            patch("cli.fapply_queue._navigate_and_clear_account_gate", new=AsyncMock(
                return_value={"attempted": False}
            )) as llm_gate,
            patch("cli.fapply_queue._close_owned_landing_tabs", new=AsyncMock(return_value=0)),
            patch("cli.fapply_queue._page_url", new=AsyncMock(return_value=url)),
            patch("cli.fapply_queue.load_autofill_facts", return_value={}),
            patch("cli.fapply_queue.run_workday_deterministic", new=AsyncMock(return_value={"summary": {}})) as engine,
            patch("cli.fapply_queue._current_page", new=AsyncMock(return_value=object())),
            patch("cli.fapply_queue._probe", new=AsyncMock(return_value={"review": False, "blockers": []})),
            patch("cli.fapply_queue.release_review_handoff", new=AsyncMock()),
        ):
            status = await open_with_fapply(browser, {"url": url}, {}, 1, deadline=MagicMock())
        return status, engine, llm_gate, update

    async def test_workday_form_in_the_reused_blank_tab_is_found(self):
        form = {
            "url": "https://wexinc.wd5.myworkdayjobs.com/en-US/wexinc/job/US---Remote/Intern_R1/apply/useMyLastApplication",
            "title": "My Information",
            "bodyText": "Job Application My Information Legal Name First Name Last Name Email Phone Resume",
            "controlCount": 9,
            "identityCount": 5,
            "fileInputs": 1,
            "passwordInputs": 0,
            "buttonLabels": ["Save and Continue"],
        }

        status, engine, _llm_gate, _update = await self.run_job(form)

        self.assertEqual(status, "workday_needs_input")
        engine.assert_awaited_once()

    async def test_workday_create_account_goes_to_engine_for_human_click_checkpoint(self):
        status, engine, llm_gate, update = await self.run_job(WEX_CREATE_ACCOUNT, tab_id="NEW")

        self.assertEqual(status, "workday_needs_input")
        engine.assert_awaited_once()
        llm_gate.assert_not_awaited()
        self.assertNotEqual(update.call_args.kwargs["status"], "failed")

    async def test_non_workday_account_gate_still_uses_navigation_agent(self):
        surface = {**WEX_CREATE_ACCOUNT, "url": "https://jobs.example.com/apply/login"}

        status, engine, llm_gate, _update = await self.run_job(surface, tab_id="NEW")

        self.assertEqual(status, "not_application")
        engine.assert_not_awaited()
        llm_gate.assert_awaited_once()


def _queued(company, url):
    return {"company": company, "url": url, "title": "Intern"}


QUEUE = [_queued("Acme", "1"), _queued("Beta", "2"), _queued("acme ", "3"), _queued("Gamma", "4"), _queued("Beta", "5")]


def test_jobs_at_the_same_company_run_back_to_back_in_first_seen_order():
    assert [j["url"] for j in order_jobs_by_company(QUEUE)] == ["1", "3", "2", "5", "4"]


def test_jobs_without_a_company_keep_their_place_and_are_never_grouped():
    jobs = [_queued("", "1"), _queued("Acme", "2"), _queued("", "3"), _queued("Acme", "4")]

    assert [j["url"] for j in order_jobs_by_company(jobs)] == ["1", "2", "4", "3"]


def test_one_job_per_company_is_picked_for_the_sign_in_sweep():
    assert [j["url"] for j in first_job_per_company(QUEUE)] == ["1", "2", "4"]


def test_only_companies_already_known_to_use_workday_are_swept():
    # The queue cannot know a new company's ATS before opening it; a previous run's
    # recorded application URL is the only up-front signal.
    workday = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x/apply"
    jobs = [
        _queued("Acme", "1"),
        {**_queued("Acme", "2"), "manual_review_url": workday},
        {**_queued("Beta", "3"), "manual_review_url": "https://jobs.lever.co/beta/x/apply"},
        _queued("Gamma", "4"),
    ]

    assert [j["url"] for j in workday_sweep_candidates(jobs)] == ["2"]


class SignInSweepTests(unittest.IsolatedAsyncioTestCase):
    """Front-load the once-per-company Create Account / Sign In click."""

    async def sweep(self, gate_outcomes, jobs=QUEUE, cancel_flag=None):
        outcomes = iter(gate_outcomes)
        browser = AsyncMock()
        browser.get_tabs.return_value = []
        gate_surface = {**WEX_CREATE_ACCOUNT}
        with (
            patch("cli.fapply_queue._run_apply_preflight", new=AsyncMock(return_value={})) as preflight,
            patch("cli.fapply_queue._wait_for_page_settle", new=AsyncMock()),
            patch("cli.fapply_queue._select_application_tab", new=AsyncMock(
                return_value=(gate_surface, {"is_application": False}, "T")
            )),
            patch("cli.fapply_queue._account_only_facts", return_value={"account_email": "a@b.c"}),
            patch("cli.fapply_queue._clear_workday_account_gate", new=AsyncMock(side_effect=lambda *_a: next(outcomes))) as clear,
            patch("cli.fapply_queue._close_owned_landing_tabs", new=AsyncMock(return_value=1)) as close,
        ):
            result = await sign_in_sweep(browser, jobs, {}, cancel_flag=cancel_flag)
        return result, preflight, clear, close

    async def test_visits_each_company_once_and_closes_the_tab_after_the_click(self):
        result, preflight, clear, close = await self.sweep(["cleared", "cleared", "cleared"])

        self.assertEqual(result, {"acme": "cleared", "beta": "cleared", "gamma": "cleared"})
        self.assertEqual([c.kwargs["url"] for c in preflight.await_args_list], ["1", "2", "4"])
        self.assertEqual(clear.await_count, 3)
        self.assertEqual(close.await_count, 3)

    async def test_a_company_already_signed_in_needs_no_click(self):
        result, _preflight, clear, _close = await self.sweep(["no_gate", "cleared", "no_gate"])

        self.assertEqual(result, {"acme": "no_gate", "beta": "cleared", "gamma": "no_gate"})

    async def test_the_sweep_stops_when_nobody_clicks_so_the_run_is_not_held_up(self):
        result, preflight, _clear, _close = await self.sweep(["cleared", "timed_out", "cleared"])

        self.assertEqual(result, {"acme": "cleared", "beta": "timed_out"})
        self.assertEqual(preflight.await_count, 2)

    async def test_a_stop_request_ends_the_sweep_before_any_navigation(self):
        result, preflight, _clear, _close = await self.sweep([], cancel_flag={"cancel_requested": True})

        self.assertEqual(result, {})
        preflight.assert_not_awaited()


def test_apply_request_accepts_fapply_mode():
    assert ApplyRequest(mode="fapply").mode == "fapply"


def test_real_application_form_is_accepted():
    result = classify_application_surface(
        {
            "url": "https://jobs.lever.co/example/role-id/apply",
            "title": "Apply - Software Engineer",
            "bodyText": "Job Application Full name Email Phone Resume Submit application",
            "controlCount": 9,
            "identityCount": 5,
            "fileInputs": 1,
            "passwordInputs": 0,
            "buttonLabels": ["Submit application"],
        }
    )

    assert result["is_application"] is True
    assert result["score"] >= 5


def test_job_landing_page_is_rejected_even_on_ats_host():
    result = classify_application_surface(
        {
            "url": "https://jobs.lever.co/example/role-id",
            "title": "Software Engineer",
            "bodyText": "About the company Responsibilities Qualifications Apply for this job",
            "controlCount": 0,
            "identityCount": 0,
            "fileInputs": 0,
            "passwordInputs": 0,
            "buttonLabels": ["Apply for this job"],
        }
    )

    assert result["is_application"] is False


def test_account_sign_in_page_is_not_mistaken_for_application():
    surface = {
        "url": "https://careers.example.com/application/login",
        "title": "Sign in",
        "bodyText": "Sign in or create an account Email Password Forgot password",
        "controlCount": 2,
        "identityCount": 1,
        "fileInputs": 0,
        "passwordInputs": 1,
        "buttonLabels": ["Sign in"],
    }
    result = classify_application_surface(surface)

    assert result["is_application"] is False
    assert "sign-in" in result["reason"]
    assert is_account_surface(surface) is True


def test_detailed_account_creation_page_is_not_mistaken_for_application():
    surface = {
        "url": "https://careers.example.com/application/apply",
        "title": "Create an account to apply",
        "bodyText": "Create an account First name Last name Email Phone Password Accept terms",
        "controlCount": 7,
        "identityCount": 5,
        "fileInputs": 0,
        "passwordInputs": 2,
        "buttonLabels": ["Create account"],
    }

    result = classify_application_surface(surface)

    assert result["is_application"] is False
    assert "account/sign-in" in result["reason"]
    assert is_account_surface(surface) is True


def test_account_deterministic_facts_exclude_application_answers():
    facts = _account_only_facts(
        {
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "phone": "+15555550100",
            "address": {"city": "London"},
            "skills": ["Python"],
            "work_authorization": "Authorized",
        }
    )

    assert facts["email"]
    assert set(facts) <= {
        "first_name",
        "last_name",
        "full_name",
        "email",
        "account_email",
        "account_password",
        "password",
        "phone",
        "phone_full",
        "terms_accepted",
        "privacy_accepted",
    }
    assert "skills" not in facts
    assert "work_authorization" not in facts


def test_fill_verification_requires_real_delta_or_positive_fapply_report():
    before = {"populated": 3, "total": 10}

    assert assess_fill_evidence(before, {"populated": 3}, {})["verified"] is False
    assert assess_fill_evidence(before, {"populated": 5}, {}) == {
        "verified": True,
        "reported": 0,
        "delta": 2,
        "filled_by_fapply": 2,
    }
    assert assess_fill_evidence(before, {"populated": 3}, {"filledCount": 4})["verified"] is True


def test_finds_newest_fapply_extension_from_override(tmp_path, monkeypatch):
    extension = tmp_path / "2.11_0"
    extension.mkdir()
    (extension / "manifest.json").write_text(json.dumps({"name": "fapply - AI co-pilot"}))
    monkeypatch.setenv("LANGHIRE_FAPPLY_EXTENSION_PATH", str(extension))

    assert find_fapply_extension_path() == extension.resolve()


def test_browser_args_load_only_fapply_extension(tmp_path):
    extension = tmp_path / FAPPLY_EXTENSION_ID / "2.11_0"
    extension.mkdir(parents=True)

    kwargs = fapply_browser_kwargs(extension)

    assert f"--load-extension={extension}" in kwargs["args"]
    assert f"--disable-extensions-except={extension}" in kwargs["args"]


def test_landing_page_apply_is_navigation_candidate_but_form_apply_is_not():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content('<button id="landing">Apply</button>')
            landing = page.evaluate(_click_apply_button_script("external_apply"), "external_apply")
            assert landing["clicked"] is True
            assert landing["trusted_click_required"] is True

            page.set_content(
                '<form><input name="first"><input name="email"><button id="final">Apply</button></form>'
            )
            final = page.evaluate(_click_apply_button_script("external_apply"), "external_apply")
            assert final["clicked"] is False
        finally:
            browser.close()
