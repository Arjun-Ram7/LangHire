"""Advance Workday application steps after Fapply, stopping before submission."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

try:
    from core.autofill_facts import _submit_guard_script, release_review_handoff, wait_while_ai_paused
    from core.workday_flow import _current_page
except ImportError:
    from backend.core.autofill_facts import _submit_guard_script, release_review_handoff, wait_while_ai_paused
    from backend.core.workday_flow import _current_page


def is_workday_application(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in (
        "myworkdayjobs.com", "myworkdaysite.com", "workday.com",
    ))


def step_script(*, advance: bool = False) -> str:
    # Probe and click share validation so a late DOM change cannot turn Next
    # into Submit between the Python check and the actual click.
    return r"""() => {
      const advance = ADVANCE;
      const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
      const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && s.display !== 'none'
          && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0;
      };
      const all = selector => Array.from(document.querySelectorAll(selector)).filter(visible);
      const label = el => clean(el.innerText || el.getAttribute('aria-label') || el.value);
      const active = all('[data-automation-id="progressBarActiveStep"], [aria-current="step"]')
        .map(label).join(' ');
      const heading = all('main h2, [role="main"] h2, h2').map(label).join(' | ');
      const next = all('[data-automation-id="pageFooterNextButton"]')[0];
      const controls = all('input, textarea, select, [role="combobox"], [role="checkbox"], [role="radiogroup"], button[aria-haspopup="listbox"]')
        .filter(el => el.tagName !== 'INPUT' || !['hidden','submit','button','reset'].includes(el.type));
      // Autofill may add experience/education rows on the same step. Field
      // counts or generated input IDs are not evidence of page navigation.
      const key = JSON.stringify([location.href, active, heading]);
      const review = /\breview\b/i.test(active || heading)
        || (next && /\b(submit|send|finish|complete|apply)\b/i.test(label(next)));
      const errors = all('[role="alert"], [data-automation-id="errorMessage"], [aria-live="assertive"]')
        .map(label).filter(t => t && !/successfully uploaded/i.test(t));
      const blockers = [];
      let humanRequired = false;
      for (const el of controls) {
        if (el.disabled) continue;
        const name = clean(el.getAttribute('aria-label') || el.labels?.[0]?.innerText || el.name || el.id || 'Required field');
        if (el.type === 'password' || /verification code|one.time|\botp\b/i.test(name)) {
          humanRequired = true;
          blockers.push('Sign-in or verification requires your input');
          continue;
        }
        if (el.getAttribute('aria-invalid') === 'true' || el.validity?.valid === false) {
          blockers.push(name);
          continue;
        }
        const required = el.required || el.getAttribute('aria-required') === 'true';
        if (!required) continue;
        let value = clean(el.value || el.innerText || el.getAttribute('aria-valuetext'));
        if (el.type === 'checkbox' || el.getAttribute('role') === 'checkbox') {
          value = el.checked || el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
        } else if (el.getAttribute('role') === 'radiogroup') {
          value = el.querySelector('[aria-checked="true"], input:checked') ? 'checked' : '';
        }
        if (!value || /^(select one|select|choose|please select|select a value)$/i.test(value)) blockers.push(name);
      }
      if (all('iframe[title*="challenge" i], [data-automation-id="captchaChallenge"]').length) {
        humanRequired = true;
        blockers.push('CAPTCHA requires your input');
      }
      const paused = !!window.__LANGHIRE_PAUSE_CONTROL?.paused;
      const safeNext = !!next && /^(save\s*(?:and|&)\s*continue|next|continue)$/i.test(label(next))
        && !next.disabled && next.getAttribute('aria-disabled') !== 'true';
      const state = {key, active: active || heading, review: !!review, blockers: [...new Set(blockers)],
        errors: [...new Set(errors)], safeNext, paused, humanRequired, controlCount: controls.length, clicked: false};
      if (advance && !review && !paused && safeNext && !blockers.length && !errors.length) {
        next.click();
        state.clicked = true;
      }
      return state;
    }""".replace("ADVANCE", "true" if advance else "false")


async def _probe(page, *, advance=False):
    raw = await page.evaluate(step_script(advance=advance))
    return json.loads(raw) if isinstance(raw, str) else raw


async def run_workday_pages(browser, fill_page, *, cancel_flag=None, worker_id=0,
                            max_steps=12, transition_timeout=12.0, poll_interval=0.5):
    """Run one bounded fill per step; preserve partial results for manual review."""
    result = {"verified": False, "clicked": False, "filled_by_fapply": 0,
              "reached_review": False, "pages": [], "reason": ""}
    seen = set()
    page = await _current_page(browser)
    origin = urlsplit(await page.evaluate("() => location.href"))
    if not is_workday_application(origin.geturl()):
        return {**result, "reason": "Not a Workday application"}
    # Workday uses an SPA: pin both tab and application path across steps.
    job_path = origin.path.split("/apply")[0]
    try:
        for _ in range(max_steps):
            if cancel_flag and cancel_flag.get("cancel_requested"):
                result["reason"] = "Queue stopped; current Workday page left open"
                break
            await wait_while_ai_paused(browser, cancel_flag, worker_id)
            if cancel_flag and cancel_flag.get("cancel_requested"):
                result["reason"] = "Queue stopped; current Workday page left open"
                break
            current_page = await _current_page(browser)
            current_url = urlsplit(await current_page.evaluate("() => location.href"))
            if current_url.netloc != origin.netloc or current_url.path.split("/apply")[0] != job_path:
                result["reason"] = "Application changed; stopped for manual review"
                break
            page = current_page
            await page.evaluate(_submit_guard_script())
            state = await _probe(page)
            if state["review"]:
                result.update(reached_review=True, reason="Workday Review reached; final Submit is yours")
                break
            if state["humanRequired"]:
                result["reason"] = "Workday needs your input: " + "; ".join(state["blockers"][:5])
                break
            if state["key"] in seen:
                result["reason"] = "Workday returned to an already processed step"
                break
            seen.add(state["key"])
            print(f"  📄 [F{worker_id}] Workday step {len(seen)}: {state['active'] or 'Application'}")
            try:
                verification = await asyncio.wait_for(fill_page(), timeout=80.0) if state["controlCount"] else {"verified": False, "clicked": False}
            except TimeoutError:
                result["reason"] = "Fapply timed out on this Workday page; finish it manually"
                break
            result["verified"] |= bool(verification.get("verified"))
            result["clicked"] |= bool(verification.get("clicked"))
            result["filled_by_fapply"] += int(verification.get("filled_by_fapply") or 0)
            after = await _probe(page)
            result["pages"].append({"step": state["active"], "fill": verification,
                                    "blockers": after["blockers"], "errors": after["errors"]})
            if after["review"]:
                result.update(reached_review=True, reason="Workday Review reached; final Submit is yours")
                break
            # If the extension advanced itself, re-probe before any further click.
            if after["key"] != state["key"]:
                continue
            if after["blockers"] or after["errors"]:
                result["reason"] = "Workday needs your input: " + "; ".join((after["blockers"] + after["errors"])[:5])
                break
            if state["controlCount"] and not verification.get("verified"):
                # A completed fill event may make no changes on a prefilled page.
                if not verification.get("event"):
                    result["reason"] = verification.get("reason") or "Fapply completion could not be verified"
                    break
            await wait_while_ai_paused(browser, cancel_flag, worker_id)
            if cancel_flag and cancel_flag.get("cancel_requested"):
                result["reason"] = "Queue stopped; current Workday page left open"
                break
            advanced = await _probe(page, advance=True)
            if not advanced["clicked"]:
                result["reason"] = "Workday Next is unavailable or the page needs manual review"
                break
            # Wait for a stable new step, not simply a new URL (Workday reuses it).
            deadline = asyncio.get_running_loop().time() + transition_timeout
            next_key = None
            while asyncio.get_running_loop().time() < deadline:
                if cancel_flag and cancel_flag.get("cancel_requested"):
                    break
                await asyncio.sleep(poll_interval)
                state_now = await _probe(page)
                if state_now["errors"]:
                    result["reason"] = "Workday validation: " + "; ".join(state_now["errors"][:5])
                    break
                ready = state_now["key"] != advanced["key"] and (state_now["safeNext"] or state_now["review"])
                if ready and next_key == state_now["key"]:
                    break
                next_key = state_now["key"] if ready else None
            else:
                result["reason"] = "Workday did not advance after Save and Continue"
            if result["reason"]:
                break
        else:
            result["reason"] = "Workday step limit reached; remaining pages need manual review"
    finally:
        # Hand back normal controls, including final Submit, without clicking it.
        await release_review_handoff(browser)
    return result
