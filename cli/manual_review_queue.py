"""Open a queue of applications, autofill static facts, and leave tabs open.

By default this is intentionally not an autonomous submitter. It opens each
pending job in its own tab, clicks through to the employer application surface
when possible, runs deterministic autofill/upload, installs the final-submit
guard, gives bounded Gemini cleanup (including login/account flows) a chance,
then records a manual_review status and moves on. Repeated Gemini page states
are a deterministic stop boundary: the current tab remains open and the next
job is opened in a new tab. The optional
--allow-safe-submit flag can release exactly one final submit only after strict
validation and risk checks pass.

Usage:
  uv run python cli/manual_review_queue.py --limit 5
  uv run python cli/manual_review_queue.py --limit 5 --easy-apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import Agent, BrowserSession

try:
    import core.shared_config as config
    from core.shared_config import (
        BROWSER_PROFILE_DIR,
        CANDIDATE_PROFILE,
        JOBS_FILE,
        LOGS_DIR,
        RESUME_PATH,
        claim_job,
        load_json,
        refresh_credentials,
        update_job,
    )
    from core.autofill_facts import load_autofill_facts
    from core.workday_flow import (
        _can_run_llm_cleanup,
        _maybe_safe_submit,
        _needs_llm_cleanup,
        _run_llm_cleanup,
        _static_fill_passes,
        _summarize_review,
        _wait_for_page_settle,
    )
except ImportError:
    import backend.core.shared_config as config
    from backend.core.shared_config import (
        BROWSER_PROFILE_DIR,
        CANDIDATE_PROFILE,
        JOBS_FILE,
        LOGS_DIR,
        RESUME_PATH,
        claim_job,
        load_json,
        refresh_credentials,
        update_job,
    )
    from backend.core.autofill_facts import load_autofill_facts
    from backend.core.workday_flow import (
        _can_run_llm_cleanup,
        _maybe_safe_submit,
        _needs_llm_cleanup,
        _run_llm_cleanup,
        _static_fill_passes,
        _summarize_review,
        _wait_for_page_settle,
    )

from cli.apply_jobs import (
    _page_url,
    _run_apply_preflight,
)



def _clear_automation_session_restore() -> int:
    """Remove restored tabs from the LangHire automation profile, not cookies."""
    removed = 0
    sessions_dir = Path(BROWSER_PROFILE_DIR) / "Default" / "Sessions"
    for pattern in ("Session_*", "Tabs_*"):
        for path in sessions_dir.glob(pattern):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        path = Path(BROWSER_PROFILE_DIR) / "Default" / name
        if path.exists():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _interest_statement(title: str, company: str) -> str:
    company = (company or "your team").strip()
    title = (title or "this role").strip()
    return (
        f"I am excited about {company} because the {title} role is a strong fit "
        "for my computer science background, prior internship experience, and "
        "interest in building reliable software with real user impact."
    )


async def open_for_manual_review(
    browser: BrowserSession,
    job: dict,
    profile: dict,
    worker_id: int,
    easy_apply: bool,
    passes: int,
    llm_cleanup: bool,
    llm_steps: int,
    llm_timeout: float,
    allow_safe_submit: bool,
    cancel_flag: dict | None = None,
) -> str:
    url = job["url"]
    title = job.get("title") or "Unknown"
    company = job.get("company") or "Unknown"
    resume_path = RESUME_PATH
    facts = load_autofill_facts(profile, resume_path)
    facts["job_title"] = title
    facts["job_company"] = company
    facts.setdefault("interest_statement", _interest_statement(title, company))

    if not claim_job(url):
        print(f"  ⏭️  [W{worker_id}] Skip claimed/non-pending: {title} at {company}")
        return "skipped"

    print(f"  🧭 [W{worker_id}] Manual-review open: {title} at {company}")
    status = "manual_review"
    error = None
    preflight: dict = {}
    review: dict = {}
    current_url = ""
    try:
        preflight = await _run_apply_preflight(
            browser,
            url=url,
            title=title,
            company=company,
            easy_apply=easy_apply,
            static_facts=facts,
            resume_path=resume_path,
            guard_final_submit=True,
            worker_id=worker_id,
            close_existing_tabs=False,
            open_in_new_tab=True,
        )
        await _wait_for_page_settle(browser, 1.0)
        review = await _static_fill_passes(browser, facts, resume_path, passes, worker_id)
        summary_before_llm = _summarize_review(review)
        needs_cleanup = _needs_llm_cleanup(summary_before_llm) or not preflight.get("clicked_linkedin")
        can_cleanup = _can_run_llm_cleanup(summary_before_llm, preflight)
        if llm_cleanup and needs_cleanup and can_cleanup:
            cleanup = await _run_llm_cleanup(
                browser,
                facts=facts,
                profile=profile,
                resume_path=resume_path,
                title=title,
                company=company,
                worker_id=worker_id,
                max_steps=llm_steps,
                timeout=llm_timeout,
                cancel_flag=cancel_flag,
            )
            if cleanup.get("last_review"):
                review = cleanup["last_review"]
            review["llm_cleanup"] = {
                "attempted": bool(cleanup.get("attempted")),
                "steps": int(cleanup.get("steps") or 0),
                "timeout": bool(cleanup.get("timeout")),
                "success": bool(cleanup.get("success")),
                "loop_detected": bool(cleanup.get("loop_detected")),
                "stop_reason": cleanup.get("stop_reason", ""),
                "errors": cleanup.get("errors") or [],
            }
            print(
                f"  🤖 [W{worker_id}] Gemini cleanup done: "
                f"steps={review['llm_cleanup']['steps']} "
                f"timeout={review['llm_cleanup']['timeout']} "
                f"reason={review['llm_cleanup']['stop_reason'] or 'done'}"
            )
            if review["llm_cleanup"]["loop_detected"]:
                print(f"  🔁 [W{worker_id}] Loop boundary reached; preserving this tab and opening the next job.")
        elif llm_cleanup and needs_cleanup and not can_cleanup:
            review["llm_cleanup"] = {
                "attempted": False,
                "stop_reason": "not on external application surface; left tab for manual review",
            }
        summary = _summarize_review(review)
        safe_submit = await _maybe_safe_submit(
            browser,
            facts=facts,
            resume_path=resume_path,
            summary=summary,
            enabled=allow_safe_submit,
        )
        review["safe_submit"] = safe_submit
        if safe_submit.get("submitted"):
            await _wait_for_page_settle(browser, 2.0)
            current_url = await _page_url(browser)
            summary = _summarize_review(review)
            update_job(
                url,
                status="applied",
                error=None,
                applied_at=datetime.now(timezone.utc).isoformat(),
                manual_review_at=datetime.now(timezone.utc).isoformat(),
                manual_review_url=current_url,
                manual_review_summary=summary,
                manual_review_notes=(preflight.get("notes") or [])[-12:],
            )
            print(
                f"  🚀 [W{worker_id}] Safe-submitted: {title} at {company} | "
                f"button={safe_submit.get('label') or 'final submit'}"
            )
            return "applied"

        current_url = await _page_url(browser)
        summary = _summarize_review(review)
        notes = preflight.get("notes") or []
        blocker_bits = []
        if summary["verification_code_required"]:
            blocker_bits.append("verification code")
        if summary["credential_error"]:
            blocker_bits.append("credential error")
        if summary["required_empty"]:
            blocker_bits.append(f"{summary['required_empty']} required blank(s)")
        if summary["invalid_fields"]:
            blocker_bits.append(f"{summary['invalid_fields']} invalid field(s)")
        if summary["visible_errors"]:
            blocker_bits.append("visible validation error")
        surface = summary.get("surface") or {}
        if (
            not summary["filled"]
            and not summary["restored"]
            and not summary["selects"]
            and not summary["choices"]
            and not summary["uploads"]
            and not summary.get("static_values_present")
            and int(surface.get("input_count") or 0) > 0
        ):
            blocker_bits.append("visible form/login fields were not autofilled")
        if blocker_bits:
            error = "Manual review needed: " + ", ".join(blocker_bits)
        elif allow_safe_submit and safe_submit.get("enabled") and not safe_submit.get("submitted"):
            safe_blockers = ", ".join(str(x) for x in (safe_submit.get("blockers") or [])[:4])
            error = (
                "Manual review needed: safe-submit "
                f"{safe_submit.get('reason') or 'blocked'}"
                + (f" ({safe_blockers})" if safe_blockers else "")
            )
        elif not preflight.get("clicked_linkedin"):
            error = "Manual review needed: LinkedIn apply button was not clicked automatically"
        else:
            error = "Manual review tab left open; finish/submit manually"

        update_job(
            url,
            status=status,
            error=error,
            manual_review_at=datetime.now(timezone.utc).isoformat(),
            manual_review_url=current_url,
            manual_review_summary=summary,
            manual_review_notes=notes[-12:],
        )
        print(
            f"  ✅ [W{worker_id}] Left open for review: {title} at {company} | "
            f"filled={summary['filled']} restored={summary['restored']} "
            f"selects={summary['selects']} choices={summary['choices']} uploads={summary['uploads']} "
            f"progress={sum(1 for x in summary['safe_progress'] if x.get('clicked'))} "
            f"required={summary['required_empty']} errors={len(summary['visible_errors'])}"
        )
        return status
    except Exception as exc:
        current_url = current_url or await _page_url(browser)
        error = f"Manual review setup error: {type(exc).__name__}: {str(exc)[:400]}"
        update_job(
            url,
            status="manual_review",
            error=error,
            manual_review_at=datetime.now(timezone.utc).isoformat(),
            manual_review_url=current_url,
            manual_review_summary=_summarize_review(review or {}),
            manual_review_notes=(preflight.get("notes") or [])[-12:] if isinstance(preflight, dict) else [],
        )
        print(f"  ⚠️  [W{worker_id}] Left tab after setup error: {title} at {company} — {error[:120]}")
        return "manual_review"


def _select_jobs(
    limit: int,
    easy_apply: bool,
    prefer_startups: bool,
    company_contains: str = "",
    title_contains: str = "",
    url_contains: str = "",
) -> list[dict]:
    jobs = load_json(JOBS_FILE, {})
    company_contains = company_contains.lower().strip()
    title_contains = title_contains.lower().strip()
    url_contains = url_contains.lower().strip()
    pending = [
        job for job in jobs.values()
        if job.get("status") == "pending"
        and ((job.get("easy_apply") is True) == easy_apply)
        and (not company_contains or company_contains in (job.get("company") or "").lower())
        and (not title_contains or title_contains in (job.get("title") or "").lower())
        and (not url_contains or url_contains in (job.get("url") or "").lower())
    ]
    if prefer_startups:
        startup_words = ("yc", "crustdata", "mindfort", "betterbasket", "naïve", "naive", "traceroot", "novaflow", "bluejay")
        pending.sort(
            key=lambda job: (
                0 if any(word in (job.get("company") or "").lower() for word in startup_words) else 1,
                job.get("company") or "",
                job.get("title") or "",
            )
        )
    return pending[:limit]


async def run_review_queue(
    selected: list[dict],
    profile: dict,
    *,
    cancel_flag: dict | None = None,
    easy_apply: bool | None = None,
    passes: int = 5,
    llm_cleanup: bool = True,
    llm_steps: int = 18,
    llm_timeout: float = 150.0,
    allow_safe_submit: bool = False,
    clear_session_restore: bool = True,
) -> dict[str, int]:
    """Prepare jobs sequentially, preserving every review/loop tab.

    This is the shared engine used by both the desktop API and the CLI. It is
    intentionally single-worker: multiple agents fighting over one persistent
    login profile is slower and less reliable than deterministic sequencing.
    """
    if clear_session_restore:
        removed = _clear_automation_session_restore()
        if removed:
            print(f"Cleared {removed} stale automation session-restore file(s).")

    browser = BrowserSession(**config.browser_session_kwargs(), keep_alive=True)
    stats: dict[str, int] = {}
    await browser.start()
    print(f"Preparing {len(selected)} job(s); review and stuck tabs will remain open.\n")
    for idx, job in enumerate(selected, start=1):
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print("  🛑 Review queue stop requested — no more jobs will be opened")
            break
        job_easy_apply = easy_apply if easy_apply is not None else bool(job.get("easy_apply"))
        status = await open_for_manual_review(
            browser,
            job,
            profile,
            worker_id=idx,
            easy_apply=job_easy_apply,
            passes=passes,
            llm_cleanup=llm_cleanup,
            llm_steps=llm_steps,
            llm_timeout=llm_timeout,
            allow_safe_submit=allow_safe_submit,
            cancel_flag=cancel_flag,
        )
        stats[status] = stats.get(status, 0) + 1
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print("  🛑 Review queue stopped after preserving the current tab")
            break
        await asyncio.sleep(0.5)
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description="Open jobs for manual review after static autofill")
    parser.add_argument("--limit", type=int, default=5, help="Number of jobs/tabs to open")
    parser.add_argument("--easy-apply", action="store_true", help="Use LinkedIn Easy Apply jobs instead of non-Easy")
    parser.add_argument("--passes", type=int, default=8, help="Static autofill/progress passes per tab")
    parser.add_argument("--no-prefer-startups", action="store_true", help="Do not prioritize YC/startup-style jobs")
    parser.add_argument("--company-contains", default="", help="Only open pending jobs whose company contains this text")
    parser.add_argument("--title-contains", default="", help="Only open pending jobs whose title contains this text")
    parser.add_argument("--url-contains", default="", help="Only open pending jobs whose URL contains this text")
    parser.add_argument("--no-llm-cleanup", action="store_true", help="Skip bounded Gemini cleanup after static autofill stalls")
    parser.add_argument("--llm-steps", type=int, default=35, help="Max Gemini cleanup steps per job")
    parser.add_argument("--llm-timeout", type=float, default=300.0, help="Max Gemini cleanup seconds per job")
    parser.add_argument(
        "--allow-safe-submit",
        action="store_true",
        help="Opt in to strict controlled final submit when all validation/risk checks pass",
    )
    args = parser.parse_args()

    refresh_credentials()
    profile = load_json(CANDIDATE_PROFILE, {})
    selected = _select_jobs(
        args.limit,
        args.easy_apply,
        prefer_startups=not args.no_prefer_startups,
        company_contains=args.company_contains,
        title_contains=args.title_contains,
        url_contains=args.url_contains,
    )
    if not selected:
        print("No pending jobs matched the manual-review queue filters.")
        return

    stats = await run_review_queue(
        selected,
        profile,
        easy_apply=args.easy_apply,
        passes=args.passes,
        llm_cleanup=not args.no_llm_cleanup,
        llm_steps=args.llm_steps,
        llm_timeout=args.llm_timeout,
        allow_safe_submit=args.allow_safe_submit,
    )
    print("\nManual review queue complete.")
    print(f"Results: {stats}")
    print("Browser tabs are intentionally left open for you to finish manually.")


if __name__ == "__main__":
    asyncio.run(main())
