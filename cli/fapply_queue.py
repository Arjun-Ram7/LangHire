"""Open application forms, using LangHire for Workday and Fapply elsewhere.

LangHire's deterministic and bounded LLM automation may navigate job landing
pages and clear sign-in/account gates.  The moment a real application form is
detected, Workday uses deterministic filling followed by LLM cleanup; other
sites use the installed Fapply extension. The queue advances Workday to Review,
and leaves the final submission to the candidate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import Agent, BrowserSession
from browser_use.utils import is_new_tab_page

try:
    import core.shared_config as config
    from core.autofill_facts import load_autofill_facts, run_static_autofill, set_ai_pause_control, wait_while_ai_paused
    from core.shared_config import (
        BROWSER_PROFILE_DIR,
        CANDIDATE_PROFILE,
        JOBS_FILE,
        RESUME_PATH,
        SENSITIVE_DATA,
        claim_job,
        load_json,
        update_job,
    )
    from core.config import load_settings
    from core.workday_flow import _current_page, _page_url, _wait_for_page_settle, run_workday_deterministic, release_review_handoff
except ImportError:
    import backend.core.shared_config as config
    from backend.core.autofill_facts import load_autofill_facts, run_static_autofill, set_ai_pause_control, wait_while_ai_paused
    from backend.core.shared_config import (
        BROWSER_PROFILE_DIR,
        CANDIDATE_PROFILE,
        JOBS_FILE,
        RESUME_PATH,
        SENSITIVE_DATA,
        claim_job,
        load_json,
        update_job,
    )
    from backend.core.config import load_settings
    from backend.core.workday_flow import _current_page, _page_url, _wait_for_page_settle, run_workday_deterministic, release_review_handoff

from cli.apply_jobs import _run_apply_preflight, _switch_to_tab
from cli.workday_pages import is_workday_application, _probe


FAPPLY_EXTENSION_ID = "hlmndegihhncfhangpfjpibgiclnffmd"
FAPPLY_FINISH_EVENTS = (
    "LEVER_FILL_FINISHED",
    "GREENHOUSE_FILL_FINISHED",
    "ASHBY_FILL_FINISHED",
    "WORKDAY_FILL_FINISHED",
    "INDEED_FILL_FINISHED",
    "APPLYTOJOB_FILL_FINISHED",
    "ICIMS_FILL_FINISHED",
    "SMARTRECRUITERS_FILL_FINISHED",
)


def _version_key(path: Path) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", path.name)
    return tuple(int(number) for number in numbers) or (0,)


def find_fapply_extension_path(extra_roots: Iterable[Path] = ()) -> Path | None:
    """Find the newest unpacked Fapply extension without copying user data."""
    override = (os.environ.get("LANGHIRE_FAPPLY_EXTENSION_PATH") or "").strip()
    roots: list[Path] = [Path(override)] if override else []
    roots.extend(Path(root) for root in extra_roots)
    if sys.platform == "darwin":
        roots.append(
            Path.home()
            / "Library/Application Support/BraveSoftware/Brave-Browser/Default/Extensions"
            / FAPPLY_EXTENSION_ID
        )
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        roots.append(
            local
            / "BraveSoftware/Brave-Browser/User Data/Default/Extensions"
            / FAPPLY_EXTENSION_ID
        )
    else:
        roots.extend(
            [
                Path.home() / ".config/BraveSoftware/Brave-Browser/Default/Extensions" / FAPPLY_EXTENSION_ID,
                Path.home() / ".config/brave/Default/Extensions" / FAPPLY_EXTENSION_ID,
            ]
        )

    candidates: list[Path] = []
    for root in roots:
        if (root / "manifest.json").is_file():
            candidates.append(root)
        elif root.is_dir():
            candidates.extend(path for path in root.iterdir() if (path / "manifest.json").is_file())
    for candidate in sorted(candidates, key=_version_key, reverse=True):
        try:
            manifest = json.loads((candidate / "manifest.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "fapply" in str(manifest.get("name") or "").lower():
            return candidate.resolve()
    return None


def fapply_browser_kwargs(extension_path: Path) -> dict[str, Any]:
    """Launch the normal LangHire profile with only the local Fapply extension."""
    kwargs = config.browser_session_kwargs()
    args = list(kwargs.get("args") or [])
    extension = str(extension_path)
    args.extend(
        [
            f"--disable-extensions-except={extension}",
            f"--load-extension={extension}",
        ]
    )
    kwargs["args"] = args
    return kwargs


_SURFACE_PROBE_JS = r"""() => {
  const norm = (value) => String(value || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0 && rect.width > 2 && rect.height > 2;
  };
  const roots = [document];
  for (let i = 0; i < roots.length; i += 1) {
    for (const el of roots[i].querySelectorAll('*')) {
      if (el.shadowRoot) roots.push(el.shadowRoot);
    }
  }
  const controls = roots.flatMap((root) => Array.from(
    root.querySelectorAll('input, textarea, select, [contenteditable="true"]')
  )).filter((el) => {
    const type = norm(el.getAttribute('type'));
    return visible(el) && !['hidden', 'button', 'submit', 'reset', 'image'].includes(type)
      && !el.disabled;
  });
  const descriptor = (el) => {
    const id = el.id;
    const labelled = norm(el.getAttribute('aria-label'));
    const explicit = id ? norm(Array.from(document.querySelectorAll('label'))
      .filter((label) => label.htmlFor === id).map((label) => label.innerText).join(' ')) : '';
    const context = norm(el.closest('label, fieldset, [role="group"], .application-question, .field, div')?.innerText);
    return norm([
      labelled, explicit, el.getAttribute('name'), el.getAttribute('placeholder'),
      el.getAttribute('autocomplete'), context.slice(0, 180)
    ].filter(Boolean).join(' '));
  };
  const labels = controls.map(descriptor);
  const bodyText = norm(document.body?.innerText).slice(0, 12000);
  const identityPatterns = [
    /first.?name|given.?name|full.?name|legal.?name/,
    /last.?name|family.?name|surname/,
    /e-?mail/,
    /phone|mobile/,
    /address|city|state|province|postal|zip|location/,
    /linkedin|portfolio|website/,
    /resume|curriculum|\bcv\b/
  ];
  const identityCount = identityPatterns.filter((pattern) => labels.some((label) => pattern.test(label))).length;
  const passwordInputs = controls.filter((el) => norm(el.getAttribute('type')) === 'password').length;
  const fileInputs = controls.filter((el) => norm(el.getAttribute('type')) === 'file').length;
  const buttons = roots.flatMap((root) => Array.from(root.querySelectorAll('button, input[type="submit"], [role="button"]')))
    .filter(visible).map((el) => norm(el.innerText || el.value || el.getAttribute('aria-label'))).filter(Boolean);
  return {
    url: location.href,
    title: document.title || '',
    bodyText,
    controlCount: controls.length,
    identityCount,
    passwordInputs,
    fileInputs,
    buttonLabels: buttons.slice(0, 40),
    fieldLabels: labels.slice(0, 30)
  };
}"""


def classify_application_surface(surface: dict[str, Any]) -> dict[str, Any]:
    """Make a conservative application-vs-landing-page decision."""
    url = str(surface.get("url") or "").lower()
    title = str(surface.get("title") or "").lower()
    text = str(surface.get("bodyText") or "").lower()
    controls = int(surface.get("controlCount") or 0)
    identity = int(surface.get("identityCount") or 0)
    files = int(surface.get("fileInputs") or 0)
    passwords = int(surface.get("passwordInputs") or 0)
    buttons = " ".join(str(label).lower() for label in surface.get("buttonLabels") or [])
    reasons: list[str] = []
    score = 0

    if not url or url.startswith(("about:", "chrome:", "brave:")):
        return {"is_application": False, "score": 0, "reason": "blank/browser page"}
    if re.search(r"page not found|job not found|404|no longer available|position has been filled", f"{title} {text[:1500]}"):
        return {"is_application": False, "score": 0, "reason": "missing or expired job page"}

    ats = any(
        marker in url
        for marker in (
            "jobs.lever.co/", "greenhouse.io/", "greenhouse.com/", "ashbyhq.com/",
            "myworkdayjobs.com/", "myworkdaysite.com/", "icims.com/", "smartrecruiters.com/",
            "oraclecloud.com/", "applytojob.com/", "jobvite.com/", "successfactors.com/",
        )
    )
    apply_path = bool(re.search(r"/(apply|application)(/|\?|$)", url))
    submit_copy = bool(re.search(r"submit application|send application|complete application", f"{text} {buttons}"))
    account_copy = bool(re.search(r"sign in|log in|create (an )?account|register|forgot password", text[:4000]))
    application_copy = bool(re.search(r"job application|apply for this job|application form|candidate information", text[:6000]))

    if ats:
        score += 1
        reasons.append("ATS host")
    if apply_path:
        score += 2
        reasons.append("application URL")
    if application_copy:
        score += 2
        reasons.append("application heading")
    if submit_copy:
        score += 2
        reasons.append("final application control")
    if files:
        score += 2
        reasons.append("file upload")
    if identity >= 2:
        score += 2
        reasons.append(f"{identity} identity field groups")
    if controls >= 4:
        score += 2
        reasons.append(f"{controls} visible fields")

    linkedin_modal = "linkedin.com" in url and controls >= 2 and bool(
        re.search(r"easy apply|contact info|resume|review your application", text[:6000])
    )
    # Account creation pages can contain name, email, phone, and even an /apply
    # URL, so field count alone must never promote them to application forms.
    account_gate = passwords > 0 and account_copy and not submit_copy
    strong_form = (files > 0 and identity >= 2) or (controls >= 4 and identity >= 2)
    is_application = bool(not account_gate and (strong_form or linkedin_modal) and score >= 5)
    if account_gate:
        reasons.append("account/sign-in page, not application form")
    elif not strong_form and not linkedin_modal:
        reasons.append("not enough visible application fields")
    return {
        "is_application": is_application,
        "score": score,
        "reason": ", ".join(reasons) or "application signals absent",
    }


def is_account_surface(surface: dict[str, Any]) -> bool:
    text = str(surface.get("bodyText") or "").lower()
    passwords = int(surface.get("passwordInputs") or 0)
    controls = int(surface.get("controlCount") or 0)
    return bool(
        passwords > 0
        or (
            controls > 0
            and re.search(
                r"sign in|log in|create (an )?account|register|already have an account|terms and conditions",
                text[:6000],
            )
        )
    )


_FIELD_SNAPSHOT_JS = r"""() => {
  const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity || 1) > 0 && rect.width > 2 && rect.height > 2;
  };
  const roots = [document];
  for (let i = 0; i < roots.length; i += 1) {
    for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  }
  const controls = roots.flatMap((root) => Array.from(
    root.querySelectorAll('input, textarea, select, [contenteditable="true"]')
  )).filter((el) => {
    const type = norm(el.getAttribute('type')).toLowerCase();
    return visible(el) && !el.disabled && !['hidden', 'button', 'submit', 'reset', 'image'].includes(type);
  });
  const populated = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = norm(el.getAttribute('type')).toLowerCase();
    if (type === 'checkbox' || type === 'radio') return Boolean(el.checked);
    if (type === 'file') return Boolean(el.files && el.files.length);
    if (tag === 'select') return el.selectedIndex >= 0 && Boolean(norm(el.value))
      && !/^(select|choose|please select|null|undefined|-)$/.test(norm(el.value).toLowerCase());
    if (el.isContentEditable) return Boolean(norm(el.innerText));
    return Boolean(norm(el.value));
  };
  return {
    total: controls.length,
    populated: controls.filter(populated).length,
    empty: controls.filter((el) => !populated(el)).length,
    url: location.href
  };
}"""


async def _evaluate_dict(browser: BrowserSession, expression: str) -> dict[str, Any]:
    page = await _current_page(browser)
    raw = await page.evaluate(expression)
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


async def probe_application_surface(browser: BrowserSession) -> tuple[dict[str, Any], dict[str, Any]]:
    surface = await _evaluate_dict(browser, _SURFACE_PROBE_JS)
    return surface, classify_application_surface(surface)


async def field_snapshot(browser: BrowserSession) -> dict[str, Any]:
    return await _evaluate_dict(browser, _FIELD_SNAPSHOT_JS)


def assess_fill_evidence(
    before: dict[str, Any], after: dict[str, Any], event: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the auditable evidence that Fapply, rather than navigation, filled fields."""
    try:
        reported = int((event or {}).get("filledCount") or (event or {}).get("filled") or 0)
    except (TypeError, ValueError):
        reported = 0
    delta = max(0, int(after.get("populated") or 0) - int(before.get("populated") or 0))
    return {
        "verified": reported > 0 or delta > 0,
        "reported": reported,
        "delta": delta,
        "filled_by_fapply": max(reported, delta),
    }


async def _install_finish_listener(browser: BrowserSession) -> None:
    names = json.dumps(list(FAPPLY_FINISH_EVENTS))
    page = await _current_page(browser)
    await page.evaluate(
        f"""() => {{
          window.__LANGHIRE_FAPPLY_RESULT = null;
          const names = {names};
          for (const name of names) {{
            const capture = (event) => {{
              const detail = event && event.detail ? event.detail : {{}};
              window.__LANGHIRE_FAPPLY_RESULT = {{event: name, at: Date.now(), ...detail}};
            }};
            window.addEventListener(name, capture, true);
            document.addEventListener(name, capture, true);
          }}
          return true;
        }}"""
    )


async def _finish_result(browser: BrowserSession) -> dict[str, Any]:
    return await _evaluate_dict(browser, "() => window.__LANGHIRE_FAPPLY_RESULT || {}")


async def _fapply_button(browser: BrowserSession, *, click: bool = False) -> dict[str, Any]:
    """Inspect/click Fapply's button through its closed shadow root via CDP."""
    session = await browser.get_or_create_cdp_session(focus=True)
    client = session.cdp_client
    await client.send.DOM.enable(params={}, session_id=session.session_id)
    document = await client.send.DOM.getFlattenedDocument(
        params={"depth": -1, "pierce": True}, session_id=session.session_id
    )
    candidates: list[tuple[int, dict[str, str], str]] = []
    for node in (document or {}).get("nodes") or []:
        if str(node.get("nodeName") or "").upper() != "BUTTON":
            continue
        raw_attrs = node.get("attributes") or []
        attrs = {str(raw_attrs[i]): str(raw_attrs[i + 1]) for i in range(0, len(raw_attrs) - 1, 2)}
        classes = attrs.get("class", "").split()
        if "fapply-start-button" in classes:
            candidates.append((int(node.get("nodeId") or 0), attrs, "start"))
        elif "fapply-signin-button" in classes:
            candidates.append((int(node.get("nodeId") or 0), attrs, "signin"))
    if not candidates:
        return {"found": False, "label": ""}

    states: list[dict[str, Any]] = []
    for node_id, attrs, kind in candidates:
        resolved = await client.send.DOM.resolveNode(
            params={"nodeId": node_id}, session_id=session.session_id
        )
        object_id = ((resolved or {}).get("object") or {}).get("objectId")
        if not object_id:
            continue
        called = await client.send.Runtime.callFunctionOn(
            params={
                "objectId": object_id,
                "functionDeclaration": "function(doClick){const label=(this.innerText||this.textContent||'').replace(/\\s+/g,' ').trim();const available=getComputedStyle(this).display!=='none'&&!this.disabled;if(doClick&&available&&label==='Start AI Autofill'){this.click();}return {label,clicked:Boolean(doClick&&available&&label==='Start AI Autofill'),disabled:Boolean(this.disabled),available};}",
                "arguments": [{"value": click}],
                "returnByValue": True,
                "userGesture": bool(click),
            },
            session_id=session.session_id,
        )
        value = ((called or {}).get("result") or {}).get("value")
        if isinstance(value, dict):
            states.append({"found": True, "attrs": attrs, "kind": kind, **value})
    visible_start = next(
        (state for state in states if state.get("kind") == "start" and state.get("available")), None
    )
    if visible_start:
        return visible_start
    visible_signin = next(
        (state for state in states if state.get("kind") == "signin" and state.get("available")), None
    )
    if visible_signin:
        return visible_signin
    if states:
        return states[0]
    return {"found": False, "label": ""}


async def _wait_for_fapply_button(browser: BrowserSession, timeout: float = 18.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {"found": False, "label": ""}
    while asyncio.get_running_loop().time() < deadline:
        try:
            last = await _fapply_button(browser)
            if last.get("found"):
                return last
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return last


async def run_fapply_and_verify(browser: BrowserSession, timeout: float | None = None) -> dict[str, Any]:
    """Click Fapply once in this run and require evidence that it filled fields."""
    button = await _wait_for_fapply_button(browser)
    label = str(button.get("label") or "").strip()
    if not button.get("found"):
        return {"verified": False, "clicked": False, "reason": "Fapply extension button was not found"}
    if label != "Start AI Autofill":
        reason = f"Fapply is not ready ({label or 'unknown state'})"
        if re.search(r"sign.?in|log.?in|register|account", label, re.I):
            reason = "Fapply sign-in is required in the LangHire browser"
        return {"verified": False, "clicked": False, "reason": reason, "button_label": label}
    if button.get("disabled"):
        return {"verified": False, "clicked": False, "reason": "Fapply autofill button is disabled"}

    before = await field_snapshot(browser)
    await _install_finish_listener(browser)
    clicked = await _fapply_button(browser, click=True)
    if not clicked.get("clicked"):
        return {"verified": False, "clicked": False, "reason": "Fapply button changed before it could be clicked", "before": before}

    timeout = timeout or float(os.environ.get("LANGHIRE_FAPPLY_TIMEOUT", "180"))
    deadline = asyncio.get_running_loop().time() + max(15.0, timeout)
    started_at = asyncio.get_running_loop().time()
    best = before
    max_delta = 0
    stable_positive_polls = 0
    previous_populated = int(before.get("populated") or 0)
    explicit: dict[str, Any] = {}
    last_button: dict[str, Any] = {}

    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2.0)
        current = await field_snapshot(browser)
        explicit = await _finish_result(browser)
        populated = int(current.get("populated") or 0)
        delta = populated - int(before.get("populated") or 0)
        if delta > max_delta:
            max_delta = delta
            best = current
        stable_positive_polls = stable_positive_polls + 1 if delta > 0 and populated == previous_populated else 0
        previous_populated = populated
        try:
            last_button = await _fapply_button(browser)
        except Exception:
            last_button = {}

        evidence = assess_fill_evidence(before, current, explicit)
        reported = int(evidence["reported"])
        if explicit:
            if evidence["verified"] or max_delta > 0:
                return {
                    "verified": True,
                    "clicked": True,
                    "reason": "Fapply reported completion and populated application fields",
                    "filled_by_fapply": max(reported, max_delta, int(evidence["delta"])),
                    "before": before,
                    "after": current,
                    "event": explicit,
                }
            return {
                "verified": False,
                "clicked": True,
                "reason": "Fapply finished but did not populate any fields",
                "before": before,
                "after": current,
                "event": explicit,
            }

        elapsed = asyncio.get_running_loop().time() - started_at
        button_ready_again = str(last_button.get("label") or "").strip() == "Start AI Autofill"
        if max_delta > 0 and elapsed >= 16.0 and (button_ready_again and stable_positive_polls >= 5):
            return {
                "verified": True,
                "clicked": True,
                "reason": "Application field population increased after Fapply",
                "filled_by_fapply": max_delta,
                "before": before,
                "after": best,
            }

    after = await field_snapshot(browser)
    return {
        "verified": False,
        "clicked": True,
        "reason": "Fapply timed out without verified field changes",
        "filled_by_fapply": max_delta,
        "before": before,
        "after": after,
        "button_label": last_button.get("label"),
    }


def _baseline_tab_ids(tabs: Iterable[Any]) -> set[str]:
    """Tabs that existed before this job. Blank tabs are excluded because
    browser_use reuses one for the first navigation, so it is not "old"."""
    return {str(tab.target_id) for tab in tabs if not is_new_tab_page(str(tab.url or ""))}


def is_workday_account_gate(surface: dict[str, Any]) -> bool:
    """Workday sign-in/create-account pages belong to the Workday engine, which
    fills them and hands the account click to the human."""
    return is_workday_application(str(surface.get("url") or "")) and is_account_surface(surface)


async def _select_application_tab(
    browser: BrowserSession, baseline_ids: set[str]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Focus the highest-confidence newly opened application tab."""
    tabs = await browser.get_tabs()
    candidate_ids = [str(tab.target_id) for tab in tabs if str(tab.target_id) not in baseline_ids]
    current_id = str(getattr(browser, "agent_focus_target_id", "") or "")
    ordered = ([current_id] if current_id in candidate_ids else []) + [
        target_id for target_id in candidate_ids if target_id != current_id
    ]
    best: tuple[dict[str, Any], dict[str, Any], str] = (
        {}, {"is_application": False, "score": -1000, "reason": "no newly opened page"}, ""
    )
    for target_id in ordered:
        try:
            await _switch_to_tab(browser, target_id)
            await asyncio.sleep(0.35)
            surface, decision = await probe_application_surface(browser)
            if int(decision.get("score") or 0) > int(best[1].get("score") or 0):
                best = (surface, decision, target_id)
            if decision.get("is_application"):
                return surface, decision, target_id
        except Exception:
            continue
    if best[2]:
        await _switch_to_tab(browser, best[2])
    return best


def _account_only_facts(profile: dict[str, Any]) -> dict[str, Any]:
    facts = load_autofill_facts(profile, RESUME_PATH)
    allowed = {
        "first_name",
        "last_name",
        "full_name",
        "email",
        "account_email",
        "account_password",
        "password",
        "phone",
        "phone_full",
        "terms_accepted",
        "privacy_accepted",
    }
    return {key: value for key, value in facts.items() if key in allowed and value not in (None, "")}


async def _navigate_and_clear_account_gate(
    browser: BrowserSession,
    *,
    profile: dict[str, Any],
    title: str,
    company: str,
    worker_id: int,
    cancel_flag: dict | None = None,
) -> dict[str, Any]:
    """Reuse the old deterministic/LLM flow only until the application form."""
    surface, decision = await probe_application_surface(browser)
    if decision.get("is_application"):
        return {"attempted": False, "reason": "already on application"}
    facts = _account_only_facts(profile)
    runtime_sensitive = (load_settings().get("sensitive_data") or {}) or SENSITIVE_DATA
    account_email = str(runtime_sensitive.get("email") or "").strip()
    account_password = str(runtime_sensitive.get("password") or "")
    password_note = (
        " Use <secret>account_email</secret> and <secret>password</secret> for account sign-in or "
        "creation."
        if account_password.strip()
        else (
            " No password is configured. Prefer guest/manual apply or available LinkedIn/Google SSO. "
            "If neither is available, stop and report that account credentials are required; never "
            "invent or generate a password."
        )
    )
    state: dict[str, Any] = {"attempted": True, "steps": 0, "reason": "", "application_reached": False}

    async def check_boundary(*, run_account_fill: bool) -> bool:
        if cancel_flag and cancel_flag.get("cancel_requested"):
            state["reason"] = "queue cancelled"
            return True
        await wait_while_ai_paused(browser, cancel_flag, worker_id)
        await _wait_for_page_settle(browser, 0.35)
        current, current_decision = await probe_application_surface(browser)
        if current_decision.get("is_application"):
            state["application_reached"] = True
            state["reason"] = "application form reached; handing off to Fapply"
            return True
        page_text = str(current.get("bodyText") or "").lower()
        if re.search(r"captcha|verification code|one[- ]time code|\botp\b|two-factor|2fa", page_text[:6000]):
            state["reason"] = "human verification/OTP required"
            return True
        if run_account_fill and is_account_surface(current):
            try:
                await run_static_autofill(
                    browser,
                    facts,
                    resume_path="",
                    guard_final_submit=True,
                )
            except Exception as exc:
                print(f"  ⚠️  [F{worker_id}] Account-only static fill skipped: {type(exc).__name__}")
        return False

    async def on_step(_browser_state, _agent_output, _step_num):
        state["steps"] += 1
        await check_boundary(run_account_fill=True)

    async def should_stop() -> bool:
        return await check_boundary(run_account_fill=False)

    try:
        await set_ai_pause_control(browser, False)
        await check_boundary(run_account_fill=True)
        if state.get("application_reached") or state.get("reason"):
            return state
        agent = Agent(
            task=(
                "NAVIGATION AND ACCOUNT-GATE MODE ONLY. Continue the expected job application for "
                f"{title} at {company}. On a LinkedIn or employer landing page, click the correct Apply "
                "button. If the employer requires sign-in or account creation, accept required "
                f"terms/privacy and click Sign In or Create Account.{password_note} "
                "Prefer Apply as guest/manual application when offered. Never reset a password, send a "
                "recovery email, guess an OTP, solve a CAPTCHA, or visit a different job. The instant the "
                "real application form appears, STOP: do not type into any application field, upload a "
                "resume, answer a screening question, click form Next/Continue, or submit. Fapply owns all "
                "application-field filling. Never click Submit/Send/Finish/Complete application."
            ),
            llm=config.get_llm(),
            browser_session=browser,
            use_vision=True,
            sensitive_data={
                "account_email": account_email,
                "email": facts.get("email", ""),
                "password": account_password,
                "first_name": facts.get("first_name", ""),
                "last_name": facts.get("last_name", ""),
                "full_name": facts.get("full_name", ""),
            },
            max_actions_per_step=1,
            enable_planning=False,
            use_thinking=False,
            max_history_items=6,
            calculate_cost=True,
            directly_open_url=False,
            register_new_step_callback=on_step,
            register_should_stop_callback=should_stop,
        )
        print(f"  🤖 [F{worker_id}] Account/navigation agent: application fields remain reserved for Fapply")
        await asyncio.wait_for(agent.run(max_steps=15), timeout=240.0)
        await check_boundary(run_account_fill=False)
        if not state.get("reason"):
            state["reason"] = "navigation/account agent stopped before application form"
        return state
    except Exception as exc:
        state["reason"] = f"navigation/account fallback failed: {type(exc).__name__}: {str(exc)[:160]}"
        return state


async def _close_owned_landing_tabs(
    browser: BrowserSession, baseline_ids: set[str], keep_target_id: str
) -> int:
    closed = 0
    for tab in await browser.get_tabs():
        target_id = str(tab.target_id)
        if target_id in baseline_ids or target_id == keep_target_id:
            continue
        try:
            await asyncio.wait_for(browser.close_page(target_id), timeout=2.0)
            closed += 1
        except Exception:
            pass
    return closed


async def open_with_fapply(
    browser: BrowserSession,
    job: dict[str, Any],
    profile: dict[str, Any],
    worker_id: int,
    *,
    cancel_flag: dict | None = None,
    deadline: asyncio.Timeout | None = None,
) -> str:
    url = str(job.get("url") or "")
    title = str(job.get("title") or "Unknown")
    company = str(job.get("company") or "Unknown")
    if not claim_job(url):
        print(f"  ⏭️  [F{worker_id}] Skip claimed/non-pending: {title} at {company}")
        return "skipped"

    print(f"  ⚡ [F{worker_id}] Opening application for Fapply: {title} at {company}")
    preflight: dict[str, Any] = {}
    current_url = ""
    baseline_ids = _baseline_tab_ids(await browser.get_tabs())
    try:
        preflight = await _run_apply_preflight(
            browser,
            url=url,
            title=title,
            company=company,
            easy_apply=bool(job.get("easy_apply")),
            static_facts={},
            resume_path="",
            guard_final_submit=False,
            worker_id=worker_id,
            close_existing_tabs=False,
            open_in_new_tab=True,
            run_static=False,
            cancel_flag=cancel_flag,
        )
        await _wait_for_page_settle(browser, 1.0)
        surface, decision, target_id = await _select_application_tab(browser, baseline_ids)
        workday_gate = is_workday_account_gate(surface)
        if not decision.get("is_application") and not workday_gate and target_id:
            fallback = await _navigate_and_clear_account_gate(
                browser,
                profile=profile,
                title=title,
                company=company,
                worker_id=worker_id,
                cancel_flag=cancel_flag,
            )
            if fallback.get("attempted"):
                preflight.setdefault("notes", []).append(str(fallback.get("reason") or "LLM navigation fallback"))
                await _wait_for_page_settle(browser, 2.0)
                surface, decision, target_id = await _select_application_tab(browser, baseline_ids)
                workday_gate = is_workday_account_gate(surface)
        current_url = str(surface.get("url") or await _page_url(browser))
        if not decision.get("is_application") and not workday_gate:
            reason = f"Application form not reached; Fapply was not clicked ({decision.get('reason')})"
            update_job(
                url,
                status="failed",
                error=reason,
                manual_review_at=datetime.now(timezone.utc).isoformat(),
                manual_review_url=current_url,
                manual_review_summary={"fapply": {"verified": False, "clicked": False, "surface": decision}},
                manual_review_notes=(preflight.get("notes") or [])[-12:],
            )
            print(f"  ⚠️  [F{worker_id}] {reason}")
            return "not_application"

        if target_id:
            closed = await _close_owned_landing_tabs(browser, baseline_ids, target_id)
            if closed:
                print(f"  🧹 [F{worker_id}] Closed {closed} landing/job-description tab(s)")
        print(
            f"  ✅ [F{worker_id}] Verified application form: "
            f"fields={surface.get('controlCount', 0)} identity={surface.get('identityCount', 0)}"
        )
        if is_workday_application(current_url):
            if deadline is not None:
                # The landing/account phase remains bounded by 60 seconds;
                # the verified multi-page application gets its own fill budget.
                deadline.reschedule(asyncio.get_running_loop().time() + 600.0)
            facts = load_autofill_facts(profile, RESUME_PATH)
            facts.update(job_title=title, job_company=company)
            print(f"  🤖 [F{worker_id}] Workday: deterministic fill → LLM → Save and Continue → Review")
            try:
                workday = await run_workday_deterministic(
                    browser, facts=facts, resume_path=RESUME_PATH, worker_id=worker_id,
                    passes=12, llm_cleanup=True, llm_steps=60, llm_timeout=300.0,
                    profile=profile, cancel_flag=cancel_flag, ignored_target_ids=baseline_ids,
                )
                final_state = await _probe(await _current_page(browser))
                reached_review = bool(final_state.get("review"))
                summary = workday.get("summary") or {}
                reason = None if reached_review else (
                    (summary.get("llm_cleanup") or {}).get("stop_reason")
                    or "; ".join(final_state.get("blockers", []) + final_state.get("errors", []))
                    or "Workday has not reached Review; remaining steps need attention"
                )
                update_job(
                    url, status="manual_review", error=reason,
                    manual_review_at=datetime.now(timezone.utc).isoformat(),
                    manual_review_url=await _page_url(browser),
                    manual_review_summary={**summary, "workday": {"engine": "langhire", "reached_review": reached_review}},
                    manual_review_notes=(preflight.get("notes") or [])[-12:],
                )
                print(f"  📄 [F{worker_id}] {reason or 'Workday Review reached; ready for your final submission'}")
                return "workday_review_ready" if reached_review else "workday_needs_input"
            finally:
                await release_review_handoff(browser)
        else:
            verification = await run_fapply_and_verify(browser)
        current_url = await _page_url(browser)
        summary = {"fapply": {**verification, "surface": decision}}
        if verification.get("verified") or "reached_review" in verification:
            update_job(
                url,
                status="manual_review",
                error=(verification.get("reason") if verification.get("reached_review") is False else None),
                manual_review_at=datetime.now(timezone.utc).isoformat(),
                manual_review_url=current_url,
                manual_review_summary=summary,
                manual_review_notes=(preflight.get("notes") or [])[-12:],
            )
            print(
                f"  ✅ [F{worker_id}] Fapply result: populated "
                f"{int(verification.get('filled_by_fapply') or 0)} field(s); "
                f"{verification.get('reason') or 'tab left open'}"
            )
            if "reached_review" in verification:
                return "workday_review_ready" if verification["reached_review"] else "workday_needs_input"
            return "fapply_verified"

        reason = str(verification.get("reason") or "Fapply did not produce verifiable field changes")
        update_job(
            url,
            status="failed",
            error=reason,
            manual_review_at=datetime.now(timezone.utc).isoformat(),
            manual_review_url=current_url,
            manual_review_summary=summary,
            manual_review_notes=(preflight.get("notes") or [])[-12:],
        )
        print(f"  ⚠️  [F{worker_id}] {reason}; tab left open")
        if not verification.get("clicked"):
            return "fapply_setup_required"
        return "fapply_unverified"
    except Exception as exc:
        current_url = current_url or await _page_url(browser)
        reason = f"Fapply setup error: {type(exc).__name__}: {str(exc)[:300]}"
        update_job(
            url,
            status="failed",
            error=reason,
            manual_review_at=datetime.now(timezone.utc).isoformat(),
            manual_review_url=current_url,
            manual_review_summary={"fapply": {"verified": False, "clicked": False}},
            manual_review_notes=(preflight.get("notes") or [])[-12:],
        )
        print(f"  ⚠️  [F{worker_id}] {reason}; tab left open")
        return "error"


async def _run_job_with_deadline(
    browser: BrowserSession,
    job: dict[str, Any],
    profile: dict[str, Any],
    worker_id: int,
    *,
    cancel_flag: dict | None = None,
    timeout_seconds: float = 60.0,
) -> str:
    """Bound one selected job so a stalled site cannot stop the batch."""
    try:
        async with asyncio.timeout(max(0.01, min(60.0, timeout_seconds))) as deadline:
            return await open_with_fapply(
                browser,
                job,
                profile,
                worker_id,
                cancel_flag=cancel_flag,
                deadline=deadline if timeout_seconds >= 60.0 else None,
            )
    except TimeoutError:
        url = str(job.get("url") or "")
        title = str(job.get("title") or "Unknown")
        company = str(job.get("company") or "Unknown")
        current_url = ""
        try:
            current_url = await asyncio.wait_for(_page_url(browser), timeout=2.0)
        except Exception:
            pass
        reason = "Fapply timed out (60 seconds for navigation, up to 10 minutes for Workday pages); tab left open"
        update_job(
            url,
            status="failed",
            error=reason,
            manual_review_at=datetime.now(timezone.utc).isoformat(),
            manual_review_url=current_url,
            manual_review_summary={
                "fapply": {
                    "verified": False,
                    "reason": reason,
                    "timed_out": True,
                }
            },
        )
        print(f"  ⏱️  [F{worker_id}] {title} at {company}: {reason}")
        return "timed_out"


def _clear_automation_session_restore() -> int:
    removed = 0
    candidates = [
        Path(BROWSER_PROFILE_DIR) / "Default/Current Session",
        Path(BROWSER_PROFILE_DIR) / "Default/Current Tabs",
        Path(BROWSER_PROFILE_DIR) / "Default/Last Session",
        Path(BROWSER_PROFILE_DIR) / "Default/Last Tabs",
    ]
    sessions = Path(BROWSER_PROFILE_DIR) / "Default/Sessions"
    if sessions.is_dir():
        candidates.extend(path for path in sessions.iterdir() if path.is_file())
    for path in candidates:
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


async def run_fapply_queue(
    selected: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    cancel_flag: dict | None = None,
    clear_session_restore: bool = True,
) -> dict[str, int]:
    extension = find_fapply_extension_path()
    if not extension:
        raise RuntimeError(
            "Fapply extension was not found in Brave. Install Fapply first, then restart LangHire."
        )
    if clear_session_restore:
        removed = _clear_automation_session_restore()
        if removed:
            print(f"Cleared {removed} stale automation session-restore file(s).")
    browser = BrowserSession(**fapply_browser_kwargs(extension), keep_alive=True)
    await browser.start()
    print(
        f"Fapply queue ready for {len(selected)} job(s). Deterministic navigation first; "
        "bounded account/navigation help when needed; Fapply fills non-Workday forms; "
        "60 seconds for navigation; Workday uses LangHire deterministic + LLM filling to Review (10 minute limit); "
        "failures skip forward; no final submissions.\n"
    )
    stats: dict[str, int] = {}
    for index, job in enumerate(selected, start=1):
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print("  🛑 Stop requested — no more jobs will be opened")
            break
        status = await _run_job_with_deadline(
            browser,
            job,
            profile,
            index,
            cancel_flag=cancel_flag,
            timeout_seconds=60.0,
        )
        stats[status] = stats.get(status, 0) + 1
        if status == "fapply_setup_required":
            print(
                "  ⏭️  Fapply setup is required on this page; leaving its tab open "
                "and continuing to the next selected job."
            )
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print("  🛑 Queue stopped after preserving the current application tab")
            break
        await asyncio.sleep(0.5)
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description="Open applications and verify Fapply autofill")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    jobs = load_json(JOBS_FILE, {})
    profile = load_json(CANDIDATE_PROFILE, {})
    selected = []
    for url, job in jobs.items():
        if job.get("status") not in {"pending", "failed", "manual_review"}:
            continue
        selected.append({**job, "url": url})
        if len(selected) >= args.limit:
            break
    if not selected:
        print("No jobs are ready for Fapply.")
        return
    stats = await run_fapply_queue(selected, profile)
    print(f"Fapply queue complete: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
