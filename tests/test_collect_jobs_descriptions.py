import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cli.collect_jobs import _apply_fetched_description, screen_jobs_individually

PROFILE_NEEDS_SPONSORSHIP = {"visa_sponsorship_needed": True}

JOB = {
    "title": "Software Engineer Intern",
    "company": "Honeywell Aerospace",
    "location": "Phoenix, AZ",
}


class ApplyFetchedDescriptionTests(unittest.TestCase):
    def test_saves_description_when_it_passes_constraints(self):
        with patch("cli.collect_jobs.update_job") as update_job:
            _apply_fetched_description(
                "https://www.linkedin.com/jobs/view/1/",
                JOB,
                "We are looking for a software engineer to join our growing team.",
                PROFILE_NEEDS_SPONSORSHIP,
            )
        update_job.assert_called_once_with(
            "https://www.linkedin.com/jobs/view/1/", description="We are looking for a software engineer to join our growing team.",
            location_screening_status="us",
            visa_screening_status="needs_review",
            visa_screening_evidence="No explicit F-1/OPT/CPT or H-1B support statement found",
            screening_checked_at=unittest.mock.ANY,
            status="manual_review",
            screening_status="needs_review",
            error="Posting does not explicitly confirm F-1/OPT/CPT or future H-1B support",
        )

    def test_blocks_job_when_real_description_reveals_citizenship_requirement(self):
        with patch("cli.collect_jobs.update_job") as update_job:
            _apply_fetched_description(
                "https://www.linkedin.com/jobs/view/2/",
                JOB,
                "Due to ITAR requirements, applicants must be a U.S. Person to be considered.",
                PROFILE_NEEDS_SPONSORSHIP,
            )
        kwargs = update_job.call_args.kwargs
        self.assertEqual(kwargs["status"], "blocked")
        self.assertEqual(kwargs["screening_status"], "ineligible")
        self.assertEqual(kwargs["visa_screening_status"], "ineligible")
        self.assertIn("U.S. citizenship", kwargs["error"])
        self.assertIn("ITAR", kwargs["description"])

    def test_explicit_opt_support_moves_job_to_pending(self):
        with patch("cli.collect_jobs.update_job") as update_job:
            _apply_fetched_description(
                "https://www.linkedin.com/jobs/view/4/",
                JOB,
                "F-1 OPT and CPT candidates are welcome for this internship program.",
                PROFILE_NEEDS_SPONSORSHIP,
            )
        kwargs = update_job.call_args.kwargs
        self.assertEqual(kwargs["status"], "pending")
        self.assertEqual(kwargs["screening_status"], "compatible")

    def test_does_nothing_when_no_description_was_extracted(self):
        with patch("cli.collect_jobs.update_job") as update_job:
            _apply_fetched_description(
                "https://www.linkedin.com/jobs/view/3/", JOB, "", PROFILE_NEEDS_SPONSORSHIP
            )
        update_job.assert_not_called()


class ScreenJobsIndividuallyTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_every_job_and_reports_screening_counts(self):
        jobs = {
            "https://www.linkedin.com/jobs/view/4290000001/": {
                **JOB,
                "company": "Sponsor Co",
            },
            "https://www.linkedin.com/jobs/view/4290000002/": {
                **JOB,
                "company": "Restricted Co",
            },
        }
        progress = []
        with (
            patch("cli.collect_jobs.BrowserSession") as browser_cls,
            patch("cli.collect_jobs.clear_stale_browser_session_state"),
            patch(
                "cli.collect_jobs.fetch_description_for_job",
                new=AsyncMock(side_effect=["supported description", "restricted description"]),
            ) as fetch,
            patch(
                "cli.collect_jobs._apply_fetched_description",
                side_effect=[
                    {"screening_status": "compatible", "error": None},
                    {"screening_status": "ineligible", "error": "requires U.S. citizenship"},
                ],
            ),
            patch("cli.collect_jobs.asyncio.sleep", new=AsyncMock()),
        ):
            browser = browser_cls.return_value
            browser.start = AsyncMock()
            browser.close = AsyncMock()
            browser.new_page = AsyncMock(return_value=MagicMock())
            summary = await screen_jobs_individually(
                jobs,
                PROFILE_NEEDS_SPONSORSHIP,
                progress_callback=lambda result: progress.append(result),
            )

        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["compatible"], 1)
        self.assertEqual(summary["ineligible"], 1)
        self.assertEqual(len(progress), 2)

    async def test_unreadable_posting_is_manual_review_not_compatible(self):
        jobs = {"https://example.com/job": dict(JOB)}
        with (
            patch("cli.collect_jobs.BrowserSession") as browser_cls,
            patch("cli.collect_jobs.clear_stale_browser_session_state"),
            patch(
                "cli.collect_jobs.fetch_description_for_job",
                new=AsyncMock(side_effect=RuntimeError("access denied")),
            ),
            patch("cli.collect_jobs.update_job") as update_job,
            patch("cli.collect_jobs.asyncio.sleep", new=AsyncMock()),
        ):
            browser = browser_cls.return_value
            browser.start = AsyncMock()
            browser.close = AsyncMock()
            browser.new_page = AsyncMock(return_value=MagicMock())
            summary = await screen_jobs_individually(jobs, PROFILE_NEEDS_SPONSORSHIP)

        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["fetch_failed"], 1)
        self.assertEqual(update_job.call_args.kwargs["status"], "manual_review")
        self.assertEqual(update_job.call_args.kwargs["screening_status"], "fetch_failed")


if __name__ == "__main__":
    unittest.main()
