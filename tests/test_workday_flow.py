import unittest
from unittest.mock import AsyncMock, patch

from playwright.sync_api import sync_playwright

from backend.core.workday_flow import (
    WorkdayDeterministicUnavailable,
    _SUBMISSION_RISK_SCAN_JS,
    _can_run_llm_cleanup,
    _cleanup_succeeded,
    save_open_questions,
    is_workday_url,
    run_workday_deterministic,
)


class SubmissionRiskScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_routine_email_nag_banner_is_not_a_verification_challenge(self):
        # A live LinkedIn Easy Apply run left a job in manual_review over a
        # "CAPTCHA/human verification challenge" that did not exist. The
        # actual page had a routine LinkedIn account nag ("update or confirm
        # your email") sitting in the page's background chrome, outside the
        # Easy Apply modal -- but live inspection showed the modal did not
        # match any of the guessed modal selectors, so scoping alone did not
        # save it. The real fix: "confirm/verify your email" are ambiguous
        # phrases that show up in routine account UI, not just verification
        # challenges, so they are no longer in the pattern list at all.
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div>Emails aren't getting through to one of your email addresses.
                  Please update or confirm your email. More info</div>
                <div>
                  <div>Contact info</div>
                  <input id="email" value="arjun@example.com">
                  <button>Next</button>
                </div>
                """
            )
            result = page.evaluate(_SUBMISSION_RISK_SCAN_JS)
            self.assertNotIn("human_verification", result["flags"], result)
        finally:
            page.close()

    def test_real_captcha_inside_the_modal_is_still_caught(self):
        page = self.browser.new_page()
        try:
            page.set_content(
                """
                <div role="dialog">
                  <div>Please complete the CAPTCHA to continue</div>
                </div>
                """
            )
            result = page.evaluate(_SUBMISSION_RISK_SCAN_JS)
            self.assertIn("human_verification", result["flags"], result)
        finally:
            page.close()

    def test_full_page_form_with_no_modal_still_scans_the_body(self):
        page = self.browser.new_page()
        try:
            page.set_content("<div>Enter the verification code we emailed you</div>")
            result = page.evaluate(_SUBMISSION_RISK_SCAN_JS)
            self.assertIn("human_verification", result["flags"], result)
        finally:
            page.close()


class CanRunLlmCleanupTests(unittest.TestCase):
    def test_allows_cleanup_on_an_opened_easy_apply_modal(self):
        # Preflight already clicked LinkedIn's own Easy Apply button and
        # opened its modal -- the cleanup agent should be allowed to
        # continue it, same as it would on any external ATS page. Blanket-
        # refusing every linkedin.com URL left the agent stuck on page one
        # of the modal, unable to click Next, for every Easy Apply job.
        summary = {"url": "https://www.linkedin.com/jobs/view/12345/", "surface": {}}
        preflight = {"clicked_linkedin": True, "easy_apply": True}
        self.assertTrue(_can_run_llm_cleanup(summary, preflight))

    def test_refuses_cleanup_for_a_non_easy_apply_job_stuck_on_linkedin(self):
        # An external-apply job that never left LinkedIn (button not found,
        # no redirect) is still the stuck case this guard exists for.
        summary = {"url": "https://www.linkedin.com/jobs/view/12345/", "surface": {}}
        preflight = {"clicked_linkedin": True, "easy_apply": False}
        self.assertFalse(_can_run_llm_cleanup(summary, preflight))


class IsWorkdayUrlTests(unittest.TestCase):
    def test_matches_myworkdayjobs_host(self):
        self.assertTrue(is_workday_url("https://acme.wd1.myworkdayjobs.com/en-US/job/apply"))

    def test_matches_myworkdaysite_host(self):
        self.assertTrue(is_workday_url("https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/apply"))

    def test_matches_bare_workday_host(self):
        self.assertTrue(is_workday_url("https://acme.workday.com/apply"))

    def test_rejects_linkedin(self):
        self.assertFalse(is_workday_url("https://www.linkedin.com/jobs/view/123"))

    def test_rejects_lookalike_domain(self):
        # "workday" appearing as a path segment or unrelated host must not match.
        self.assertFalse(is_workday_url("https://notworkday.example.com/apply"))
        self.assertFalse(is_workday_url("https://example.com/workday/apply"))

    def test_rejects_empty_or_malformed(self):
        self.assertFalse(is_workday_url(""))
        self.assertFalse(is_workday_url("not a url"))


class RunWorkdayDeterministicTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_run_has_no_blockers_and_never_submits(self):
        review = {
            "url": "https://acme.wd1.myworkdayjobs.com/en-US/job/review",
            "requiredEmpty": 0,
            "invalidFields": 0,
            "verificationCodeRequired": False,
            "credentialError": False,
            "needsLlm": [],
            "visibleErrors": [],
            "safe_progress": [],
        }
        with (
            patch("backend.core.workday_flow._static_fill_passes", new=AsyncMock(return_value=review)),
            patch("backend.core.workday_flow._run_llm_cleanup") as cleanup,
            patch("backend.core.workday_flow.try_controlled_final_submit") as submit,
        ):
            result = await run_workday_deterministic(
                browser=object(),
                facts={"job_title": "SWE Intern", "job_company": "Acme"},
                resume_path="/tmp/resume.pdf",
                worker_id=1,
            )

        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["blockers"], ["ready for final review; static engine stopped before Submit"])
        cleanup.assert_not_called()
        submit.assert_not_called()

    async def test_stuck_field_triggers_bounded_llm_cleanup(self):
        review_before = {
            "url": "https://acme.wd1.myworkdayjobs.com/en-US/job/apply",
            "requiredEmpty": 1,
            "invalidFields": 0,
            "verificationCodeRequired": False,
            "credentialError": False,
            "needsLlm": ["why do you want to work here"],
            "visibleErrors": [],
            "requiredEmptyLabels": ["Why do you want to work here?"],
            "safe_progress": [],
        }
        cleaned_review = {**review_before, "requiredEmpty": 0, "needsLlm": []}
        with (
            patch("backend.core.workday_flow._static_fill_passes", new=AsyncMock(return_value=review_before)),
            patch(
                "backend.core.workday_flow._run_llm_cleanup",
                new=AsyncMock(return_value={
                    "attempted": True,
                    "steps": 4,
                    "timeout": False,
                    "success": True,
                    "stop_reason": "",
                    "last_review": cleaned_review,
                }),
            ) as cleanup,
            patch("backend.core.workday_flow.try_controlled_final_submit") as submit,
        ):
            result = await run_workday_deterministic(
                browser=object(),
                facts={"job_title": "SWE Intern", "job_company": "Acme"},
                resume_path="/tmp/resume.pdf",
                worker_id=1,
            )

        cleanup.assert_awaited_once()
        submit.assert_not_called()
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["blockers"], ["ready for final review; static engine stopped before Submit"])

    async def test_raises_unavailable_when_no_progress_made(self):
        with patch(
            "backend.core.workday_flow._static_fill_passes",
            new=AsyncMock(side_effect=RuntimeError("cdp session lost")),
        ):
            with self.assertRaises(WorkdayDeterministicUnavailable):
                await run_workday_deterministic(
                    browser=object(),
                    facts={},
                    resume_path="/tmp/resume.pdf",
                    worker_id=1,
                )

    async def test_late_failure_is_folded_into_manual_review_result(self):
        review = {
            "url": "https://acme.wd1.myworkdayjobs.com/en-US/job/apply",
            "requiredEmpty": 0,
            "invalidFields": 0,
            "verificationCodeRequired": False,
            "credentialError": False,
            "needsLlm": ["stock question"],
            "visibleErrors": [],
            "safe_progress": [],
        }
        with (
            patch("backend.core.workday_flow._static_fill_passes", new=AsyncMock(return_value=review)),
            patch(
                "backend.core.workday_flow._run_llm_cleanup",
                new=AsyncMock(side_effect=RuntimeError("agent crashed")),
            ),
        ):
            result = await run_workday_deterministic(
                browser=object(),
                facts={},
                resume_path="/tmp/resume.pdf",
                worker_id=1,
            )

        self.assertEqual(result["status"], "manual_review")
        self.assertIn("deterministic engine error", result["blockers"][0])


if __name__ == "__main__":
    unittest.main()


class CleanupSucceededTests(unittest.TestCase):
    def test_agent_success_with_required_blanks_is_not_a_success(self):
        # The cleanup agent reported success after four steps while eight
        # required fields were still empty, so the job was recorded as finished
        # when it was not.
        review = {"requiredEmpty": 8, "needsLlm": ["required-empty: cards[abc][field0]"]}

        self.assertFalse(_cleanup_succeeded(True, review))

    def test_agent_success_with_nothing_left_is_a_success(self):
        self.assertTrue(_cleanup_succeeded(True, {"requiredEmpty": 0, "needsLlm": []}))

    def test_agent_failure_is_never_upgraded_to_success(self):
        self.assertFalse(_cleanup_succeeded(False, {"requiredEmpty": 0, "needsLlm": []}))

    def test_abandoned_fields_do_not_block_success(self):
        # A field abandoned after three attempts is a deliberate hand-off to the
        # human, not unfinished cleanup work.
        review = {"requiredEmpty": 1, "needsLlm": [], "abandoned": 1}

        self.assertTrue(_cleanup_succeeded(True, review))


class SaveOpenQuestionsTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.added = []

        def qa_add(self, question, answer="", question_type="text", source_domain=""):
            self.added.append((question, answer, question_type, source_domain))
            return {"id": len(self.added)}

    def test_open_questions_are_banked_with_type_and_domain(self):
        # Review mode never wrote to the Q&A bank, so every question the
        # candidate answered by hand was discarded and asked again next time.
        store = self.FakeStore()
        summary = {
            "open_questions": [
                {"question": "What is the hardest technical challenge you have faced?",
                 "type": "textarea", "required": True},
            ]
        }

        saved = save_open_questions(
            summary, "https://jobs.lever.co/palantir/abc/apply", store=store
        )

        self.assertEqual(saved, 1)
        question, answer, qtype, domain = store.added[0]
        self.assertEqual(question, "What is the hardest technical challenge you have faced?")
        self.assertEqual(answer, "")
        self.assertEqual(qtype, "textarea")
        self.assertEqual(domain, "jobs.lever.co")

    def test_nothing_is_banked_when_there_are_no_open_questions(self):
        store = self.FakeStore()

        self.assertEqual(save_open_questions({"open_questions": []}, "https://x.com", store=store), 0)
        self.assertEqual(store.added, [])

    def test_a_missing_store_is_not_an_error(self):
        summary = {"open_questions": [{"question": "Why this company?", "type": "textarea"}]}

        self.assertEqual(save_open_questions(summary, "https://x.com", store=None), 0)
