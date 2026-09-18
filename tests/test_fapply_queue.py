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
    _run_job_with_deadline,
    assess_fill_evidence,
    classify_application_surface,
    fapply_browser_kwargs,
    find_fapply_extension_path,
    is_account_surface,
    open_with_fapply,
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
