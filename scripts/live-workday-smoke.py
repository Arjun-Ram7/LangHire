"""Run deterministic, fill-only smoke testing against one Workday posting.

The runner never releases the final-submit guard and never invokes an LLM. It
uses LangHire's persistent automation browser profile, fills known facts,
advances through safe non-final controls, and prints a sanitized report for
each page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import BrowserSession

from backend.core.autofill_facts import (
    load_autofill_facts,
    run_static_autofill,
    try_safe_progress_step,
)
from backend.core.config import load_profile
from backend.core.shared_config import RESUME_PATH, browser_session_kwargs, validate_job_url
from backend.core.workday_flow import (
    _pause_for_workday_human_click,
    _summarize_review,
    _wait_for_page_settle,
    _wait_for_visible_surface,
    is_workday_url,
)
from cli.apply_jobs import _page_url


async def run(url: str, passes: int, hold_seconds: int, cdp_url: str | None = None) -> None:
    if not validate_job_url(url) or not is_workday_url(url):
        raise SystemExit("URL must be a public Workday or myworkdayjobs.com posting")

    profile = load_profile()
    facts = load_autofill_facts(profile, RESUME_PATH)
    facts.setdefault("job_title", "Software Engineering Intern")
    facts.setdefault("job_company", "Workday smoke test")

    browser = (
        BrowserSession(cdp_url=cdp_url, keep_alive=True)
        if cdp_url
        else BrowserSession(**browser_session_kwargs(), keep_alive=True)
    )
    print("WORKDAY_STAGE browser_start", flush=True)
    await asyncio.wait_for(browser.start(), timeout=25.0)
    print("WORKDAY_STAGE browser_ready", flush=True)
    current_url = await _page_url(browser)
    print(
        "WORKDAY_STAGE current_page "
        + json.dumps({"url": current_url, "attached": bool(cdp_url)}, ensure_ascii=True),
        flush=True,
    )
    if not cdp_url or current_url.rstrip("/") != url.rstrip("/"):
        print("WORKDAY_STAGE navigate", flush=True)
        await browser.navigate_to(url, new_tab=not bool(cdp_url))
    await _wait_for_page_settle(browser, 3.0)

    reports: list[dict] = []
    previous_stall: tuple | None = None
    for pass_number in range(1, max(1, passes) + 1):
        print(f"WORKDAY_STAGE pass_{pass_number}_surface", flush=True)
        surface = await _wait_for_visible_surface(browser, timeout=10.0)
        print(f"WORKDAY_STAGE pass_{pass_number}_autofill", flush=True)
        review = await asyncio.wait_for(
            run_static_autofill(
                browser,
                facts,
                resume_path=RESUME_PATH,
                guard_final_submit=True,
            ),
            timeout=35.0,
        )
        checkpoint_timed_out = False
        while (review.get("human_checkpoint") or {}).get("required"):
            print(
                "WORKDAY_HUMAN_CHECKPOINT "
                + json.dumps(review["human_checkpoint"], ensure_ascii=True),
                flush=True,
            )
            checkpoint_result = await _pause_for_workday_human_click(
                browser,
                1,
                review["human_checkpoint"],
            )
            if checkpoint_result.get("timed_out"):
                checkpoint_timed_out = True
                break
            review = await asyncio.wait_for(
                run_static_autofill(
                    browser,
                    facts,
                    resume_path=RESUME_PATH,
                    guard_final_submit=True,
                ),
                timeout=35.0,
            )
        review["surface"] = surface
        summary = _summarize_review(review)
        report = {
            "pass": pass_number,
            "url": await _page_url(browser),
            "summary": summary,
        }
        reports.append(report)
        print("WORKDAY_PASS " + json.dumps(report, ensure_ascii=True), flush=True)
        print(
            "WORKDAY_BRIEF "
            + json.dumps(
                {
                    "pass": pass_number,
                    "url": report["url"],
                    "title": surface.get("title"),
                    "required_empty": summary.get("required_empty"),
                    "invalid_fields": summary.get("invalid_fields"),
                    "required_empty_labels": summary.get("required_empty_labels"),
                    "visible_errors": summary.get("visible_errors"),
                    "verification_code_required": summary.get("verification_code_required"),
                },
                ensure_ascii=True,
            ),
            flush=True,
        )

        if checkpoint_timed_out:
            print("WORKDAY_STOP human_click_checkpoint_timed_out", flush=True)
            break

        if summary["verification_code_required"] or summary["credential_error"]:
            print("WORKDAY_STOP verification_or_credentials_required", flush=True)
            break

        stall = (
            report["url"],
            tuple(summary.get("visible_errors") or []),
            tuple(summary.get("required_empty_labels") or []),
            summary.get("filled", 0),
            summary.get("choices", 0),
        )
        if stall == previous_stall and (stall[1] or stall[2]):
            print("WORKDAY_STOP repeated_unresolved_state", flush=True)
            break
        previous_stall = stall

        print(f"WORKDAY_STAGE pass_{pass_number}_progress", flush=True)
        progress = await asyncio.wait_for(try_safe_progress_step(browser), timeout=12.0)
        print("WORKDAY_PROGRESS " + json.dumps(progress, ensure_ascii=True), flush=True)
        print(
            "WORKDAY_PROGRESS_BRIEF "
            + json.dumps(
                {
                    key: progress.get(key)
                    for key in (
                        "clicked",
                        "reason",
                        "label",
                        "finalish",
                        "trusted_click",
                        "trusted_action",
                        "trusted_target",
                    )
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
        if not progress.get("clicked"):
            if progress.get("reason") in {
                "account_fields_empty_before_create",
                "account_fields_empty_before_continue",
            }:
                await _wait_for_page_settle(browser, 2.0)
                continue
            if progress.get("reason") == "no_safe_progress_control" and (
                not surface.get("title")
                or surface.get("loadingish")
                or int(surface.get("input_count") or 0) == 0
                or int(surface.get("body_length") or 0) < 300
            ):
                print("WORKDAY_PROGRESS_RETRY waiting_for_interactive_surface", flush=True)
                await asyncio.sleep(2.0)
                continue
            break
        await _wait_for_page_settle(browser, 3.0)

    final_report = {
        "passes": len(reports),
        "final_url": await _page_url(browser),
        "final": reports[-1]["summary"] if reports else {},
        "final_submit_enabled": False,
        "browser_left_open": True,
    }
    print("WORKDAY_FINAL " + json.dumps(final_report, ensure_ascii=True), flush=True)
    print(
        "WORKDAY_FINAL_BRIEF "
        + json.dumps(
            {
                "passes": final_report["passes"],
                "final_url": final_report["final_url"],
                "required_empty_labels": final_report["final"].get("required_empty_labels"),
                "visible_errors": final_report["final"].get("visible_errors"),
                "final_submit_enabled": False,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    if hold_seconds > 0:
        print(f"WORKDAY_MONITOR holding browser open for {hold_seconds}s", flush=True)
        await asyncio.sleep(hold_seconds)
    print("WORKDAY_STAGE monitor_disconnect", flush=True)
    try:
        await asyncio.wait_for(browser.stop(), timeout=12.0)
    except Exception as exc:
        print(f"WORKDAY_MONITOR_DISCONNECT_WARNING {type(exc).__name__}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill-only deterministic Workday smoke test")
    parser.add_argument("url", help="Public Workday job-posting URL")
    parser.add_argument("--passes", type=int, default=12, help="Maximum safe page/progress passes")
    parser.add_argument("--hold-seconds", type=int, default=300, help="Keep the review browser open after the run")
    parser.add_argument("--cdp-url", help="Attach to an existing Desktop 2 automation browser")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.passes, args.hold_seconds, args.cdp_url))


if __name__ == "__main__":
    main()
