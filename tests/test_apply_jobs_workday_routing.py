import unittest
from unittest.mock import AsyncMock, patch

from cli.apply_jobs import _maybe_apply_via_workday_engine
from backend.core.workday_flow import WorkdayDeterministicUnavailable


class MaybeApplyViaWorkdayEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_workday_url_returns_none(self):
        with patch("cli.apply_jobs._page_url", new=AsyncMock(return_value="https://boards.greenhouse.io/acme/jobs/1")):
            result = await _maybe_apply_via_workday_engine(
                browser=object(), facts={}, resume_path="/tmp/r.pdf", worker_id=1,
            )
        self.assertIsNone(result)

    async def test_workday_url_returns_deterministic_result(self):
        with (
            patch("cli.apply_jobs._page_url", new=AsyncMock(return_value="https://acme.wd1.myworkdayjobs.com/apply")),
            patch(
                "cli.apply_jobs.run_workday_deterministic",
                new=AsyncMock(return_value={
                    "status": "manual_review",
                    "summary": {},
                    "blockers": ["ready for final review; static engine stopped before Submit"],
                }),
            ) as engine,
        ):
            result = await _maybe_apply_via_workday_engine(
                browser=object(), facts={"job_title": "SWE"}, resume_path="/tmp/r.pdf", worker_id=1,
            )
        engine.assert_awaited_once()
        self.assertEqual(result["status"], "manual_review")

    async def test_engine_unavailable_returns_none_for_agent_fallback(self):
        with (
            patch("cli.apply_jobs._page_url", new=AsyncMock(return_value="https://acme.wd1.myworkdayjobs.com/apply")),
            patch(
                "cli.apply_jobs.run_workday_deterministic",
                new=AsyncMock(side_effect=WorkdayDeterministicUnavailable("cdp session lost")),
            ),
        ):
            result = await _maybe_apply_via_workday_engine(
                browser=object(), facts={}, resume_path="/tmp/r.pdf", worker_id=1,
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
