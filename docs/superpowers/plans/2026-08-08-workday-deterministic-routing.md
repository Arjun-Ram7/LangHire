# Workday Deterministic Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Workday application pages through the existing deterministic (no-LLM) autofill/click engine instead of the vision-based `browser_use.Agent`, with a bounded per-question LLM fallback, and never auto-click final Submit.

**Architecture:** Extract the engine currently trapped inside `cli/manual_review_queue.py` into a new shared module `backend/core/workday_flow.py` (breaks a circular-import blocker), add `is_workday_url` + `run_workday_deterministic` to it, then call that from `cli/apply_jobs.py:apply_to_job` when the current page is a Workday host, falling back to the existing vision agent only if the deterministic engine fails before making any progress.

**Tech Stack:** Python 3, `browser_use` (Agent/BrowserSession), `unittest` + `playwright.sync_api` (existing test style in `tests/test_autofill_facts.py`).

## Global Constraints

- Never call `try_controlled_final_submit` from the new Workday routing path — this feature always stops before the final Submit click (confirmed with user).
- Reuse the existing `manual_review` job status — no `JobStatus` type or UI changes (already fully supported: blue badge, "Review & Apply" tab).
- Default `passes=8`, `llm_steps=35`, `llm_timeout=300.0` — match `cli/manual_review_queue.py`'s existing CLI defaults exactly.
- Non-Workday ATS behavior in `apply_to_job` must not change.
- Follow the existing `try: from core.X import ...; except ImportError: from backend.core.X import ...` dual-import pattern used throughout this codebase (frozen-app vs. dev-mode path resolution) for any new cross-module import.

---

### Task 1: Create `backend/core/workday_flow.py` by moving the engine out of `manual_review_queue.py`

**Files:**
- Create: `backend/core/workday_flow.py`
- Modify: `cli/manual_review_queue.py` (remove moved definitions, add imports)
- Modify: `cli/apply_jobs.py` (remove moved definitions, add imports)
- Modify: `scripts/live-workday-smoke.py` (update imports)
- Test: `tests/test_workday_flow.py` (new)

**Interfaces:**
- Produces (from `backend/core/workday_flow.py`, all signatures unchanged from their current locations unless noted):
  - `async def _current_page(browser) -> Page`
  - `async def _page_url(browser: BrowserSession) -> str`
  - `async def _wait_for_page_settle(browser: BrowserSession, seconds: float = 1.5) -> str`
  - `def _human_click_timeout_seconds() -> float`
  - `async def _pause_for_workday_human_click(browser: BrowserSession, worker_id: int, initial: dict | None = None) -> dict`
  - `_GEMINI_LOOP_REPEAT_LIMIT: int`, `_LOOP_STOP_MARKERS: tuple`
  - `def _looks_like_loop_stop(*messages: object) -> bool`
  - `def _agent_action_signature(agent_output: object) -> str`
  - `def _summarize_review(review: dict) -> dict`
  - `def _needs_llm_cleanup(summary: dict) -> bool`
  - `def _surface_is_external_application(summary: dict) -> bool`
  - `def _can_run_llm_cleanup(summary: dict, preflight: dict) -> bool`
  - `def _safe_submit_summary_blockers(summary: dict) -> list[str]`
  - `async def _submission_risk_scan(browser: BrowserSession) -> dict`
  - `async def _maybe_safe_submit(browser, *, facts, resume_path, summary, enabled) -> dict`
  - `def _facts_for_cleanup_prompt(facts: dict) -> dict`
  - `async def _probe_visible_surface(browser: BrowserSession) -> dict`
  - `async def _wait_for_visible_surface(browser: BrowserSession, timeout: float = 14.0) -> dict`
  - `async def _static_fill_passes(browser, facts, resume_path, passes, worker_id) -> dict`
  - `async def _run_llm_cleanup(browser, *, facts, profile, resume_path, title, company, worker_id, max_steps, timeout, cancel_flag=None) -> dict`

This task is a pure move — no behavior changes. Verify by running the existing Workday test suite before and after and diffing output (should be identical pass/fail).

- [ ] **Step 1: Copy the block verbatim into the new file**

Create `backend/core/workday_flow.py` with this header, then paste the functions listed above in the interfaces section, moved verbatim (byte-for-byte body, only touching import lines) from their current locations:
- From `cli/apply_jobs.py`: `_current_page` (lines 205-212), `_page_url` (215-226), `_wait_for_page_settle` (262-277), `_human_click_timeout_seconds` (130-134), `_pause_for_workday_human_click` (137-171).
- From `cli/manual_review_queue.py`: everything from `_GEMINI_LOOP_REPEAT_LIMIT` through the end of `_run_llm_cleanup` (lines 84-849), **except** `_clear_automation_session_restore` and `_interest_statement` (those stay in `manual_review_queue.py` — CLI-only / already duplicated in `apply_jobs.py`).

File header:

```python
"""Deterministic (no-LLM) Workday application engine.

Fills known fields, clicks through the multi-page wizard, and falls back to
a bounded vision-agent cleanup only for fields static facts can't answer.
Shared by cli/apply_jobs.py (main Apply flow) and cli/manual_review_queue.py
(standalone review-queue CLI) — it lives in backend/core so both callers can
import it without a circular dependency between them.
"""

from __future__ import annotations

import asyncio
import json
import os

from browser_use import Agent, BrowserSession

try:
    import core.shared_config as config
    from core.shared_config import LOGS_DIR
    from core.autofill_facts import (
        format_facts_for_prompt,
        probe_workday_human_checkpoint,
        run_static_autofill,
        try_controlled_final_submit,
        try_safe_progress_step,
        wait_for_workday_human_checkpoint,
    )
except ImportError:
    import backend.core.shared_config as config
    from backend.core.shared_config import LOGS_DIR
    from backend.core.autofill_facts import (
        format_facts_for_prompt,
        probe_workday_human_checkpoint,
        run_static_autofill,
        try_controlled_final_submit,
        try_safe_progress_step,
        wait_for_workday_human_checkpoint,
    )
```

Order the moved functions exactly as they appear in the interfaces list above (dependency order: page/timing helpers, then loop-detection helpers, then summarize/blocker helpers, then the two big orchestration functions `_static_fill_passes` and `_run_llm_cleanup` last).

- [ ] **Step 2: Remove the moved definitions from `cli/apply_jobs.py` and import them instead**

In `cli/apply_jobs.py`, delete the bodies of `_current_page`, `_page_url`, `_wait_for_page_settle`, `_human_click_timeout_seconds`, `_pause_for_workday_human_click` (they now live in `workday_flow.py`). Add to both branches of the existing try/except import block (around line 35-64):

```python
    from core.workday_flow import (
        _current_page,
        _human_click_timeout_seconds,
        _page_url,
        _pause_for_workday_human_click,
        _wait_for_page_settle,
    )
```
and the `backend.core.workday_flow` equivalent in the `except ImportError` branch. Leave every call site (`_page_url(browser)`, `_wait_for_page_settle(...)`, `_pause_for_workday_human_click(...)`) untouched — same names, now imported instead of defined locally.

- [ ] **Step 3: Remove the moved definitions from `cli/manual_review_queue.py` and import them instead**

Delete the moved function/constant bodies from `cli/manual_review_queue.py` (everything listed in Step 1's second bullet). Change the existing import block:

```python
from cli.apply_jobs import (
    _page_url,
    _pause_for_workday_human_click,
    _run_apply_preflight,
    _wait_for_page_settle,
)
```

to:

```python
from cli.apply_jobs import (
    _page_url,
    _run_apply_preflight,
)
try:
    from core.workday_flow import (
        _GEMINI_LOOP_REPEAT_LIMIT,
        _agent_action_signature,
        _can_run_llm_cleanup,
        _facts_for_cleanup_prompt,
        _looks_like_loop_stop,
        _maybe_safe_submit,
        _needs_llm_cleanup,
        _pause_for_workday_human_click,
        _probe_visible_surface,
        _run_llm_cleanup,
        _safe_submit_summary_blockers,
        _static_fill_passes,
        _submission_risk_scan,
        _summarize_review,
        _surface_is_external_application,
        _wait_for_page_settle,
        _wait_for_visible_surface,
    )
except ImportError:
    from backend.core.workday_flow import (
        _GEMINI_LOOP_REPEAT_LIMIT,
        _agent_action_signature,
        _can_run_llm_cleanup,
        _facts_for_cleanup_prompt,
        _looks_like_loop_stop,
        _maybe_safe_submit,
        _needs_llm_cleanup,
        _pause_for_workday_human_click,
        _probe_visible_surface,
        _run_llm_cleanup,
        _safe_submit_summary_blockers,
        _static_fill_passes,
        _submission_risk_scan,
        _summarize_review,
        _surface_is_external_application,
        _wait_for_page_settle,
        _wait_for_visible_surface,
    )
```

Only keep names in this import list that Step 1 actually confirmed are referenced elsewhere in `manual_review_queue.py` outside the moved block (check with `grep -n` for each name before finalizing — e.g. `_GEMINI_LOOP_REPEAT_LIMIT`/`_looks_like_loop_stop`/`_agent_action_signature` may only be used inside `_run_llm_cleanup` itself, in which case they don't need re-importing into `manual_review_queue.py` at all; drop any that come back unused).

- [ ] **Step 4: Update `scripts/live-workday-smoke.py` imports**

Change:
```python
from cli.apply_jobs import _page_url, _pause_for_workday_human_click, _wait_for_page_settle
from cli.manual_review_queue import _summarize_review, _wait_for_visible_surface
```
to:
```python
from cli.apply_jobs import _page_url
try:
    from core.workday_flow import _pause_for_workday_human_click, _summarize_review, _wait_for_page_settle, _wait_for_visible_surface
except ImportError:
    from backend.core.workday_flow import _pause_for_workday_human_click, _summarize_review, _wait_for_page_settle, _wait_for_visible_surface
```

- [ ] **Step 5: Verify nothing broke**

Run:
```bash
python -c "import backend.core.workday_flow"
python -c "import cli.apply_jobs"
python -c "import cli.manual_review_queue"
python -c "import scripts.live_workday_smoke" 2>/dev/null || python -m py_compile scripts/live-workday-smoke.py
uv run pytest tests/test_autofill_facts.py -q
```
Expected: all imports succeed, existing test suite still passes (unchanged pass count/names — this step introduced no logic changes).

- [ ] **Step 6: Commit**

```bash
git add backend/core/workday_flow.py cli/apply_jobs.py cli/manual_review_queue.py scripts/live-workday-smoke.py
git commit -m "refactor: extract Workday deterministic engine into backend/core/workday_flow.py"
```

---

### Task 2: Add `is_workday_url` and test it

**Files:**
- Modify: `backend/core/workday_flow.py`
- Test: `tests/test_workday_flow.py` (new)

**Interfaces:**
- Consumes: nothing new (uses stdlib `urllib.parse.urlparse`)
- Produces: `def is_workday_url(url: str) -> bool` — importable from `backend.core.workday_flow` (and `core.workday_flow` in the try/except dev-path style, but as a plain function this only needs one definition; callers use the dual-import pattern, not the function itself)

- [ ] **Step 1: Write the failing test**

Create `tests/test_workday_flow.py`:

```python
import unittest

from backend.core.workday_flow import is_workday_url


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workday_flow.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_workday_url'`

- [ ] **Step 3: Implement `is_workday_url`**

Add to `backend/core/workday_flow.py` (near the top, after imports, `from urllib.parse import urlparse` added to the import block):

```python
def is_workday_url(url: str) -> bool:
    """True if url is hosted on a Workday-operated career site domain."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return (
        host.endswith("myworkdayjobs.com")
        or host.endswith("myworkdaysite.com")
        or host.endswith("workday.com")
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_workday_flow.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/core/workday_flow.py tests/test_workday_flow.py
git commit -m "feat: add is_workday_url host detection"
```

---

### Task 3: Add `run_workday_deterministic` orchestration function

**Files:**
- Modify: `backend/core/workday_flow.py`
- Test: `tests/test_workday_flow.py`

**Interfaces:**
- Consumes: `_static_fill_passes`, `_summarize_review`, `_needs_llm_cleanup`, `_can_run_llm_cleanup`, `_run_llm_cleanup` (all from Task 1, same module)
- Produces:
  - `class WorkdayDeterministicUnavailable(Exception)` — raised only when the engine fails before completing a single `_static_fill_passes` call; callers should fall back to the vision agent.
  - `async def run_workday_deterministic(browser: BrowserSession, *, facts: dict, resume_path: str, worker_id: int, passes: int = 8, llm_cleanup: bool = True, llm_steps: int = 35, llm_timeout: float = 300.0) -> dict` — returns `{"status": "manual_review", "summary": dict, "blockers": list[str]}`. Never raises once `_static_fill_passes` has returned successfully at least once.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workday_flow.py`:

```python
from unittest.mock import AsyncMock, patch

from backend.core.workday_flow import WorkdayDeterministicUnavailable, run_workday_deterministic


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
                new=AsyncMock(return_value={"attempted": True, "steps": 4, "timeout": False, "success": True, "stop_reason": "", "last_review": cleaned_review}),
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_workday_flow.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_workday_deterministic'`

- [ ] **Step 3: Implement `run_workday_deterministic`**

Add to `backend/core/workday_flow.py` (after the moved functions):

```python
class WorkdayDeterministicUnavailable(Exception):
    """Raised when the deterministic engine fails before completing any pass.

    Callers should fall back to the vision agent when they see this; any
    later failure is folded into the returned manual_review result instead.
    """


def _workday_blockers(summary: dict) -> list[str]:
    blockers: list[str] = []
    if summary.get("verification_code_required"):
        blockers.append("verification/OTP code required")
    if summary.get("credential_error"):
        blockers.append("credential error visible")
    if summary.get("required_empty"):
        blockers.append(f"{summary['required_empty']} required blank(s)")
    if summary.get("invalid_fields"):
        blockers.append(f"{summary['invalid_fields']} invalid field(s)")
    if summary.get("visible_errors"):
        blockers.append("visible validation error")
    if summary.get("needs_llm"):
        blockers.append("fields still need manual judgment")
    if not blockers:
        blockers.append("ready for final review; static engine stopped before Submit")
    return blockers


async def run_workday_deterministic(
    browser: BrowserSession,
    *,
    facts: dict,
    resume_path: str,
    worker_id: int,
    passes: int = 8,
    llm_cleanup: bool = True,
    llm_steps: int = 35,
    llm_timeout: float = 300.0,
) -> dict:
    """Fill and click through a Workday application without a vision agent.

    Never calls try_controlled_final_submit — this path always stops before
    the final Submit click so the user reviews and submits themselves.
    """
    review: dict = {}
    progress_made = False
    try:
        review = await _static_fill_passes(browser, facts, resume_path, passes, worker_id)
        progress_made = True

        summary_before_cleanup = _summarize_review(review)
        needs_cleanup = _needs_llm_cleanup(summary_before_cleanup)
        can_cleanup = _can_run_llm_cleanup(summary_before_cleanup, preflight={})
        if llm_cleanup and needs_cleanup and can_cleanup:
            cleanup = await _run_llm_cleanup(
                browser,
                facts=facts,
                profile={},
                resume_path=resume_path,
                title=facts.get("job_title", ""),
                company=facts.get("job_company", ""),
                worker_id=worker_id,
                max_steps=llm_steps,
                timeout=llm_timeout,
            )
            if cleanup.get("last_review"):
                review = cleanup["last_review"]
            review["llm_cleanup"] = {
                "attempted": bool(cleanup.get("attempted")),
                "steps": int(cleanup.get("steps") or 0),
                "timeout": bool(cleanup.get("timeout")),
                "success": bool(cleanup.get("success")),
                "stop_reason": cleanup.get("stop_reason", ""),
            }
    except Exception as exc:
        if not progress_made:
            raise WorkdayDeterministicUnavailable(f"{type(exc).__name__}: {exc}") from exc
        summary = _summarize_review(review)
        return {
            "status": "manual_review",
            "summary": summary,
            "blockers": [f"deterministic engine error: {type(exc).__name__}: {exc}"],
        }

    summary = _summarize_review(review)
    return {"status": "manual_review", "summary": summary, "blockers": _workday_blockers(summary)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_workday_flow.py -v`
Expected: PASS (10 tests total: 6 from Task 2 + 4 from this task)

- [ ] **Step 5: Commit**

```bash
git add backend/core/workday_flow.py tests/test_workday_flow.py
git commit -m "feat: add run_workday_deterministic orchestration"
```

---

### Task 4: Wire routing into `apply_to_job`

**Files:**
- Modify: `cli/apply_jobs.py`
- Test: `tests/test_apply_jobs_workday_routing.py` (new)

**Interfaces:**
- Consumes: `is_workday_url`, `run_workday_deterministic`, `WorkdayDeterministicUnavailable` from `backend.core.workday_flow` (Tasks 2-3); existing `_page_url`, `save_job_status` in `cli/apply_jobs.py`.
- Produces: no new public interface — modifies `apply_to_job`'s control flow only.

`apply_to_job` currently runs preflight, handles the pre-agent Workday human checkpoint (`cli/apply_jobs.py` around line 732-751), then unconditionally builds the big `Agent` and calls `agent.run()` (around line 1185-1256). Insert the routing decision right after the pre-agent checkpoint block and before `agent = Agent(...)` is built: if `is_workday_url(await _page_url(browser))`, attempt `run_workday_deterministic` and return its result directly, skipping `Agent` construction entirely; only fall through to the existing `Agent` path if `WorkdayDeterministicUnavailable` is raised.

- [ ] **Step 1: Write the failing test**

Create `tests/test_apply_jobs_workday_routing.py`. This test exercises the routing decision only — not the full `apply_to_job` function (which needs a live browser/LLM/job store) — so it targets a small helper extracted for this purpose (see Step 3):

```python
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
                new=AsyncMock(return_value={"status": "manual_review", "summary": {}, "blockers": ["ready for final review; static engine stopped before Submit"]}),
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_apply_jobs_workday_routing.py -v`
Expected: FAIL with `ImportError: cannot import name '_maybe_apply_via_workday_engine'`

- [ ] **Step 3: Implement the routing helper and call it from `apply_to_job`**

Add the import (both branches of the existing try/except block in `cli/apply_jobs.py`, alongside the other `core.autofill_facts`/`core.workday_flow` imports added in Task 1):

```python
    from core.workday_flow import WorkdayDeterministicUnavailable, is_workday_url, run_workday_deterministic
```
(and the `backend.core.workday_flow` equivalent in the except branch).

Add this function near `_pause_for_workday_human_click` (both now live in the same module after Task 1's import, so keep this one local to `apply_jobs.py` since it's a routing decision specific to `apply_to_job`, not shared engine logic):

```python
async def _maybe_apply_via_workday_engine(
    browser: BrowserSession,
    facts: dict,
    resume_path: str,
    worker_id: int,
) -> dict | None:
    """Try the deterministic Workday engine; return None to fall back to the agent."""
    current_url = await _page_url(browser)
    if not is_workday_url(current_url):
        return None
    try:
        return await run_workday_deterministic(
            browser,
            facts=facts,
            resume_path=resume_path,
            worker_id=worker_id,
        )
    except WorkdayDeterministicUnavailable as exc:
        print(f"  ⚠️  [W{worker_id}] Workday deterministic engine unavailable ({exc}); falling back to agent")
        return None
```

In `apply_to_job`, immediately after the existing pre-agent Workday human-checkpoint block finishes (right before `agent = Agent(` is constructed, i.e. right before the `if dry_run: run_mode_instructions = ...` block that precedes it), add:

```python
    workday_result = await _maybe_apply_via_workday_engine(browser, static_facts, resume_path, worker_id)
    if workday_result is not None:
        blockers = ", ".join(workday_result.get("blockers") or [])
        await save_job_status(url, workday_result["status"], error=f"Manual review needed: {blockers}" if blockers else None)
        print(f"  🧭 [W{worker_id}] Workday deterministic engine: {title} at {company} — {blockers or 'ready for review'}")
        return workday_result["status"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_apply_jobs_workday_routing.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: all tests pass (no regressions in `test_autofill_facts.py` or `test_workday_flow.py`)

- [ ] **Step 6: Commit**

```bash
git add cli/apply_jobs.py tests/test_apply_jobs_workday_routing.py
git commit -m "feat: route Workday applications through the deterministic engine"
```

---

### Task 5: Manual smoke verification

**Files:** none (verification only)

- [ ] **Step 1: Static smoke test (no LLM, no job store writes)**

Run against a real, currently-open Workday posting URL:
```bash
uv run python scripts/live-workday-smoke.py --url "<a live myworkdayjobs.com posting URL>" --passes 8
```
Expected: `WORKDAY_STAGE` log lines show fields getting filled and safe-progress clicks advancing pages, and the script never reports a submit action (it doesn't call submit at all).

- [ ] **Step 2: End-to-end dry run through the app**

Start the backend (`uv run python backend/main.py` or via the desktop app), trigger Apply on a real Workday job from the Automation dialog with dry-run/no-final-submit settings, and confirm:
- The job ends in `manual_review` status (blue badge in the "Review & Apply" tab).
- The browser tab is left on the Workday Review page (or wherever it stalled) with fields filled, not on a Submit confirmation page.
- No `browser_use.Agent` vision-agent log lines appear in the backend log for that job (only `run_static_autofill`/`try_safe_progress_step`/deterministic-engine lines), confirming the vision agent was skipped.

- [ ] **Step 3: Record results**

Note the outcome (pass/fail + any surprises) in the PR description or final report — this task has no automated test, it's the acceptance check for the whole feature.
