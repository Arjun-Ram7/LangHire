import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cli.collect_jobs import _title_matches_speedyapply_position, collect_speedyapply

PROFILE = {"country": "US", "visa_sponsorship_needed": True}


class TitleMatchesSpeedyapplyPositionTests(unittest.TestCase):
    def test_matches_verbose_real_world_position_text(self):
        # SpeedyApply's real listings are verbose and never contain the
        # target title as a literal substring.
        self.assertTrue(
            _title_matches_speedyapply_position(
                "Software Engineer Intern",
                "Software Engineer: Cloud & Distributed Backend Intern Opportunities for University Students - Redmond",
            )
        )

    def test_engineering_and_engineer_are_equivalent(self):
        self.assertTrue(
            _title_matches_speedyapply_position(
                "Software Engineering Intern", "Software Engineer Intern"
            )
        )

    def test_matches_ml_titled_position(self):
        self.assertTrue(
            _title_matches_speedyapply_position(
                "Machine Learning Intern",
                "Machine Learning Engineer Intern - Applied AI Team",
            )
        )

    def test_keeps_short_but_meaningful_acronym(self):
        # A 2-letter token like "ai" must still count, but only as a whole
        # word -- not as a substring inside unrelated words like "email".
        self.assertTrue(
            _title_matches_speedyapply_position(
                "AI Engineer Intern",
                "AI/ML Engineer Intern - Applied Research",
            )
        )
        self.assertFalse(
            _title_matches_speedyapply_position(
                "AI Engineer Intern",
                "Engineer Intern - email support and training tools",
            )
        )

    def test_rejects_unrelated_discipline(self):
        self.assertFalse(
            _title_matches_speedyapply_position(
                "Software Engineer Intern",
                "Mechanical Engineer Intern - Manufacturing",
            )
        )

    def test_empty_title_matches_everything(self):
        self.assertTrue(_title_matches_speedyapply_position("", "Any Position At All"))


class CollectSpeedyapplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_saves_matching_row_and_skips_non_matching(self):
        rows = [
            {
                "company": "Microsoft",
                "position": "Software Engineer: Cloud Intern Opportunities for University Students",
                "location": "Redmond, WA",
                "url": "https://apply.careers.microsoft.com/careers/job/1",
            },
            {
                "company": "Acme Manufacturing",
                "position": "Mechanical Engineer Intern",
                "location": "Detroit, MI",
                "url": "https://acme.example.com/jobs/2",
            },
        ]
        with (
            patch("cli.collect_jobs.BrowserSession") as MockBrowser,
            patch("cli.collect_jobs.clear_stale_browser_session_state"),
            patch("cli.collect_jobs.read_jobs", return_value={}),
            patch("cli.collect_jobs.atomic_upsert_job") as mock_upsert,
        ):
            mock_upsert.side_effect = lambda url, job: (job, True)
            browser_instance = MockBrowser.return_value
            browser_instance.start = AsyncMock()
            browser_instance.close = AsyncMock()
            page = MagicMock()
            page.goto = AsyncMock()
            page.evaluate = AsyncMock(return_value=__import__("json").dumps(rows))
            browser_instance.new_page = AsyncMock(return_value=page)

            with patch("cli.collect_jobs._wait_for_ready", new=AsyncMock(return_value={})):
                found = await collect_speedyapply("Software Engineer Intern", {}, PROFILE, max_jobs=10)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["company"], "Microsoft")
        self.assertEqual(found[0]["status"], "manual_review")
        self.assertEqual(found[0]["screening_status"], "pending")
        self.assertEqual(mock_upsert.call_count, 1)
        self.assertEqual(mock_upsert.call_args.args[0], "https://apply.careers.microsoft.com/careers/job/1")

    async def test_does_not_resave_already_collected_url(self):
        rows = [
            {
                "company": "Microsoft",
                "position": "Software Engineer Intern Opportunities",
                "location": "Redmond, WA",
                "url": "https://apply.careers.microsoft.com/careers/job/1",
            },
        ]
        existing = {
            "https://apply.careers.microsoft.com/careers/job/1": {"status": "pending"},
        }
        with (
            patch("cli.collect_jobs.BrowserSession") as MockBrowser,
            patch("cli.collect_jobs.clear_stale_browser_session_state"),
            patch("cli.collect_jobs.read_jobs", return_value=dict(existing)),
            patch("cli.collect_jobs.atomic_upsert_job") as mock_upsert,
        ):
            browser_instance = MockBrowser.return_value
            browser_instance.start = AsyncMock()
            browser_instance.close = AsyncMock()
            page = MagicMock()
            page.goto = AsyncMock()
            page.evaluate = AsyncMock(return_value=__import__("json").dumps(rows))
            browser_instance.new_page = AsyncMock(return_value=page)

            with patch("cli.collect_jobs._wait_for_ready", new=AsyncMock(return_value={})):
                found = await collect_speedyapply("Software Engineer Intern", existing, PROFILE, max_jobs=10)

        self.assertEqual(found, [])
        mock_upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
