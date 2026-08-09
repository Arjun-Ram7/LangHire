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
from urllib.parse import urlparse

from browser_use import Agent, BrowserSession

try:
    import core.shared_config as config
    from core.shared_config import LOGS_DIR
    from core.autofill_facts import (
        _submit_guard_script,
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
        _submit_guard_script,
        format_facts_for_prompt,
        probe_workday_human_checkpoint,
        run_static_autofill,
        try_controlled_final_submit,
        try_safe_progress_step,
        wait_for_workday_human_checkpoint,
    )


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


async def _current_page(browser: BrowserSession):
    page = await browser.get_current_page()
    if page is None:
        await browser.new_page("about:blank")
        page = await browser.get_current_page()
    if page is None:
        raise RuntimeError("Browser did not expose a current page")
    return page


async def _page_url(browser: BrowserSession) -> str:
    try:
        page = await _current_page(browser)
        url = await page.evaluate("() => window.location.href")
        if url:
            return url
    except Exception:
        pass
    try:
        return await browser.get_current_page_url()
    except Exception:
        return ""


async def _wait_for_page_settle(browser: BrowserSession, seconds: float = 1.5) -> str:
    """Wait briefly for SPA navigation/new-tab redirects to settle."""
    last_url = ""
    stable = 0
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        url = await _page_url(browser)
        if url and url == last_url:
            stable += 1
            if stable >= 2:
                return url
        else:
            stable = 0
            last_url = url
        await asyncio.sleep(0.25)
    return last_url or await _page_url(browser)


def _human_click_timeout_seconds() -> float:
    try:
        return max(5.0, float(os.environ.get("LANGHIRE_HUMAN_CLICK_TIMEOUT_SECONDS", "300")))
    except (TypeError, ValueError):
        return 300.0


async def _pause_for_workday_human_click(
    browser: BrowserSession,
    worker_id: int,
    initial: dict | None = None,
) -> dict:
    checkpoint = initial if isinstance(initial, dict) else None
    if not checkpoint or not checkpoint.get("required"):
        checkpoint = await probe_workday_human_checkpoint(browser)
    if not checkpoint.get("required"):
        return {**checkpoint, "completed": False, "timed_out": False}

    while checkpoint.get("required"):
        action = str(checkpoint.get("action") or "Create Account")
        print(
            f"    🖱️  [W{worker_id}] LangHire paused — click {action} in Workday; "
            "AI will resume automatically"
        )
        result = await wait_for_workday_human_checkpoint(
            browser,
            initial=checkpoint,
            timeout_seconds=_human_click_timeout_seconds(),
        )
        if result.get("timed_out"):
            print(f"    ⏭️  [W{worker_id}] No {action} click detected before timeout; skipping this posting")
            return result

        # A successful account action can immediately reveal another protected
        # Workday account action. Keep the AI frozen until that chain is clear.
        await asyncio.sleep(0.35)
        checkpoint = result.get("next_checkpoint") or await probe_workday_human_checkpoint(browser)
        if not checkpoint.get("required"):
            print(f"    ▶️  [W{worker_id}] {action} click/page change detected; AI resuming")
            return result

    return {**checkpoint, "completed": False, "timed_out": False}


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
        "abandoned": int(review.get("abandoned") or 0),
        "abandoned_labels": (review.get("abandonedLabels") or [])[:8],
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
            return {"flags": ["risk_scan_parse_error"]}
    except Exception as exc:
        return {"flags": [f"risk_scan_failed:{type(exc).__name__}"]}


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
        # verificationCodeRequired above only catches OTP/PIN text inputs — a
        # CAPTCHA (recaptcha/hcaptcha) is an interactive challenge widget, not
        # a text field, so it was never caught until the pre-submit risk scan
        # ran (too late — cleanup would burn its whole step budget trying to
        # click through something it can't solve). Check every step instead.
        try:
            risk = await _submission_risk_scan(browser)
            if "human_verification" in (risk.get("flags") or []):
                state["stop_reason"] = "CAPTCHA/human verification challenge present; leaving tab open for manual review"
                return
        except Exception:
            pass

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
        "4a. Work through visible fields in a single top-to-bottom pass, in the order they appear on the page. Before "
        "acting on any field, check its current value first: if it already shows a filled/selected value (static-filled "
        "or otherwise), skip it immediately and move to the next field below it. Never re-click, re-select, or retype "
        "a field that already has a value, even if you are not the one who filled it.\n"
        "4b. Once you act on a field, do not return to it later in the same pass unless it is still visibly blank or "
        "shows a validation error. Do not bounce between an earlier field and a later one — always move forward.\n"
        "5. If a location/autocomplete field rejects free text, clear it once, type the location, then select the matching dropdown option.\n"
        "6. If a privacy/terms dialog appears, read it and click Accept/Agree only. Never click Decline/Reject.\n"
        "7. Click non-final Next/Continue/Create Account/Start Application buttons only after visible required blanks are handled.\n"
        "8. Continue through multi-page forms by pressing safe Next/Continue buttons after each page is filled. "
        "Never click Submit, Submit application, Send application, Finish application, Complete application, or any final apply/send button. "
        "If that is the only remaining action, call done(success=true) and leave the tab open.\n\n"
        "Loop and timeout rules:\n"
        "- Do not try the same failed click or same text field more than twice.\n"
        "- If a field already shows a value, that counts as done — do not click it again to \"double check\" it.\n"
        "- If you are stuck on one field, dropdown, login, privacy dialog, or page for more than 5 steps, skip it and move to "
        "the next field if the page allows it; only call done(success=false) if skipping is not possible (e.g. it blocks all further fields).\n"
        "- If a verification code/OTP/2FA/email confirmation is requested, call done(success=false). Do not open email and do not guess.\n"
        "- If credentials are rejected, call done(success=false). Do not reset passwords or send recovery emails.\n\n"
        "Resume file is available at the configured upload path. Upload it when an obvious resume/CV file input is visible.\n"
        "Name rule: use first_name for first/preferred name fields, last_name for last/family name fields, "
        "and full_name only for full/legal name fields. Do not overwrite static-filled name fields.\n"
        f"{format_facts_for_prompt(_facts_for_cleanup_prompt(facts))}"
    )

    print(f"  🤖 [W{worker_id}] Gemini cleanup: up to {max_steps} steps / {int(timeout)}s")
    try:
        # Install the submit-click guard as a page-lifecycle init script so it
        # is live from the first paint of every future navigation, not just
        # reactively between agent steps. This is what actually closes the
        # Pariveda race (a same-step navigate-then-click could fire before
        # on_step ever got a chance to reinstall the guard on the new page) —
        # max_actions_per_step no longer needs to carry that burden.
        try:
            await browser._cdp_add_init_script(_submit_guard_script())
        except Exception as exc:
            print(f"  ⚠️  [W{worker_id}] Could not install proactive submit guard: {type(exc).__name__}: {exc}")
        llm = config.get_llm()
        # gemini-2.5-flash-lite has been observed returning malformed JSON
        # mid-run (a Pydantic validation error on AgentOutput), which
        # browser_use otherwise just retries against the same failing model.
        # A fallback model lets it recover instead of burning retries/steps.
        fallback_llm = None
        try:
            from core.config import load_llm_settings
            from core.llm_factory import create_fallback_llm
            fallback_llm = create_fallback_llm(load_llm_settings())
        except Exception:
            try:
                from backend.core.config import load_llm_settings
                from backend.core.llm_factory import create_fallback_llm
                fallback_llm = create_fallback_llm(load_llm_settings())
            except Exception as exc:
                print(f"  ⚠️  [W{worker_id}] Could not build fallback LLM: {type(exc).__name__}: {exc}")
        agent = Agent(
            task=task,
            llm=llm,
            fallback_llm=fallback_llm,
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
            # Back to 3: the proactive init-script guard above (not step
            # boundaries) is what protects against the Submit-button race now.
            # At 1 action/step every click got its own fresh screenshot+reasoning
            # cycle, which gave the model repeated chances to "re-check" fields
            # it had already answered correctly — that oscillation is what
            # burned the whole step budget on Nuclear's demographics section
            # and left real required fields (country, sponsorship, etc.) never
            # reached. The resolved-field lock in autofill_facts.py (pointer-
            # events:none once a combobox/choice is confirmed) is the actual
            # fix for that: there's nothing left to click, regardless of batch size.
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
        # A clean done(success=false) from the agent doesn't go through on_step's
        # stop_reason branches (those are for hard triggers like a checkpoint
        # timeout or loop detection) — without this, its own accurate reason for
        # giving up is discarded and callers fall back to a generic/misleading
        # message (e.g. blaming "Apply button never clicked" when the agent had
        # in fact clicked Apply and then hit a genuinely blank/broken page).
        if not state.get("stop_reason") and not state["success"]:
            try:
                final_text = result.final_result()
                if final_text:
                    state["stop_reason"] = str(final_text)[:400]
            except Exception:
                pass
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


class WorkdayDeterministicUnavailable(Exception):
    """Raised when the deterministic engine fails before completing any pass.

    Callers should fall back to the vision agent when they see this; any
    later failure is folded into the returned manual_review result instead.
    """


def scale_cleanup_budget(summary: dict, base_steps: int, base_timeout: float) -> tuple[int, float]:
    """Scale the LLM cleanup budget to how much work is actually left.

    A flat 35-step/300s budget regardless of form size is why Nuclear's
    genuinely-required fields (country, sponsorship, authorization) never
    got reached — the model spent its fixed budget on the fields it hit
    first and ran out before the rest. A page with many unresolved custom
    questions gets proportionally more room; a near-complete page keeps the
    default (never less — a short form finishing early is not a problem).
    """
    remaining = int(summary.get("required_empty") or 0) + len(summary.get("needs_llm") or [])
    if remaining <= 8:
        return base_steps, base_timeout
    # +2 steps and +15s per field beyond the baseline of 8, capped at 2x so a
    # single pathological page can't consume the whole worker indefinitely.
    extra_fields = remaining - 8
    scaled_steps = min(base_steps * 2, base_steps + extra_fields * 2)
    scaled_timeout = min(base_timeout * 2, base_timeout + extra_fields * 15.0)
    return scaled_steps, scaled_timeout


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
            scaled_steps, scaled_timeout = scale_cleanup_budget(summary_before_cleanup, llm_steps, llm_timeout)
            cleanup = await _run_llm_cleanup(
                browser,
                facts=facts,
                profile={},
                resume_path=resume_path,
                title=facts.get("job_title", ""),
                company=facts.get("job_company", ""),
                worker_id=worker_id,
                max_steps=scaled_steps,
                timeout=scaled_timeout,
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
