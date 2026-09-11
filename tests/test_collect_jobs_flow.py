import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cli.collect_jobs import collect_for_title


PROFILE = {
    "country": "US",
    "target_locations": [],
    "work_authorization": "F-1 CPT/OPT work authorization",
    "visa_sponsorship_needed": True,
    "future_sponsorship_needed": True,
}


class CollectForTitleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_ineligible_result_and_counts_compatible_job(self):
        cards = [
            {
                "id": "4290000001",
                "url": "https://www.linkedin.com/jobs/view/4290000001/",
                "title": "Software Engineer Intern",
                "company": "Restricted Co",
                "location": "Austin, TX",
            },
            {
                "id": "4290000002",
                "url": "https://www.linkedin.com/jobs/view/4290000002/",
                "title": "Backend Software Engineer Intern",
                "company": "Sponsor Co",
                "location": "Boston, MA",
            },
        ]
        details = [
            ({
                **cards[0],
                "has_job_root": True,
                "description": "Candidates must not now or in the future require visa sponsorship. " * 5,
            }, ""),
            ({
                **cards[1],
                "has_job_root": True,
                "description": "We welcome F-1 OPT candidates and can sponsor H-1B visas. " * 5,
            }, ""),
        ]

        with (
            patch("cli.collect_jobs.BrowserSession") as browser_cls,
            patch("cli.collect_jobs.clear_stale_browser_session_state"),
            patch("cli.collect_jobs.refresh_credentials"),
            patch("cli.collect_jobs._wait_for_linkedin_login", new=AsyncMock(return_value=True)),
            patch("cli.collect_jobs._wait_for_ready", new=AsyncMock(return_value={"text": "jobs"})),
            patch("cli.collect_jobs._extract_cards", new=AsyncMock(return_value=cards)),
            patch("cli.collect_jobs._wait_for_expected_job", new=AsyncMock(side_effect=details)),
            patch("cli.collect_jobs.asyncio.sleep", new=AsyncMock()),
            patch("cli.collect_jobs.atomic_upsert_job") as atomic_upsert,
        ):
            browser = browser_cls.return_value
            browser.start = AsyncMock()
            browser.close = AsyncMock()
            page = MagicMock()
            page.evaluate = AsyncMock(return_value=json.dumps({"ok": True}))
            page.get_url = AsyncMock(return_value="https://www.linkedin.com/jobs/search/")
            browser.new_page = AsyncMock(return_value=page)
            atomic_upsert.side_effect = lambda url, fields: (dict(fields), True)

            found = await collect_for_title(
                "Software Engineer", {}, PROFILE, max_jobs=1
            )

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["company"], "Sponsor Co")
        self.assertEqual(found[0]["status"], "pending")
        self.assertEqual(found[0]["screening_status"], "compatible")
        self.assertEqual(atomic_upsert.call_count, 1)
