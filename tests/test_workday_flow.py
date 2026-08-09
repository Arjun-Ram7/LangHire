import unittest
from unittest.mock import AsyncMock, patch

from backend.core.workday_flow import (
    WorkdayDeterministicUnavailable,
    is_workday_url,
    run_workday_deterministic,
)


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
