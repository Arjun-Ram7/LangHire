import unittest
from unittest.mock import AsyncMock, patch

from cli.collect_jobs import collect_speedyapply
from cli.speedyapply import parse_readme

PROFILE = {"country": "US", "visa_sponsorship_needed": True}
README = '''
### FAANG+
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://company.example"><strong>Big &amp; Co</strong></a> | Firmware Intern | Austin, TX | $50/hr | <a href="https://apply.example/1?a=1&amp;b=2"><img src="badge.png" alt="Apply"/></a> | 1d |
### Quant
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| Quant | Software Intern | NYC | <a href="https://apply.example/quant">Apply</a> | 1d |
### Other
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <strong>Acme</strong> | Developer Co-Op | Remote - USA | <a href="https://apply.example/2"><img alt="Apply"/></a> | 2d |
| Closed | Software Intern | Boston, MA | 🔒 | 3d |
'''


class ParseReadmeTests(unittest.TestCase):
    def test_extracts_posting_links_from_both_table_shapes(self):
        rows = parse_readme(README)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["url"], "https://apply.example/1?a=1&b=2")
        self.assertEqual(rows[0]["company"], "Big & Co")
        self.assertEqual(rows[0]["salary"], "$50/hr")
        self.assertEqual(rows[1]["salary"], "")
        self.assertEqual([r["source_section"] for r in rows], ["FAANG+", "Other"])
        self.assertEqual(rows[1]["posting_age"], "2d")

    def test_missing_section_fails_instead_of_silently_importing_nothing(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            parse_readme("GitHub error page")
        with self.assertRaisesRegex(ValueError, "missing"):
            parse_readme(README.split("### Other")[0])

    def test_escaped_pipe_does_not_shift_apply_column(self):
        rows = parse_readme(README.replace("Developer Co-Op", r"Developer \| Co-Op"))
        self.assertEqual(rows[1]["position"], "Developer | Co-Op")
        self.assertEqual(rows[1]["url"], "https://apply.example/2")

    def test_invalid_application_url_fails(self):
        with self.assertRaisesRegex(ValueError, "Invalid"):
            parse_readme(README.replace("https://apply.example/2", "javascript:alert(1)"))


class CollectSpeedyapplyTests(unittest.IsolatedAsyncioTestCase):
    async def run_collector(self, *, existing=None, max_jobs=0, cancelled=False):
        rows = parse_readme(README)
        with (
            patch("cli.collect_jobs.fetch_speedyapply_rows", new=AsyncMock(return_value=rows + rows)) as fetch,
            patch("cli.collect_jobs.BrowserSession") as browser,
            patch("cli.collect_jobs.read_jobs", return_value=existing or {}),
            patch("cli.collect_jobs.atomic_upsert_job", side_effect=lambda url, job: (job, True)) as upsert,
        ):
            found = await collect_speedyapply(
                "Software Engineer Intern", existing or {}, PROFILE,
                max_jobs=max_jobs, cancel_flag={"cancel_requested": cancelled}, run_id="test-run",
            )
            browser.assert_not_called()
            return found, upsert.call_count, fetch.call_count

    async def test_collects_all_roles_once_without_title_filter_or_browser(self):
        found, writes, _ = await self.run_collector()
        self.assertEqual(writes, 2)
        self.assertEqual([j["title"] for j in found], ["Firmware Intern", "Developer Co-Op"])
        self.assertEqual(found[0]["collection_run_id"], "test-run")
        self.assertEqual(found[0]["status"], "manual_review")
        self.assertEqual(found[0]["screening_status"], "pending")
        self.assertEqual(found[1]["source_section"], "Other")

    async def test_preserves_existing_jobs_and_limits_only_new_jobs(self):
        existing = {"https://apply.example/1?a=1&b=2": {"status": "applied"}}
        found, writes, _ = await self.run_collector(existing=existing, max_jobs=1)
        self.assertEqual(writes, 1)
        self.assertEqual(found[0]["url"], "https://apply.example/2")
        self.assertEqual(existing["https://apply.example/1?a=1&b=2"]["status"], "applied")

    async def test_honors_explicit_cap(self):
        found, writes, _ = await self.run_collector(max_jobs=1)
        self.assertEqual(len(found), 1)
        self.assertEqual(writes, 1)

    async def test_cancelled_run_does_not_fetch_or_write(self):
        found, writes, fetches = await self.run_collector(cancelled=True)
        self.assertEqual((found, writes, fetches), ([], 0, 0))


if __name__ == "__main__":
    unittest.main()
