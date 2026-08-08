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
    from core.autofill_facts import (
        format_facts_for_prompt,
        load_autofill_facts,
        run_static_autofill,
        try_controlled_final_submit,
        try_safe_progress_step,
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
    from backend.core.autofill_facts import (
        format_facts_for_prompt,
        load_autofill_facts,
        run_static_autofill,
        try_controlled_final_submit,
        try_safe_progress_step,
    )

from cli.apply_jobs import (
    _page_url,
    _pause_for_workday_human_click,
    _run_apply_preflight,
    _wait_for_page_settle,
)


# A click followed by a couple of waits can legitimately leave the DOM
# unchanged while a cross-site application opens. Six identical post-step
# states gives Gemini five real recovery attempts before deterministic handoff.
_GEMINI_LOOP_REPEAT_LIMIT = 6
_LOOP_STOP_MARKERS = (
    "loop detected",
    "loop_detection",
    "repeating the same action",
    "same action repeated",
    "same blank/error state repeated",
    "stuck in a loop",
    "stuck in loop",
    "repeated action",
)


def _looks_like_loop_stop(*messages: object) -> bool:
    text = " ".join(str(message or "").lower() for message in messages)
    return any(marker in text for marker in _LOOP_STOP_MARKERS)


def _agent_action_signature(agent_output: object) -> str:
    """Describe action shape without retaining typed text or other secrets."""
    actions = getattr(agent_output, "action", None) or []
    safe_actions: list[dict] = []
    for action in actions:
        try:
            dumped = action.model_dump(exclude_none=True)
        except Exception:
            dumped = {"type": type(action).__name__}
        for name, params in dumped.items():
            if not isinstance(params, dict):
                safe_actions.append({str(name): {}})
                continue
            safe_actions.append({
                str(name): {
                    key: value
                    for key, value in params.items()
                    if key not in {"text", "value", "password", "content"}
                }
            })
    return json.dumps(safe_actions, sort_keys=True, default=str)


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


def _summarize_review(review: dict) -> dict:
    upload = review.get("resume_upload") or {}
    guard = review.get("submit_guard") or {}
    checkpoint = review.get("human_checkpoint") or {}
    checkpoint_result = review.get("human_checkpoint_result") or {}
    surface = review.get("surface") or {}
    debug_inputs = (review.get("debugInputs") or [])[:16]
    static_values_present = any(
        item.get("picked") and item.get("has_value")
        for item in debug_inputs
        if isinstance(item, dict)
    )
    return {
        "url": review.get("url", ""),
        "error": review.get("error", ""),
        "surface": surface,
        "filled": int(review.get("filled") or 0),
        "restored": int(review.get("restored") or 0),
        "selects": int(review.get("selects") or 0),
        "choices": int(review.get("choices") or 0),
        "uploads": int(upload.get("uploaded") or 0) if isinstance(upload, dict) else 0,
        "required_empty": int(review.get("requiredEmpty") or 0),
        "invalid_fields": int(review.get("invalidFields") or 0),
        "verification_code_required": bool(review.get("verificationCodeRequired")),
        "credential_error": bool(review.get("credentialError")),
        "needs_llm": (review.get("needsLlm") or [])[:8],
        "required_empty_labels": (review.get("requiredEmptyLabels") or [])[:8],
        "visible_errors": (review.get("visibleErrors") or [])[:8],
        "matches": (review.get("matches") or [])[:24],
        "final_submit_guarded": bool(isinstance(guard, dict) and (guard.get("disabled") or guard.get("blocked"))),
        "human_checkpoint": {
            "required": bool(checkpoint.get("required")),
            "action": checkpoint.get("action", ""),
            "reason": checkpoint.get("reason", ""),
            "timed_out": bool(checkpoint_result.get("timed_out")),
        },
        "safe_progress": (review.get("safe_progress") or [])[-8:],
        "llm_cleanup": review.get("llm_cleanup") or {},
        "safe_submit": review.get("safe_submit") or {},
        "debug_inputs": debug_inputs,
        "static_values_present": static_values_present,
    }


def _needs_llm_cleanup(summary: dict) -> bool:
    if summary.get("verification_code_required") or summary.get("credential_error"):
        return False
    if summary.get("required_empty") or summary.get("invalid_fields"):
        return True
    if summary.get("needs_llm") or summary.get("visible_errors"):
        return True
    if summary.get("final_submit_guarded"):
        return False
    surface = summary.get("surface") or {}
    if surface.get("accountish") or surface.get("formish"):
        return True
    safe = summary.get("safe_progress") or []
    if safe and (safe[-1].get("reason") or "").endswith("repeated"):
        return True
    if safe and safe[-1].get("reason") in {"no_safe_progress_control", "loop_signature_repeated"}:
        return True
    return False


def _surface_is_external_application(summary: dict) -> bool:
    surface = summary.get("surface") or {}
    url = str(summary.get("url") or surface.get("url") or "").lower()
    if not url or "linkedin.com" in url or "job-apply-resources" in url:
        return False
    if surface.get("accountish") or surface.get("formish"):
        return True
    return any(
        marker in url
        for marker in (
            "greenhouse.io", "lever.co", "workday", "successfactors", "ashbyhq",
            "jobvite", "icims", "smartrecruiters", "oraclecloud", "applicantstack",
            "boards.greenhouse", "/apply", "jobs."
        )
    )


def _can_run_llm_cleanup(summary: dict, preflight: dict) -> bool:
    """Allow Gemini on an ATS surface or as the promised LinkedIn fallback."""
    if _surface_is_external_application(summary):
        return True
    surface = summary.get("surface") or {}
    url = str(summary.get("url") or surface.get("url") or "").lower()
    if "linkedin.com" not in url:
        return bool(surface.get("accountish"))
    # Preflight deliberately avoids risky/ambiguous controls. When it cannot
    # reach Apply, vision-based Gemini must get a bounded attempt instead of us
    # silently leaving the job on LinkedIn.
    return not bool(preflight.get("clicked_linkedin")) or bool(surface.get("accountish"))


def _safe_submit_summary_blockers(summary: dict) -> list[str]:
    """Conservative pre-submit rules. Anything ambiguous stays manual."""
    blockers: list[str] = []
    if not _surface_is_external_application(summary):
        blockers.append("not on an external employer application surface")
    if summary.get("verification_code_required"):
        blockers.append("verification/OTP code required")
    if summary.get("credential_error"):
        blockers.append("credential error visible")
    if summary.get("required_empty"):
        blockers.append(f"{summary.get('required_empty')} required blank(s)")
    if summary.get("invalid_fields"):
        blockers.append(f"{summary.get('invalid_fields')} invalid field(s)")
    if summary.get("required_empty_labels"):
        blockers.append("required labels still blank")
    if summary.get("visible_errors"):
        blockers.append("visible validation error")
    if summary.get("needs_llm"):
        blockers.append("fields still need LLM/manual judgment")
    if not summary.get("final_submit_guarded"):
        blockers.append("final submit control was not guarded/detected")

    cleanup = summary.get("llm_cleanup") or {}
    if cleanup.get("timeout"):
        blockers.append("LLM cleanup timed out")
    stop_reason = str(cleanup.get("stop_reason") or "").lower()
    hard_stop_words = (
        "repeated", "stuck", "timeout", "timed out", "credential",
        "verification", "otp", "error", "unavailable",
    )
    if cleanup.get("attempted") and any(word in stop_reason for word in hard_stop_words):
        blockers.append(f"LLM cleanup stopped unsafely: {cleanup.get('stop_reason')}")

    if (
        not summary.get("filled")
        and not summary.get("restored")
        and not summary.get("selects")
        and not summary.get("choices")
        and not summary.get("uploads")
        and not summary.get("static_values_present")
    ):
        blockers.append("no static autofill activity recorded")
    return blockers[:12]


async def _submission_risk_scan(browser: BrowserSession) -> dict:
    """Scan visible page text for cases we should always leave manual."""
    try:
        page = await browser.get_current_page()
        if page is None:
            return {"flags": ["no_current_page"], "url": ""}
        raw = await page.evaluate(
            r"""() => {
              const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
              const text = norm(document.body?.innerText || '');
              const url = location.href;
              const patterns = [
                ['human_verification', ['captcha', 'recaptcha', 'hcaptcha', 'verification code', 'one-time code', 'one time code', 'otp', 'two-factor', 'two factor', '2fa', 'verify your email', 'confirm your email']],
                ['legal_attestation', ['i certify', 'i attest', 'under penalty', 'electronic signature', 'type your name as your signature', 'background check', 'drug test', 'social security', 'ssn']],
                ['assessment_or_video', ['take assessment', 'complete assessment', 'assessment test', 'video interview', 'record a video', 'work sample']],
                ['sensitive_manual_review', ['security clearance', 'government clearance', 'export control', 'export-controlled', 'non-compete']]
              ];
              const flags = [];
              for (const [name, words] of patterns) {
                if (words.some((word) => text.includes(word))) flags.push(name);
              }
              const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden'
                  && style.display !== 'none'
                  && Number(style.opacity || 1) > 0
                  && rect.width > 2
                  && rect.height > 2;
              };
              const finalish = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a, [role="button"]'))
                .filter(visible)
                .map((el) => norm([el.innerText, el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title')].filter(Boolean).join(' ')))
                .filter((label) => /\b(submit|send|finish|complete|apply)\b/.test(label))
                .slice(0, 8);
              return {url, flags: [...new Set(flags)], finalish, bodySample: text.slice(0, 300)};
            }"""
        )
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw) if raw else {"flags": ["empty_risk_scan"]}
        except (TypeError, json.JSONDecodeError):
            return {"flags": ["invalid_risk_scan"]}
    except Exception as exc:
        return {"flags": [f"risk_scan_failed:{type(exc).__name__}"], "error": str(exc)[:200]}


async def _maybe_safe_submit(
    browser: BrowserSession,
    *,
    facts: dict,
    resume_path: str,
    summary: dict,
    enabled: bool,
) -> dict:
    if not enabled:
        return {"enabled": False, "attempted": False, "submitted": False, "reason": "disabled"}

    blockers = _safe_submit_summary_blockers(summary)
    if blockers:
        return {
            "enabled": True,
            "attempted": False,
            "submitted": False,
            "reason": "policy_blocked",
            "blockers": blockers,
        }

    risk = await _submission_risk_scan(browser)
    flags = risk.get("flags") or []
    if flags:
        return {
            "enabled": True,
            "attempted": False,
            "submitted": False,
            "reason": "risk_scan_blocked",
            "blockers": flags[:8],
            "risk": risk,
        }

    result = await try_controlled_final_submit(browser, facts, resume_path=resume_path)
    return {
        "enabled": True,
        "attempted": True,
        "submitted": bool(result.get("submitted")),
        "reason": result.get("reason", ""),
        "label": result.get("label", ""),
        "blockers": (result.get("blockers") or [])[:8],
        "risk": risk,
        "review_summary": _summarize_review(result.get("review") or {}),
    }


def _facts_for_cleanup_prompt(facts: dict) -> dict:
    """Give the agent stable facts without URL-looking values that trigger auto-navigation."""
    keep = [
        "full_name", "first_name", "last_name", "current_location", "school", "degree", "major", "gpa",
        "graduation", "education_start_date", "education_end_date",
        "prior_internships", "age_over_18", "work_authorization", "authorized_to_work_us",
        "visa_sponsorship_needed", "future_sponsorship_needed", "h1b_sponsorship_needed",
        "visa_status", "cpt_status", "opt_status", "willing_to_relocate",
        "veteran_status", "disability_status", "gender", "race_ethnicity",
        "hispanic_latino", "driver_license", "accommodations_needed",
        "desired_pay", "job_title", "job_company", "interest_statement",
    ]
    return {key: facts[key] for key in keep if facts.get(key)}


async def _probe_visible_surface(browser: BrowserSession) -> dict:
    page = await browser.get_current_page()
    if page is None:
        return {"url": "", "ready": "", "input_count": 0, "button_count": 0, "body_length": 0}
    raw = await page.evaluate(
        r"""() => {
          const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
          const visible = (el) => {
            if (!el) return false;
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden'
              && style.display !== 'none'
              && Number(style.opacity || 1) > 0
              && rect.width > 2
              && rect.height > 2;
          };
          const body = document.body?.innerText || '';
          const text = norm(body);
          const inputs = Array.from(document.querySelectorAll('input, textarea, select')).filter(visible);
          const buttons = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a, [role="button"]')).filter(visible);
          return {
            url: location.href,
            ready: document.readyState,
            input_count: inputs.length,
            button_count: buttons.length,
            body_length: body.length,
            accountish: /sign in|log in|create account|register|password|not a registered user/i.test(body),
            formish: inputs.length > 0 || /resume|upload|application|candidate|work authorization|sponsorship/i.test(body),
            loadingish: /loading|please wait/i.test(text) && inputs.length === 0,
            title: document.title
          };
        }"""
    )
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {}


async def _wait_for_visible_surface(browser: BrowserSession, timeout: float = 14.0) -> dict:
    """Wait for the current tab to show real login/form controls, not a redirect shell."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict = {}
    reloaded_shell = False
    while asyncio.get_event_loop().time() < deadline:
        last = await _probe_visible_surface(browser)
        ready = str(last.get("ready") or "").lower()
        body_length = int(last.get("body_length") or 0)
        input_count = int(last.get("input_count") or 0)
        button_count = int(last.get("button_count") or 0)
        url = str(last.get("url") or "").lower()
        real_surface = (
            last.get("accountish")
            or last.get("formish")
            or input_count > 0
            or (button_count >= 2 and body_length > 300)
        )
        if real_surface and ready in {"interactive", "complete"} and not last.get("loadingish"):
            return last
        if (
            not reloaded_shell
            and ("successfactors.com/career" in url or "jobs.hr.cloud.sap" in url)
            and ready in {"interactive", "complete", "loading"}
            and body_length < 150
            and asyncio.get_event_loop().time() + 6 < deadline
        ):
            try:
                page = await browser.get_current_page()
                if page is not None:
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            reloaded_shell = True
        await asyncio.sleep(0.5)
    return last


async def _static_fill_passes(
    browser: BrowserSession,
    facts: dict,
    resume_path: str,
    passes: int,
    worker_id: int,
) -> dict:
    review: dict = {}
    safe_progress: list[dict] = []
    seen_signatures: dict[str, int] = {}
    seen_progress_clicks: dict[str, int] = {}
    last_surface: dict = {}
    for idx in range(max(1, passes)):
        last_surface = await _wait_for_visible_surface(browser, timeout=12.0 if idx == 0 else 5.0)
        review = await run_static_autofill(
            browser,
            facts,
            resume_path=resume_path,
            guard_final_submit=True,
        )
        checkpoint_timed_out = False
        while (review.get("human_checkpoint") or {}).get("required"):
            checkpoint_result = await _pause_for_workday_human_click(
                browser,
                worker_id,
                review["human_checkpoint"],
            )
            review["human_checkpoint_result"] = checkpoint_result
            if checkpoint_result.get("timed_out"):
                safe_progress.append({
                    "clicked": False,
                    "reason": "human_click_checkpoint_timed_out",
                    "label": checkpoint_result.get("action", ""),
                    "url": checkpoint_result.get("url", ""),
                })
                checkpoint_timed_out = True
                break
            review = await run_static_autofill(
                browser,
                facts,
                resume_path=resume_path,
                guard_final_submit=True,
            )
        review["safe_progress"] = safe_progress
        review["surface"] = last_surface
        if checkpoint_timed_out:
            break
        if review.get("verificationCodeRequired") or review.get("credentialError"):
            break

        guard = review.get("submit_guard") or {}
        if isinstance(guard, dict) and guard.get("blocked"):
            safe_progress.append({
                "clicked": False,
                "reason": "final_submit_guard_blocked",
                "label": (guard.get("blocked") or {}).get("label", ""),
                "url": review.get("url", ""),
            })
            break

        signature = json.dumps(
            {
                "url": review.get("url", ""),
                "required": (review.get("requiredEmptyLabels") or [])[:6],
                "errors": (review.get("visibleErrors") or [])[:6],
                "needs": (review.get("needsLlm") or [])[:6],
                "filled": review.get("filled") or 0,
                "restored": review.get("restored") or 0,
                "selects": review.get("selects") or 0,
                "choices": review.get("choices") or 0,
            },
            sort_keys=True,
        )
        seen_signatures[signature] = seen_signatures.get(signature, 0) + 1
        if seen_signatures[signature] > 2:
            safe_progress.append({
                "clicked": False,
                "reason": "loop_signature_repeated",
                "url": review.get("url", ""),
            })
            break

        progress = await try_safe_progress_step(browser)
        progress_entry = {
            "clicked": bool(progress.get("clicked")),
            "reason": progress.get("reason", ""),
            "label": progress.get("label", ""),
            "url": progress.get("url", ""),
            "empty_fields": (progress.get("empty_fields") or [])[:8],
            "candidates": (progress.get("candidates") or [])[:8],
        }
        safe_progress.append(progress_entry)
        if progress.get("clicked"):
            click_sig = json.dumps(
                {
                    "reason": progress_entry["reason"],
                    "label": progress_entry["label"][:100],
                    "url": progress_entry["url"],
                },
                sort_keys=True,
            )
            seen_progress_clicks[click_sig] = seen_progress_clicks.get(click_sig, 0) + 1
            if seen_progress_clicks[click_sig] > 2:
                safe_progress.append({
                    "clicked": False,
                    "reason": "same_progress_click_repeated",
                    "label": progress_entry["label"],
                    "url": progress_entry["url"],
                })
                break
        if not progress.get("clicked"):
            if (
                (
                    progress.get("reason") == "no_safe_progress_control"
                    or int(review.get("requiredEmpty") or 0) > 0
                    or int(review.get("invalidFields") or 0) > 0
                    or bool(review.get("needsLlm") or [])
                    or bool(review.get("visibleErrors") or [])
                )
                and (
                    str(last_surface.get("ready") or "").lower() not in {"interactive", "complete"}
                    or int(last_surface.get("body_length") or 0) < 300
                    or (
                        int(last_surface.get("input_count") or 0) == 0
                        and int(last_surface.get("button_count") or 0) < 2
                    )
                    or int(review.get("requiredEmpty") or 0) > 0
                    or int(review.get("invalidFields") or 0) > 0
                    or bool(review.get("needsLlm") or [])
                    or bool(review.get("visibleErrors") or [])
                )
                and idx < max(1, passes) - 1
            ):
                await asyncio.sleep(1.0)
                continue
            break
        await _wait_for_page_settle(browser, 3.0)
        await _wait_for_visible_surface(browser, timeout=8.0)
        # A short wait lets React/ATS widgets settle after upload/autocomplete.
        await asyncio.sleep(0.45)
    return review


async def _run_llm_cleanup(
    browser: BrowserSession,
    *,
    facts: dict,
    profile: dict,
    resume_path: str,
    title: str,
    company: str,
    worker_id: int,
    max_steps: int,
    timeout: float,
    cancel_flag: dict | None = None,
) -> dict:
    """Bounded Gemini/browser-use cleanup for fields static autofill could not finish."""
    if max_steps <= 0 or timeout <= 0:
        return {"attempted": False, "reason": "disabled"}

    state: dict = {
        "attempted": True,
        "steps": 0,
        "stop_reason": "",
        "timeout": False,
        "success": False,
        "errors": [],
        "last_review": {},
        "loop_detected": False,
    }
    previous_issue_sig = ""
    consecutive_issue_repeats = 0

    sensitive_data = {
        "email": facts.get("email", ""),
        "account_email": facts.get("account_email") or facts.get("email", ""),
        "password": facts.get("account_password", ""),
        "first_name": facts.get("first_name", ""),
        "last_name": facts.get("last_name", ""),
        "full_name": facts.get("full_name", ""),
        "phone": facts.get("phone_full") or facts.get("phone", ""),
    }

    async def on_step(_browser_state, _agent_output, _step_num):
        nonlocal previous_issue_sig, consecutive_issue_repeats
        state["steps"] += 1
        review = await run_static_autofill(
            browser,
            facts,
            resume_path=resume_path,
            guard_final_submit=True,
        )
        state["last_review"] = review
        checkpoint = review.get("human_checkpoint") or {}
        while checkpoint.get("required"):
            checkpoint_result = await _pause_for_workday_human_click(
                browser,
                worker_id,
                checkpoint,
            )
            if checkpoint_result.get("timed_out"):
                state["stop_reason"] = (
                    "Workday human-click checkpoint timed out; leaving this tab open and advancing to the next job"
                )
                return
            review = await run_static_autofill(
                browser,
                facts,
                resume_path=resume_path,
                guard_final_submit=True,
            )
            state["last_review"] = review
            checkpoint = review.get("human_checkpoint") or {}
        guard = review.get("submit_guard") or {}
        if isinstance(guard, dict) and guard.get("blocked"):
            state["stop_reason"] = "final submit/review page reached; final button is guarded"
            return
        if review.get("verificationCodeRequired"):
            state["stop_reason"] = "verification code required"
            return
        if review.get("credentialError"):
            state["stop_reason"] = "credential error visible"
            return

        issue_sig = json.dumps(
            {
                "url": review.get("url", ""),
                "required": (review.get("requiredEmptyLabels") or [])[:5],
                "errors": (review.get("visibleErrors") or [])[:5],
                "needs": (review.get("needsLlm") or [])[:5],
                "invalid": int(review.get("invalidFields") or 0),
                # A stable page is not a loop if Gemini has switched strategy.
                # This prevents a newly discovered modal/Apply Manually action
                # from being stopped just because the URL and blank fields did
                # not change yet.
                "action": _agent_action_signature(_agent_output),
            },
            sort_keys=True,
        )
        if issue_sig == previous_issue_sig:
            consecutive_issue_repeats += 1
        else:
            previous_issue_sig = issue_sig
            consecutive_issue_repeats = 1
        if consecutive_issue_repeats >= _GEMINI_LOOP_REPEAT_LIMIT:
            state["loop_detected"] = True
            state["stop_reason"] = (
                f"same blank/error state repeated {_GEMINI_LOOP_REPEAT_LIMIT} consecutive Gemini steps; "
                "leaving tab open and advancing to the next job"
            )

    async def should_stop():
        if cancel_flag and cancel_flag.get("cancel_requested"):
            state["stop_reason"] = "application queue cancelled by user"
        return bool(state.get("stop_reason"))

    task = (
        "BOUNDED CLEANUP MODE. Continue from the current browser tab only. Do not navigate back to LinkedIn unless "
        "the current page is blank, broken, or unrelated.\n"
        f"EXPECTED_JOB: {title} at {company}\n\n"
        "Goal: try hard to complete the visible application/login/account/form flow until the final review/submit point, then STOP with the tab open. "
        "Do not submit or send the final application.\n\n"
        "Order of operations:\n"
        "1. Let the static autofill layer handle common fields first. It runs after every step.\n"
        "1a. If the current page is still the expected LinkedIn job, inspect it with vision, click its Apply or Easy Apply "
        "control once, and continue into the application. If LinkedIn shows a login wall, attempt the visible login flow "
        "before declaring the job stuck. Do not browse to a different job.\n"
        "2. Handle account creation and login pages when required. Use <secret>account_email</secret> and "
        "<secret>password</secret> for external ATS account creation/login. If confirm password appears, use the same password.\n"
        "2a. If a 'LangHire paused' banner appears on Workday, take no action. The runner has frozen you while the "
        "user performs the protected Create Account/Sign In click and will resume automatically.\n"
        "3. For Workday/Greenhouse/Lever/Ashby/SuccessFactors/Oracle/iCIMS-style pages, choose manual/guest apply when available. "
        "Avoid resume-autofill shortcuts like Use My Last Application or Autofill with Resume.\n"
        "4. Fill only fields still blank after static autofill. Do not rewrite fields that already contain static values.\n"
        "5. If a location/autocomplete field rejects free text, clear it once, type the location, then select the matching dropdown option.\n"
        "6. If a privacy/terms dialog appears, read it and click Accept/Agree only. Never click Decline/Reject.\n"
        "7. Click non-final Next/Continue/Create Account/Start Application buttons only after visible required blanks are handled.\n"
        "8. Continue through multi-page forms by pressing safe Next/Continue buttons after each page is filled. "
        "Never click Submit, Submit application, Send application, Finish application, Complete application, or any final apply/send button. "
        "If that is the only remaining action, call done(success=true) and leave the tab open.\n\n"
        "Loop and timeout rules:\n"
        "- Do not try the same failed click or same text field more than twice.\n"
        "- If you are stuck on one field, dropdown, login, privacy dialog, or page for more than 5 steps, call done(success=false) and explain it.\n"
        "- If a verification code/OTP/2FA/email confirmation is requested, call done(success=false). Do not open email and do not guess.\n"
        "- If credentials are rejected, call done(success=false). Do not reset passwords or send recovery emails.\n\n"
        "Resume file is available at the configured upload path. Upload it when an obvious resume/CV file input is visible.\n"
        "Name rule: use first_name for first/preferred name fields, last_name for last/family name fields, "
        "and full_name only for full/legal name fields. Do not overwrite static-filled name fields.\n"
        f"{format_facts_for_prompt(_facts_for_cleanup_prompt(facts))}"
    )

    print(f"  🤖 [W{worker_id}] Gemini cleanup: up to {max_steps} steps / {int(timeout)}s")
    try:
        llm = config.get_llm()
        agent = Agent(
            task=task,
            llm=llm,
            use_vision=True,
            browser_session=browser,
            sensitive_data=sensitive_data,
            available_file_paths=[resume_path],
            include_attributes=[
                "id", "name", "type", "role", "aria-label", "placeholder", "autocomplete",
                "value", "required", "aria-required", "aria-invalid",
                "data-static-autofilled", "data-hybrid-needs-llm", "data-hybrid-needs-llm-reason",
                "data-hybrid-required-empty", "data-hybrid-invalid-field", "data-hybrid-visible-error",
                "data-hybrid-final-submit-blocked",
            ],
            save_conversation_path=str(LOGS_DIR / f"manual_cleanup_{company.replace(' ', '_')}_{title.replace(' ', '_')[:30]}"),
            max_failures=4,
            loop_detection_enabled=True,
            loop_detection_window=4,
            step_timeout=45,
            llm_timeout=75,
            max_actions_per_step=3,
            enable_planning=False,
            use_thinking=False,
            max_history_items=8,
            calculate_cost=True,
            directly_open_url=False,
            register_new_step_callback=on_step,
            register_should_stop_callback=should_stop,
        )
        result = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=timeout)
        try:
            state["success"] = bool(result.is_successful())
        except Exception:
            state["success"] = False
        try:
            state["errors"] = [str(e) for e in (result.errors() or []) if e][:5]
        except Exception:
            state["errors"] = []
        # browser-use also has its own loop detector. Normalize that result into
        # the same deterministic queue transition used by our page-state guard.
        if not state.get("stop_reason") and _looks_like_loop_stop(*state["errors"]):
            state["loop_detected"] = True
            state["stop_reason"] = "Gemini/browser loop detector fired; leaving tab open and advancing to the next job"
    except asyncio.TimeoutError:
        state["timeout"] = True
        state["stop_reason"] = state.get("stop_reason") or "Gemini cleanup timed out"
    except Exception as exc:
        if _looks_like_loop_stop(type(exc).__name__, exc):
            state["loop_detected"] = True
            state["stop_reason"] = (
                "Gemini/browser loop detector fired; leaving tab open and advancing to the next job"
            )
        else:
            state["stop_reason"] = state.get("stop_reason") or f"Gemini cleanup error: {type(exc).__name__}: {str(exc)[:300]}"

    try:
        final_review = await run_static_autofill(
            browser,
            facts,
            resume_path=resume_path,
            guard_final_submit=True,
        )
        state["last_review"] = final_review
    except Exception as exc:
        if not state.get("stop_reason"):
            state["stop_reason"] = f"cleanup stopped; final static scan unavailable: {type(exc).__name__}"
        if not state.get("last_review"):
            state["last_review"] = {"error": f"final_static_scan_failed: {type(exc).__name__}: {str(exc)[:200]}"}
    return state


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
