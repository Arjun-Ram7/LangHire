# Workday deterministic routing — design

Date: 2026-08-08

## Problem

The app's main Apply automation (`cli/apply_jobs.py:apply_to_job`, invoked by
`backend/main.py`'s `/jobs/apply` endpoint via `apply_jobs.worker`) always
drives the browser with a vision-based LLM agent (`browser_use.Agent`), one
LLM call per page action. Workday is the most common ATS the user applies
through, and its multi-page wizard (My Info, My Experience, Application
Questions, Voluntary Disclosures, Self-Identify, Review) is structurally
consistent across employers. Driving it with a full vision agent is slower
and more expensive than necessary.

A separate, uncommitted body of work already exists that fills and clicks
through this exact wizard without any LLM:

- `backend/core/autofill_facts.py`: `run_static_autofill` (deterministic
  per-page field filling), `try_safe_progress_step` (clicks
  Next/Continue/Save-and-Continue/Sign-In/Create-Account submit),
  `probe_workday_human_checkpoint` / `wait_for_workday_human_checkpoint`
  (pauses and asks a human to click protected Create Account / Sign In
  buttons Workday requires a trusted click for), `try_controlled_final_submit`
  (gated final-submit click), `upload_resume_to_file_inputs`.
- `cli/manual_review_queue.py`: a standalone CLI script that already
  orchestrates the above into a full per-job loop (`open_for_manual_review`)
  with a bounded Gemini/browser-use fallback (`_run_llm_cleanup`, capped at
  35 steps / 300s) for fields the static facts file can't answer, plus
  summarization/blocker helpers (`_summarize_review`, `_needs_llm_cleanup`,
  `_can_run_llm_cleanup`, `_surface_is_external_application`,
  `_safe_submit_summary_blockers`).
- `scripts/live-workday-smoke.py`: a no-LLM smoke test against a live Workday
  posting.

None of this is wired into the flow the UI's Automation dialog actually
triggers. That flow always builds and runs the big `Agent` in
`apply_to_job`, for every ATS including Workday.

## Goal

When `apply_to_job` reaches a Workday-hosted application page, skip the
vision agent entirely and drive the form with the deterministic engine
instead, falling back to a bounded per-question LLM assist only when static
facts can't answer something, and always stopping before the final Submit
click so the user reviews and submits themselves.

## Non-goals

- No change to non-Workday ATS behavior (Lever, Greenhouse, Ashby, etc.)
  — those keep using the existing vision agent.
- No automatic final submission for Workday jobs in this flow (confirmed
  with user: stop at Review, human clicks Submit).
- No UI changes — `manual_review` status already renders correctly
  (blue badge, "Review & Apply" tab) and needs no schema change.
- No change to `cli/manual_review_queue.py`'s own CLI behavior or flags.

## Design

### 1. Extract the shared engine

`cli/manual_review_queue.py` currently owns the orchestration logic
(`_static_fill_passes`, `_summarize_review`, `_needs_llm_cleanup`,
`_can_run_llm_cleanup`, `_surface_is_external_application`,
`_safe_submit_summary_blockers`, `_run_llm_cleanup`, `_submission_risk_scan`)
that `cli/apply_jobs.py` now also needs. `manual_review_queue.py` already
imports from `apply_jobs.py` (`_page_url`, `_pause_for_workday_human_click`,
`_run_apply_preflight`, `_wait_for_page_settle`), so `apply_jobs.py` cannot
import back from it without a cycle.

Move those functions into a new module, `backend/core/workday_flow.py`.
`manual_review_queue.py` imports them from there instead of defining them;
`apply_jobs.py` imports the same module. This is a pure move (no behavior
change) — it fixes the layering problem blocking reuse, which is exactly the
kind of pre-existing tangle the brainstorming skill says to clean up when it
sits in the way of the current work.

`_run_llm_cleanup` builds its own bounded `browser_use.Agent` internally
(scoped task, `max_steps`, `timeout`) — this becomes the "AI pass for just
the one unanswered question" fallback. It moves as-is.

### 2. Detect Workday and route

Add `is_workday_url(url: str) -> bool` to `backend/core/workday_flow.py`
(same host check already used in `live-workday-smoke.py`:
`myworkdayjobs.com`, `myworkdaysite.com`, `workday.com` suffixes).

In `apply_to_job`, after `_run_apply_preflight` and the existing
pre-agent Workday human-checkpoint handling (both already navigate to the
real application surface before any agent is built), read the current page
URL and check `is_workday_url`. If true, call a new
`run_workday_deterministic(browser, facts=static_facts, resume_path=...,
worker_id=..., dry_run=..., allow_final_submit=...)` in
`backend/core/workday_flow.py` instead of constructing/running the
`browser_use.Agent`. If false, fall through to today's agent path
unchanged.

### 3. `run_workday_deterministic` behavior

Built from `open_for_manual_review`'s body, trimmed to what `apply_to_job`
needs (it doesn't re-claim the job or manage tabs — `apply_to_job` already
does that):

1. Run `_static_fill_passes` with `passes=8` (same default the CLI uses) —
   each pass: wait for a visible surface, `run_static_autofill`, handle a
   Workday human checkpoint inline via the existing
   `_pause_for_workday_human_click` if one appears, `try_safe_progress_step`
   to click the page forward.
2. Summarize with `_summarize_review` / `_needs_llm_cleanup`. If cleanup is
   needed and allowed (`_can_run_llm_cleanup`), run `_run_llm_cleanup`
   (35 steps / 300s bound, same as CLI default) so the LLM only has to
   resolve the specific stuck field(s), then let the deterministic loop
   continue via the cleanup's own step callback (already re-runs
   `run_static_autofill` after each cleanup step).
3. Never call `try_controlled_final_submit` — this path always stops short
   of the final click, regardless of `allow_final_submit`/dry_run flags.
4. Return a result dict: `{"status": "manual_review", "summary": ..., "blockers": [...]}`.

`apply_to_job` takes that result and calls the existing `save_job_status(url,
"manual_review", error=<blocker summary text>)`, then returns
`"manual_review"` — reusing the status the frontend already fully supports.

### 4. Safety fallback

If `run_workday_deterministic` raises before completing at least one
successful `run_static_autofill` pass (e.g., the page redirected somewhere
that isn't actually the Workday wizard), catch the exception in
`apply_to_job`, log it, and fall through to building the existing vision
`Agent` as today — so a Workday host detection false-positive doesn't strand
the job with no automation at all.

### 5. Testing

- Unit tests for `is_workday_url` (table of hosts: `*.myworkdayjobs.com`,
  `*.myworkdaysite.com`, `*.workday.com`, and negative cases like
  `linkedin.com`, a bare `workday` substring in an unrelated domain).
- Move the existing `_needs_llm_cleanup`/summarize-related tests (if any)
  along with the functions; add coverage for the new
  `run_workday_deterministic` result shape using mocks in the same style as
  `WorkdayHumanCheckpointWaitTests` (`AsyncMock` patches on
  `backend.core.workday_flow.run_static_autofill`,
  `try_safe_progress_step`, `_run_llm_cleanup`), since a real browser-driven
  integration test isn't practical in CI.
- Manual verification: run `scripts/live-workday-smoke.py` against a live
  Workday posting (unchanged), then a live dry-run through the Automation
  dialog against a Workday job to confirm it lands in `manual_review` with
  fields filled and stops before Submit.

## Open questions resolved during brainstorming

- Unanswerable custom question -> bounded LLM fallback for that question,
  not a full manual-review handoff and not a silent skip. (existing
  `_run_llm_cleanup`, reused)
- Wizard scope -> drive every page automatically, stop at Review, never
  auto-click final Submit.
- Integration target -> wire into the main Apply flow (auto-detected by
  URL), not left as a separate manual CLI tool.
