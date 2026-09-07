
"""
Script 2: Apply to collected jobs with multiple concurrent workers.
One agent per job. Tracks status in jobs.json.

Usage:
  uv run python apply_jobs.py                          # 1 worker, easy apply
  uv run python apply_jobs.py --workers 3              # 3 concurrent workers
  uv run python apply_jobs.py --workers 2 --no-easy-apply  # non-easy apply
  uv run python apply_jobs.py --limit 10               # apply to max 10 jobs
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import Agent, BrowserSession

try:
    import core.shared_config as config
    from core.shared_config import (
        BASE_DIR, BROWSER_PROFILE_DIR,
        JOBS_FILE, QA_FILE, CANDIDATE_PROFILE, LOGS_DIR, RESUME_PATH, SENSITIVE_DATA, BLOCKED_DOMAINS,
        AWS_PROFILE, AWS_REGION, MODEL_ID,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        build_memory_context, extract_from_history, normalize_question,
        read_jobs, claim_job, update_job, get_memory_store,
    )
    from core.autofill_facts import (
        format_facts_for_prompt,
        load_autofill_facts,
        probe_workday_human_checkpoint,
        run_static_autofill,
        try_controlled_final_submit,
        wait_for_workday_human_checkpoint,
    )
    from core.workday_flow import (
        WorkdayDeterministicUnavailable,
        _current_page,
        _human_click_timeout_seconds,
        _page_url,
        _pause_for_workday_human_click,
        _wait_for_page_settle,
        is_workday_url,
        run_workday_deterministic,
    )
    from core.config import load_settings
    from memory import extract_learnings_from_markers, extract_learnings_via_llm, store_learnings
    from memory.metrics import MetricsStore
    from core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
except ImportError:
    import backend.core.shared_config as config
    from backend.core.shared_config import (
        BASE_DIR, BROWSER_PROFILE_DIR,
        JOBS_FILE, QA_FILE, CANDIDATE_PROFILE, LOGS_DIR, RESUME_PATH, SENSITIVE_DATA, BLOCKED_DOMAINS,
        AWS_PROFILE, AWS_REGION, MODEL_ID,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        build_memory_context, extract_from_history, normalize_question,
        read_jobs, claim_job, update_job, get_memory_store,
    )
    from backend.core.autofill_facts import (
        format_facts_for_prompt,
        load_autofill_facts,
        probe_workday_human_checkpoint,
        run_static_autofill,
        try_controlled_final_submit,
        wait_for_workday_human_checkpoint,
    )
    from backend.core.workday_flow import (
        WorkdayDeterministicUnavailable,
        _current_page,
        _human_click_timeout_seconds,
        _page_url,
        _pause_for_workday_human_click,
        _wait_for_page_settle,
        is_workday_url,
        run_workday_deterministic,
    )
    from backend.core.config import load_settings
    from backend.memory import extract_learnings_from_markers, extract_learnings_via_llm, store_learnings
    from backend.memory.metrics import MetricsStore
    from backend.core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start

# Lock for thread-safe QA file writes
_qa_lock = asyncio.Lock()


_ERROR_MAP = [
    ("'NoneType' object is not subscriptable", "Browser agent encountered an unexpected page state. The page may have changed or timed out."),
    ("'NoneType' object has no attribute", "Browser agent lost track of a page element. The site may have redirected or loaded slowly."),
    ("net::ERR_", "Network error — the page failed to load. Check your internet connection."),
    ("Timeout", "Operation timed out. The page took too long to respond."),
    ("ERR_CONNECTION_REFUSED", "Could not connect to the website. It may be temporarily down."),
    ("security token", "AWS credentials expired. They will be refreshed automatically on retry."),
    ("rate limit", "API rate limit reached. Wait a moment and try again."),
    ("context was destroyed", "Browser page closed unexpectedly during the application."),
    ("Target page, context or browser has been closed", "Browser closed unexpectedly during the application."),
]


def _friendly_error(raw: str) -> str:
    """Translate raw Python/browser errors into user-readable messages."""
    for pattern, friendly in _ERROR_MAP:
        if pattern.lower() in raw.lower():
            return friendly
    if len(raw) > 200 and ("Traceback" in raw or "Error:" in raw):
        return "Application failed due to an unexpected error. Check the Logs page for details."
    return raw


async def save_job_status(url: str, status: str, error: str | None = None):
    fields: dict = {"status": status}
    if error:
        fields["error"] = _friendly_error(error)
    else:
        fields["error"] = None
    if status == "applied":
        fields["applied_at"] = datetime.now(timezone.utc).isoformat()
    update_job(url, **fields)


async def save_new_qa(new_questions: dict, source_domain: str = ""):
    if not new_questions:
        return
    async with _qa_lock:
        store = get_memory_store()
        if store:
            for q, a in new_questions.items():
                store.qa_add(question=q, answer=a or "", source_domain=source_domain)
        else:
            qa = load_json(QA_FILE, {})
            existing_norms = {normalize_question(k) for k in qa}
            for q, a in new_questions.items():
                if normalize_question(q) not in existing_norms:
                    qa[q] = a
                    existing_norms.add(normalize_question(q))
            save_json(QA_FILE, qa)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _otp_mailbox(email: str) -> tuple[str, str]:
    """Return the right inbox for verification emails."""
    domain = email.split("@", 1)[1].lower().strip() if "@" in email else ""
    microsoft_domains = {
        "vt.edu",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "office365.com",
    }
    if domain in microsoft_domains or domain.endswith(".onmicrosoft.com"):
        return "Outlook", "https://outlook.office.com/mail/"
    if domain in {"gmail.com", "googlemail.com"}:
        return "Gmail", "https://mail.google.com"
    # Many university/work accounts are Microsoft-backed. Use Outlook as the
    # safer default for non-Gmail addresses instead of guessing Gmail.
    return "Outlook", "https://outlook.office.com/mail/"


def _interest_statement(title: str, company: str) -> str:
    company = (company or "your team").strip()
    title = (title or "this role").strip()
    return (
        f"I am excited about {company} because the {title} role is a strong fit for my computer science background, "
        "prior internship experience, and interest in building reliable software with real user impact. I would be "
        "excited to learn from the engineering team while contributing strong fundamentals, curiosity, and a high "
        "ownership mindset."
    )


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


async def _switch_to_tab(browser: BrowserSession, target_id: str) -> None:
    session = await browser.get_or_create_cdp_session(target_id=target_id, focus=True)
    await session.cdp_client.send.Target.activateTarget(params={"targetId": target_id})


async def _close_other_tabs(browser: BrowserSession, worker_id: int, reason: str = "cleanup") -> int:
    """Keep the focused tab only.

    Chrome may restore tabs from the persistent login profile. Those stale tabs
    confuse browser-use and can trigger the "broken tabs" pile-up the user saw.
    """
    keep_target_id = getattr(browser, "agent_focus_target_id", None)
    if not keep_target_id:
        return 0
    closed = 0
    try:
        tabs = await browser.get_tabs()
    except Exception:
        return 0
    for tab in tabs:
        target_id = getattr(tab, "target_id", "")
        if not target_id or target_id == keep_target_id:
            continue
        try:
            await asyncio.wait_for(browser.close_page(target_id), timeout=2.0)
            closed += 1
        except Exception:
            pass
    if closed:
        print(f"  🧹 [W{worker_id}] Closed {closed} stale browser tab(s) during {reason}")
    return closed


async def _wait_for_linkedin_job_surface(browser: BrowserSession, timeout: float = 24.0) -> dict:
    """Wait for LinkedIn job details to render before searching for Apply."""
    async def probe() -> dict:
        page = await _current_page(browser)
        raw = await page.evaluate(
            r"""() => {
              const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden'
                  && style.display !== 'none'
                  && Number(style.opacity || 1) > 0
                  && rect.width > 4
                  && rect.height > 4;
              };
              const body = document.body?.innerText || '';
              const buttons = Array.from(document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')).filter(visible);
              const applyButtons = buttons
                .map((el) => [el.innerText, el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title')].filter(Boolean).join(' '))
                .filter((text) => /\bapply\b/i.test(text));
              return {
                url: location.href,
                ready: document.readyState,
                title: document.title,
                bodyLength: body.length,
                buttonCount: buttons.length,
                applyCount: applyButtons.length,
                applyLabels: applyButtons.slice(0, 8).map((text) => String(text || '').replace(/\s+/g, ' ').trim().slice(0, 120))
              };
            }"""
        )
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    deadline = asyncio.get_event_loop().time() + timeout
    reloaded = False
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        last = await probe()
        ready = str(last.get("ready") or "").lower()
        body_length = int(last.get("bodyLength") or 0)
        button_count = int(last.get("buttonCount") or 0)
        apply_count = int(last.get("applyCount") or 0)
        if ready in {"interactive", "complete"} and (apply_count > 0 or (body_length > 700 and button_count > 2)):
            return last
        if not reloaded and ready == "complete" and body_length < 100 and asyncio.get_event_loop().time() + 12 < deadline:
            try:
                page = await _current_page(browser)
                await page.reload(wait_until="domcontentloaded", timeout=15000)
                reloaded = True
            except Exception:
                reloaded = True
        await asyncio.sleep(0.75)
    return last


async def _wait_for_new_or_redirected_tab(
    browser: BrowserSession,
    before_target_ids: set[str],
    before_url: str,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """Switch to a newly-opened tab when a click spawned one; otherwise keep current tab."""
    deadline = asyncio.get_event_loop().time() + timeout
    selected_new_target = ""
    while asyncio.get_event_loop().time() < deadline:
        tabs = await browser.get_tabs()
        new_tabs = [tab for tab in tabs if tab.target_id not in before_target_ids]
        if new_tabs:
            # Use the most recent page target. LinkedIn often opens about:blank first,
            # then redirects it to the ATS.
            selected_new_target = new_tabs[-1].target_id
            await _switch_to_tab(browser, selected_new_target)
            url = await _wait_for_page_settle(browser, 1.0)
            if url and not url.startswith("about:blank"):
                return True, url

        current_url = await _page_url(browser)
        if current_url and current_url != before_url and not current_url.startswith("about:blank"):
            return False, current_url
        await asyncio.sleep(0.4)

    if selected_new_target:
        await _switch_to_tab(browser, selected_new_target)
        return True, await _page_url(browser)
    return False, await _page_url(browser)


def _click_apply_button_script(mode: str) -> str:
    """Return JS that clicks a visible application-navigation button without touching final submit."""
    return r"""
(mode) => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const norm = (value) => clean(value).toLowerCase();
  const isVisible = (el) => {
    if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity || 1) === 0) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) return false;
    return true;
  };
  const allElements = (selector, root = document, seen = new Set()) => {
    const out = [];
    if (!root || seen.has(root)) return out;
    seen.add(root);
    try { out.push(...Array.from(root.querySelectorAll(selector))); } catch (_) {}
    let nodes = [];
    try { nodes = Array.from(root.querySelectorAll('*')); } catch (_) {}
    for (const node of nodes) {
      if (node.shadowRoot) out.push(...allElements(selector, node.shadowRoot, seen));
    }
    return out;
  };
  const labelFor = (el) => {
    const bits = [
      el.innerText, el.textContent, el.value,
      el.getAttribute('aria-label'), el.getAttribute('title'),
      el.getAttribute('data-control-name'), el.getAttribute('name'),
      el.getAttribute('id'), el.getAttribute('href')
    ];
    return clean(bits.filter(Boolean).join(' '));
  };
  const candidates = allElements(
    'button,a,[role="button"],input[type="button"],input[type="submit"],div[aria-label],span[role="button"]'
  )
    .filter(isVisible)
    .map((el) => ({ el, label: labelFor(el), lower: norm(labelFor(el)) }))
    .filter((item) => item.lower);

  const bad = /(submit final|final submit|finish application|send application|complete application|withdraw|delete|remove|save|saved|share|follow|notify|alert|report|tailor|tailor my resume|resume tools?|job-apply-resources|autofill|auto fill|use my last application|last application|resume autofill|mygreenhouse|my greenhouse|quick apply with|sign in|signin|log in|login|forgot password|reset password|security code|verification code|one[- ]time code|magic link)/i;
  const isBad = (item) => bad.test(item.label);
  const click = (item, reason) => {
    item.el.scrollIntoView({ block: 'center', inline: 'center' });
    item.el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
    item.el.click();
    return { clicked: true, reason, label: item.label.slice(0, 180), url: window.location.href };
  };

  if (mode === 'linkedin_easy') {
    const exact = candidates.find((item) => /\beasy apply\b/i.test(item.label) && !isBad(item));
    if (exact) return click(exact, 'linkedin_easy_apply');
    const fallback = candidates.find((item) => /\bquick apply\b/i.test(item.label) && !isBad(item));
    if (fallback) return click(fallback, 'linkedin_quick_apply');
  }

  if (mode === 'linkedin_external') {
    const strong = candidates.find((item) =>
      (
        /\bapply\b/i.test(item.label) ||
        /\bapply on company (site|website)\b/i.test(item.label)
      ) &&
      !/\beasy apply\b|\bquick apply\b/i.test(item.label) &&
      !isBad(item)
    );
    if (strong) return click(strong, 'linkedin_external_apply');
  }

  if (mode === 'external_apply') {
    const patterns = [
      /\bapply manually\b/i,
      /\bmanual application\b/i,
      /\bcontinue without\b/i,
      /\bapply without\b/i,
      /\bapply as guest\b/i,
      /\bcontinue as guest\b/i,
      /\bapply now\b/i,
      /\bapply to role\b/i,
      /\bapply for this job\b/i,
      /\bapply for this position\b/i,
      /\bapply to this job\b/i,
      /\bstart application\b/i,
      /\bbegin application\b/i,
      /\bcontinue to apply\b/i,
      /\bcontinue application\b/i,
      /\bcreate profile\b/i,
      /\bi'?m interested\b/i,
      /^apply$/i
    ];
    for (const pattern of patterns) {
      const match = candidates.find((item) => pattern.test(item.label) && !isBad(item));
      if (match) return click(match, 'external_apply');
    }
  }

  return {
    clicked: false,
    mode,
    url: window.location.href,
    candidates: candidates.filter((item) => !isBad(item)).slice(0, 24).map((item) => item.label.slice(0, 100))
  };
}
"""


async def _click_apply_button(browser: BrowserSession, mode: str) -> dict:
    page = await _current_page(browser)
    raw = await page.evaluate(_click_apply_button_script(mode), mode)
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {"clicked": False, "raw": raw}
    return parsed if isinstance(parsed, dict) else {"clicked": False, "raw": raw}


async def _probe_application_surface(browser: BrowserSession) -> dict:
    page = await _current_page(browser)
    raw = await page.evaluate(
        r"""() => {
          const text = document.body ? document.body.innerText.toLowerCase() : '';
          const inputs = Array.from(document.querySelectorAll('input, textarea, select'));
          const required = inputs.filter((el) => el.required || el.getAttribute('aria-required') === 'true').length;
          const fileInputs = inputs.filter((el) => String(el.type || '').toLowerCase() === 'file').length;
          return JSON.stringify({
            inputCount: inputs.length,
            required,
            fileInputs,
            looksLikeForm: inputs.length >= 3 || fileInputs > 0 || /first name|last name|email|phone|resume|cv|password|sign in|login|verification|otp/.test(text)
          });
        }"""
    )
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


async def _run_apply_preflight(
    browser: BrowserSession,
    *,
    url: str,
    title: str,
    company: str,
    easy_apply: bool,
    static_facts: dict,
    resume_path: str,
    guard_final_submit: bool,
    worker_id: int,
    close_existing_tabs: bool = True,
    open_in_new_tab: bool = False,
) -> dict:
    """Fast deterministic navigation to the real application surface.

    This intentionally only clicks application-navigation buttons. The agent
    remains responsible for complex login/form reasoning, and submit is still
    protected by the static guard.
    """
    notes: list[str] = []
    result = {
        "attempted": True,
        "started": False,
        "clicked_linkedin": False,
        "clicked_external": False,
        "current_url": "",
        "notes": notes,
        "easy_apply": easy_apply,
    }
    try:
        await browser.start()
        result["started"] = True
        await browser.navigate_to(url, new_tab=open_in_new_tab)
        await _wait_for_page_settle(browser, 2.5)
        if close_existing_tabs:
            await _close_other_tabs(browser, worker_id, "preflight start")
        current = await _page_url(browser)
        result["current_url"] = current
        notes.append(f"opened LinkedIn job: {current}")
        print(f"  ⚡ [W{worker_id}] Preflight opened LinkedIn: {title} at {company}")

        linkedin_surface = await _wait_for_linkedin_job_surface(browser, timeout=24.0)
        if linkedin_surface.get("bodyLength"):
            notes.append(
                "LinkedIn rendered: "
                f"body={linkedin_surface.get('bodyLength')} "
                f"buttons={linkedin_surface.get('buttonCount')} "
                f"apply={linkedin_surface.get('applyCount')}"
            )
        else:
            notes.append(f"LinkedIn did not render usable body before apply search: {linkedin_surface}")

        try:
            await run_static_autofill(browser, static_facts, resume_path, guard_final_submit=guard_final_submit)
        except Exception:
            pass

        before_tabs = await browser.get_tabs()
        before_target_ids = {tab.target_id for tab in before_tabs}
        before_url = await _page_url(browser)
        mode = "linkedin_easy" if easy_apply else "linkedin_external"
        click_result = await _click_apply_button(browser, mode)
        if not click_result.get("clicked") and not easy_apply:
            # Metadata can be stale; LinkedIn sometimes labels external buttons
            # as quick/easy variants. Try the generic easy/quick label once.
            click_result = await _click_apply_button(browser, "linkedin_easy")
        if click_result.get("clicked"):
            result["clicked_linkedin"] = True
            notes.append(f"clicked LinkedIn button: {click_result.get('label', '')}")
            print(f"  ⚡ [W{worker_id}] Preflight clicked LinkedIn button: {click_result.get('label', '')[:80]}")
        else:
            notes.append(f"LinkedIn apply button not found; candidates={click_result.get('candidates', [])[:8]}")
            print(f"  ⚠️  [W{worker_id}] Preflight could not find LinkedIn apply button; agent will fallback")
            return result

        opened_new, current = await _wait_for_new_or_redirected_tab(browser, before_target_ids, before_url, timeout=10)
        if close_existing_tabs:
            await _close_other_tabs(browser, worker_id, "after LinkedIn apply")
        result["current_url"] = current
        if opened_new:
            notes.append(f"switched to new apply tab: {current}")
        else:
            notes.append(f"same-tab apply surface: {current}")

        try:
            autofill_result = await run_static_autofill(browser, static_facts, resume_path, guard_final_submit=guard_final_submit)
            if autofill_result.get("filled") or autofill_result.get("selects") or autofill_result.get("choices"):
                notes.append("static autofill ran after LinkedIn click")
        except Exception:
            pass

        if easy_apply:
            # Easy Apply/Quick Apply lives in a LinkedIn modal. Let static fill
            # it once, then the agent handles Next/review without submitting.
            return result

        initial_form_probe = await _probe_application_surface(browser)
        if initial_form_probe.get("looksLikeForm"):
            notes.append("external page looks like a form/login surface; still checking for an obvious employer apply button")

        for attempt in range(3):
            current = await _page_url(browser)
            if not current:
                break
            lowered = current.lower()
            if "linkedin.com" in lowered and attempt > 0:
                break

            before_tabs = await browser.get_tabs()
            before_target_ids = {tab.target_id for tab in before_tabs}
            before_url = current
            click_result = await _click_apply_button(browser, "external_apply")
            if not click_result.get("clicked"):
                if attempt < 2:
                    notes.append(f"external apply button not found on pass {attempt + 1}; waiting for render")
                    await asyncio.sleep(1.75)
                    continue
                if initial_form_probe.get("looksLikeForm"):
                    notes.append("no additional employer apply button found; current surface is already form/login-like")
                else:
                    notes.append("external apply button not found after retries")
                break
            result["clicked_external"] = True
            notes.append(f"clicked external button: {click_result.get('label', '')}")
            print(f"  ⚡ [W{worker_id}] Preflight clicked external button: {click_result.get('label', '')[:80]}")
            await _wait_for_new_or_redirected_tab(browser, before_target_ids, before_url, timeout=12)
            await _wait_for_page_settle(browser, 5.0)
            try:
                await run_static_autofill(browser, static_facts, resume_path, guard_final_submit=guard_final_submit)
            except Exception:
                pass

            # Once a form, login, or upload surface appears, stop pre-clicking.
            form_probe = await _probe_application_surface(browser)
            if form_probe.get("looksLikeForm"):
                notes.append("stopped preflight on form/login surface")
                break

        result["current_url"] = await _page_url(browser)
        return result
    except Exception as exc:
        notes.append(f"preflight error: {type(exc).__name__}: {str(exc)[:200]}")
        print(f"  ⚠️  [W{worker_id}] Preflight skipped after error: {str(exc)[:120]}")
        return result


async def apply_to_job(
    job: dict,
    profile: dict,
    qa: dict,
    applied_labels: list[str],
    easy_apply: bool,
    worker_id: int,
    resume_path_override: str | None = None,
    dry_run: bool | None = None,
) -> str:
    """Apply to a single job. Returns final status."""
    dry_run = _env_truthy("LANGHIRE_APPLY_DRY_RUN") if dry_run is None else dry_run
    allow_final_submit = _env_truthy("LANGHIRE_ALLOW_FINAL_SUBMIT")
    guard_final_submit = True
    url = job["url"]
    title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")
    dry_label = " DRY RUN" if dry_run else ""
    print(f"  🚀 [W{worker_id}] Starting{dry_label}: {title} at {company}")

    if not config.validate_job_url(url):
        if not dry_run:
            await save_job_status(url, "blocked", "Invalid or internal URL")
        print(f"  🚫 [W{worker_id}] Blocked (invalid URL): {title} at {company}")
        return "blocked"

    if any(domain in url for domain in BLOCKED_DOMAINS):
        if not dry_run:
            await save_job_status(url, "blocked", "Blocked domain")
        print(f"  🚫 [W{worker_id}] Blocked: {title} at {company}")
        return "blocked"

    if not dry_run and not claim_job(url):
        print(f"  ⏭️  [W{worker_id}] Skipped (already claimed): {title} at {company}")
        return "skipped"

    # Always refresh credentials before each job to avoid mid-run expiry
    refresh_credentials()

    llm = config.get_llm()
    # Use the shared browser profile in OS data dir (same as login endpoint)
    browser = BrowserSession(**config.browser_session_kwargs())
    mem_store = get_memory_store()
    # Count memories injected for metrics tracking
    domain = mem_store.extract_domain(url)
    memories_before = mem_store.get_domain_memories(url, limit=50)
    memories_injected_count = len(memories_before) if memories_before else 0
    run_started_at = datetime.now(timezone.utc)

    resume_path = resume_path_override or RESUME_PATH

    # Use tailored resume if available for this job
    try:
        from resume.tailor import get_tailored_resume_path
        tailored_path = get_tailored_resume_path(url)
        if tailored_path:
            resume_path = tailored_path
            print(f"  📄 [W{worker_id}] Using tailored resume: {tailored_path}")
    except ImportError:
        pass

    static_facts = load_autofill_facts(profile, resume_path)
    static_facts["job_title"] = title
    static_facts["job_company"] = company
    static_facts.setdefault("interest_statement", _interest_statement(title, company))
    memory = build_memory_context(profile, qa, applied_labels, job_url=url)
    static_context = format_facts_for_prompt(static_facts)
    if static_context:
        memory = f"{memory}\n\n{static_context}"

    preflight = await _run_apply_preflight(
        browser,
        url=url,
        title=title,
        company=company,
        easy_apply=easy_apply,
        static_facts=static_facts,
        resume_path=resume_path,
        guard_final_submit=guard_final_submit,
        worker_id=worker_id,
    )
    pre_agent_checkpoint = await probe_workday_human_checkpoint(browser)
    if pre_agent_checkpoint.get("required"):
        checkpoint_result = await _pause_for_workday_human_click(
            browser,
            worker_id,
            pre_agent_checkpoint,
        )
        if checkpoint_result.get("timed_out"):
            reason = (
                "Workday human-click checkpoint timed out. "
                "Stopping this job so another pending application can be tried."
            )
            if not dry_run:
                await save_job_status(url, "failed", reason)
            try:
                await browser.kill()
            except Exception:
                try:
                    await browser.close()
                except Exception:
                    pass
            return "dry_failed" if dry_run else "failed"

    workday_result = await _maybe_apply_via_workday_engine(browser, static_facts, resume_path, worker_id)
    if workday_result is not None:
        blockers = ", ".join(workday_result.get("blockers") or [])
        if not dry_run:
            await save_job_status(url, workday_result["status"], error=f"Manual review needed: {blockers}" if blockers else None)
        print(f"  🧭 [W{worker_id}] Workday deterministic engine: {title} at {company} — {blockers or 'ready for review'}")
        return workday_result["status"]

    preflight_notes = "; ".join(preflight.get("notes") or [])
    if preflight.get("clicked_linkedin"):
        current_url = preflight.get("current_url") or ""
        preflight_context = (
            f"PREFLIGHT STATUS: A deterministic no-LLM preflight already opened START_URL and clicked the "
            f"{'LinkedIn Easy/Quick Apply button' if easy_apply else 'LinkedIn external Apply button'}.\n"
            f"Current browser URL is: {current_url}\n"
            f"Preflight notes: {preflight_notes}\n"
            f"Continue from the current page/modal. Do NOT navigate back to START_URL unless the current page is broken, "
            f"blank, or clearly unrelated to EXPECTED_JOB.\n\n"
        )
        initial_actions = []
    else:
        preflight_context = (
            f"PREFLIGHT STATUS: Deterministic preflight did not click the apply button. "
            f"Notes: {preflight_notes or 'none'}\n"
            f"Before doing anything else, navigate to START_URL even if an old browser tab is already open.\n\n"
        )
        initial_actions = [{"navigate": {"url": url, "new_tab": False}}]

    # Profile email is for application forms; credentials email/password are for ATS login.
    # Reload Settings here so UI/local credential edits apply without requiring
    # a Python module reload.
    runtime_sensitive = (load_settings().get("sensitive_data") or {}) or SENSITIVE_DATA
    agent_sensitive_data = {
        "email": profile.get("email", "").strip(),
        "account_email": runtime_sensitive.get("email", "").strip(),
        "password": runtime_sensitive.get("password", ""),
        "first_name": static_facts.get("first_name", ""),
        "last_name": static_facts.get("last_name", ""),
        "full_name": static_facts.get("full_name", ""),
        "phone": static_facts.get("phone_full") or static_facts.get("phone", ""),
    }
    if not agent_sensitive_data["email"]:
        agent_sensitive_data["email"] = agent_sensitive_data["account_email"]
    otp_email = agent_sensitive_data["account_email"] or agent_sensitive_data["email"]
    otp_provider, otp_url = _otp_mailbox(otp_email)

    if preflight.get("clicked_linkedin"):
        login_preamble = (
            f"FIRST — LOGIN CHECK:\n"
            f"1. Use the current browser page/modal produced by PREFLIGHT. Do not navigate to the LinkedIn feed just to check login.\n"
            f"2. If the current page shows a LinkedIn login page instead of the job/application page, WAIT for the user to log in manually. "
            f"Check every 15 seconds (refresh). Wait up to 5 minutes.\n"
            f"3. Do NOT open email during the login check. Only use {otp_provider} later if an OTP/verification-code page appears.\n\n"
        )
    else:
        login_preamble = (
            f"FIRST — LOGIN CHECK:\n"
            f"1. Start from START_URL. Do not navigate to the LinkedIn feed just to check login.\n"
            f"2. If START_URL shows a LinkedIn login page instead of the job details page, WAIT for the user to log in manually. "
            f"Check every 15 seconds (refresh). Wait up to 5 minutes.\n"
            f"3. Do NOT open email during the login check. Only use {otp_provider} later if an OTP/verification-code page appears.\n\n"
        )

    otp_instructions = (
        "\n\nOTP/VERIFICATION CODES: If ANY site asks for a verification code, OTP, or 2FA token:\n"
        "1. Do not guess or invent the code.\n"
        "2. Do not enter placeholder codes such as 000000 or 123456.\n"
        f"3. Stop and report that a human must retrieve the code from {otp_provider}: {otp_url}.\n"
        "4. The static copilot will also stop the run automatically when it detects verification-code inputs."
    )

    if easy_apply:
        if preflight.get("clicked_linkedin"):
            easy_apply_start = (
                f"THEN: Continue the already-open LinkedIn Easy Apply / Quick Apply form for {title} at {company}. "
                f"Do not close the modal or navigate back to the job listing unless the modal is broken. "
            )
        else:
            easy_apply_start = (
                f"THEN: Go to {url} on LinkedIn. Click Easy Apply and complete the application. "
            )
        apply_instructions = (
            f"{login_preamble}"
            f"{easy_apply_start}"
            f"Use resume at {resume_path}. Auto-fill all fields from candidate profile."
            f"{otp_instructions}"
        )
    else:
        has_password = bool(agent_sensitive_data.get("password", "").strip())
        password_note = ""
        known_account_note = (
            "- On Workday Create Account or Sign In forms, let static autofill prepare the credentials and privacy checkbox. "
            "If a 'LangHire paused' banner appears, take no action: the runner is waiting for the user to make that one "
            "protected account click and will resume you automatically afterward.\n"
        )
        if not has_password:
            password_note = (
                "\n\nIMPORTANT: No password is configured in Settings. If the external site requires "
                "account creation or login:\n"
                "1. First check if you can apply as a guest (without creating an account)\n"
                "2. Try 'Sign in with LinkedIn' or 'Sign in with Google' buttons\n"
                "3. If no guest/SSO option exists, stop and report that account credentials are needed. "
                "Do not invent or generate a password.\n"
            )

        if preflight.get("clicked_linkedin"):
            linkedin_flow = (
                "LINKEDIN APPLY FLOW:\n"
                "- Already completed by deterministic preflight. Do not repeat the LinkedIn Apply click and do not go "
                "back to LinkedIn unless the current page is broken, blank, or unrelated.\n\n"
            )
        else:
            linkedin_flow = (
                f"REQUIRED LINKEDIN APPLY FLOW:\n"
                f"1. Wait until the LinkedIn job details page is fully loaded.\n"
                f"2. Find the main blue button in the job details panel. It should say 'Apply', 'Apply on company site', "
                f"or similar. Do NOT click job cards, company links, share/save buttons, or recommendations.\n"
                f"3. Click that main blue Apply button exactly once.\n"
                f"4. If a new tab/window opens, switch to the newest non-blank tab. If it opens about:blank first, wait "
                f"up to 8 seconds for it to redirect.\n"
                f"5. Once an external employer/ATS page is open, stay there. Do NOT go back to LinkedIn unless the external "
                f"page is broken or blocked.\n\n"
            )

        if preflight.get("clicked_external"):
            external_goal = (
                "- Preflight already clicked the first employer-side Apply/Start button. Continue from the current "
                "login/form surface. If another non-final Apply/Continue/Create profile button is visible, use it once; "
                "otherwise start filling or logging in.\n"
            )
        else:
            external_goal = (
                "- Your first goal on the external site is to reach the actual application form.\n"
            )

        apply_instructions = (
            f"{login_preamble}"
            f"THEN: Continue the external employer application for {title} at {company}.\n\n"
            f"{linkedin_flow}"
            f"REQUIRED EXTERNAL SITE APPLY FLOW:\n"
            f"{external_goal}"
            f"- Look for non-final buttons/links with text like: Apply, Apply Now, Start Application, Continue, "
            f"Continue to Apply, I'm interested, Begin, Next, or Create profile.\n"
            f"- On Workday-style start pages, choose Apply Manually. Do not choose Autofill with Resume, "
            f"Use My Last Application, or any resume-autofill shortcut.\n"
            f"{known_account_note}"
            f"- Check both the top and bottom of the page. Scroll down through the job description; many ATS sites put "
            f"Apply near the bottom or in a sticky header.\n"
            f"- If you land on a careers search results page, search or filter for the exact job title '{title}' and company "
            f"'{company}', open the matching job detail page, then click its Apply button.\n"
            f"- Cookie/consent banners are handled by the static layer. Never click Reject Cookies, Decline Cookies, "
            f"or Deny Cookies; if a banner remains, use Accept/Agree/Allow All only.\n"
            f"- If you land on a login page, prefer guest apply or SSO. If login/account creation is required, use the "
            f"configured account credentials below.\n"
            f"- If a site rejects the configured email/password, stop and report a human credential blocker. Do not click "
            f"Forgot password, Reset password, Resend email, or trigger account recovery during a job-application dry run.\n"
            f"- If a button does not work after 2 clicks, try a different visible Apply/Next/Continue control or scroll; "
            f"do not keep clicking the same failed element.\n"
            f"- If you cannot reach a form after two complete passes over the external page, call done with success=false "
            f"and explain exactly where you got stuck.\n\n"
            f"FORM FILLING:\n"
            f"- A deterministic static autofill layer runs after every browser step for name, email, phone, address, "
            f"education, work authorization, sponsorship, login email/password, confirm password, and resume upload.\n"
            f"- Treat any field already filled by static autofill as LOCKED. Do not clear it, rewrite it, or replace it "
            f"with a paraphrase.\n"
            f"- Use resume at {resume_path}. The static layer will upload the resume when it finds an obvious resume/CV file input.\n"
            f"- Only use the LLM for fields that are still empty after static autofill and cannot be answered from saved Q&A/memory.\n"
            f"- Answer work authorization, sponsorship, veteran, disability, demographic, education, experience, phone, "
            f"address, and eligibility questions from the candidate profile and memory.\n"
            f"- Do not leave visible required text boxes unattended. If a textbox remains empty and the answer is knowable "
            f"from profile/resume/Q&A, fill it. If the answer is not safely knowable, stop and report the blocker.\n"
            f"- Do not submit the final application unless the app is configured to do so; otherwise stop at the review "
            f"page and report what is ready for user review.\n\n"
            f"EMAIL USAGE:\n"
            f"- For APPLICATION FORM fields (contact email, email address, etc.): use <secret>email</secret>\n"
            f"- For LOGGING IN or CREATING ACCOUNTS on external ATS sites: use <secret>account_email</secret> and <secret>password</secret>\n"
            f"{password_note}"
            f"If it's a video funnel or recruitment pitch, report failure and stop. "
            f"If the external form is broken after 3 attempts, report failure and stop.\n\n"
            f"BLOCKED SITES — if redirected to any of these, immediately call done with success=false: {', '.join(BLOCKED_DOMAINS)}"
            f"{otp_instructions}"
        )

    _agent_log_start("apply", f"{title} at {company}")

    MAX_STEPS = int(os.environ.get("LANGHIRE_APPLY_MAX_STEPS", "70"))
    _step_count = {"n": 0}
    _blocked_submit = {"value": None}
    _fatal_stop = {"reason": None}
    _controlled_submit_requested = {"value": False}
    _otp_reported = {"value": False}
    _last_static_issue = {"sig": ""}
    _credential_error_count = {"n": 0}
    _controlled_submit_result = {"value": None}

    async def _on_step_with_limit(browser_state, agent_output, step_num):
        _agent_on_step(browser_state, agent_output, step_num)
        _step_count["n"] += 1
        try:
            autofill_result = await run_static_autofill(
                browser,
                static_facts,
                resume_path,
                guard_final_submit=guard_final_submit,
            )
            while (autofill_result.get("human_checkpoint") or {}).get("required"):
                checkpoint_result = await _pause_for_workday_human_click(
                    browser,
                    worker_id,
                    autofill_result["human_checkpoint"],
                )
                if checkpoint_result.get("timed_out"):
                    _fatal_stop["reason"] = (
                        "Workday human-click checkpoint timed out. "
                        "Stopping this job so another pending application can be tried."
                    )
                    return
                # Fill the page revealed by the human click before giving
                # control back to the AI, and catch any immediately chained
                # Workday account checkpoint.
                autofill_result = await run_static_autofill(
                    browser,
                    static_facts,
                    resume_path,
                    guard_final_submit=guard_final_submit,
                )
            guard = autofill_result.get("submit_guard") or {}
            if isinstance(guard, dict) and guard.get("blocked"):
                _blocked_submit["value"] = guard["blocked"]
                if dry_run or not allow_final_submit:
                    _fatal_stop["reason"] = f"No-submit guard blocked final submission: {guard['blocked']}"
                else:
                    _controlled_submit_requested["value"] = True
            missing_password_fields = int(autofill_result.get("passwordFieldsWithoutPassword") or 0)
            if missing_password_fields and not static_facts.get("account_password"):
                _fatal_stop["reason"] = (
                    "Password field is visible, but no account password is configured. "
                    "Human login or Settings password is required."
                )
            if autofill_result.get("verificationCodeRequired") and not _otp_reported["value"]:
                _otp_reported["value"] = True
                print(f"    🔐 [W{worker_id}] Verification code requested — use {otp_provider}: {otp_url}")
                if not _env_truthy("LANGHIRE_ALLOW_OTP_AUTOMATION"):
                    _fatal_stop["reason"] = f"Verification code required. Open {otp_provider} at {otp_url} to retrieve it."
            filled = int(autofill_result.get("filled") or 0)
            restored = int(autofill_result.get("restored") or 0)
            selects = int(autofill_result.get("selects") or 0)
            choices = int(autofill_result.get("choices") or 0)
            required_empty = int(autofill_result.get("requiredEmpty") or 0)
            invalid_fields = int(autofill_result.get("invalidFields") or 0)
            needs_llm = autofill_result.get("needsLlm") or []
            visible_errors = autofill_result.get("visibleErrors") or []
            required_labels = autofill_result.get("requiredEmptyLabels") or []
            credential_error = bool(autofill_result.get("credentialError"))
            if credential_error:
                _credential_error_count["n"] += 1
                _fatal_stop["reason"] = (
                    "The external login rejected the configured email/password. "
                    "Stopping to avoid account lockout or password-reset side effects."
                )
            cookie = autofill_result.get("cookie_helper") or {}
            upload = autofill_result.get("resume_upload") or {}
            uploaded = int(upload.get("uploaded") or 0) if isinstance(upload, dict) else 0
            cookie_clicked = bool(cookie.get("clicked")) if isinstance(cookie, dict) else False
            reject_blocked = bool(cookie.get("blockedReject")) if isinstance(cookie, dict) else False
            if filled or restored or selects or choices or uploaded or cookie_clicked or reject_blocked:
                cookie_note = ""
                if cookie_clicked:
                    cookie_note = f", cookie={str(cookie.get('label', 'accepted'))[:40]}"
                elif reject_blocked:
                    cookie_note = ", cookie_reject_blocked=1"
                print(
                    f"    🧩 [W{worker_id}] Static autofill: "
                    f"text={filled}, restored={restored}, selects={selects}, choices={choices}, uploads={uploaded}{cookie_note}"
                )
            issue_sig = json.dumps(
                {
                    "required": required_labels[:5],
                    "errors": visible_errors[:5],
                    "needs": needs_llm[:5],
                    "invalid": invalid_fields,
                    "credential": credential_error,
                },
                sort_keys=True,
            )
            if (required_empty or invalid_fields or visible_errors or needs_llm or credential_error) and issue_sig != _last_static_issue["sig"]:
                _last_static_issue["sig"] = issue_sig
                pieces = []
                if required_empty:
                    pieces.append(f"required_blank={required_empty}: {required_labels[:3]}")
                if invalid_fields:
                    pieces.append(f"invalid={invalid_fields}")
                if visible_errors:
                    pieces.append(f"errors={visible_errors[:3]}")
                if credential_error:
                    pieces.append("credential_error=1")
                if needs_llm:
                    pieces.append(f"needs_llm={needs_llm[:3]}")
                print(f"    🧭 [W{worker_id}] Copilot review: " + " | ".join(pieces))
            agent_text = " ".join(
                str(getattr(agent_output, attr, "") or "")
                for attr in ("memory", "next_goal", "evaluation_previous_goal")
            ).lower()
            ready_for_submit_language = any(
                phrase in agent_text
                for phrase in (
                    "all required fields are filled",
                    "all required fields have been filled",
                    "all required fields are now filled",
                    "next step is to call done",
                    "final submission button is blocked",
                    "only remaining action",
                    "apply button is visible but should not be clicked",
                    "final submit button is visible",
                    "all other visible required fields appear to be filled",
                    "determine if the form is complete",
                )
            )
            optional_section_loop_language = (
                any(
                    phrase in agent_text
                    for phrase in (
                        "repeatedly scrolling",
                        "scrolling to find",
                        "not visible despite",
                        "could not be found through scrolling",
                        "fields are not visible",
                        "fields remain elusive",
                        "searching for",
                    )
                )
                and any(
                    phrase in agent_text
                    for phrase in (
                        "disability status",
                        "voluntary self-identification",
                        "voluntary self identification",
                    )
                )
            )
            repeated_form_failure_language = any(
                phrase in agent_text
                for phrase in (
                    "previous attempts to fill these fields have failed",
                    "repeated failures",
                    "element index issue",
                    "element index was not available",
                    "index was not available",
                    "dropdown did not contain",
                    "dropdowns are not populating",
                    "stuck on",
                    "same field",
                    "could not be populated",
                )
            )
            has_hard_static_blocker = bool(
                required_empty
                or invalid_fields
                or visible_errors
                or credential_error
                or autofill_result.get("verificationCodeRequired")
            )
            if repeated_form_failure_language and _step_count["n"] >= 10 and has_hard_static_blocker:
                _fatal_stop["reason"] = (
                    "Repeated required-field/dropdown failure detected. "
                    "Stopping this job so another pending application can be tried."
                )
            if repeated_form_failure_language and _step_count["n"] >= 10 and not has_hard_static_blocker:
                _controlled_submit_requested["value"] = True
            if optional_section_loop_language and _step_count["n"] >= 10 and not has_hard_static_blocker:
                _controlled_submit_requested["value"] = True
            if (
                allow_final_submit
                and not dry_run
                and _step_count["n"] >= 3
                and ready_for_submit_language
                and not has_hard_static_blocker
            ):
                _controlled_submit_requested["value"] = True
            if (
                allow_final_submit
                and not dry_run
                and _controlled_submit_requested.get("value")
                and _controlled_submit_result.get("value") is None
                and _step_count["n"] >= 10
                and not has_hard_static_blocker
            ):
                _controlled_submit_result["value"] = await try_controlled_final_submit(browser, static_facts, resume_path)
        except Exception as autofill_err:
            print(f"    ⚠️  [W{worker_id}] Static autofill skipped: {str(autofill_err)[:120]}")
        if _step_count["n"] >= MAX_STEPS:
            raise Exception(f"Reached maximum of {MAX_STEPS} steps — stopping to avoid wasting tokens")

    async def _should_stop_for_static_blocker():
        return bool(_fatal_stop.get("reason") or _controlled_submit_result.get("value") is not None)

    async def _on_done_with_controlled_submit(history):
        _agent_on_done(history)
        try:
            success = bool(history.is_successful()) if hasattr(history, "is_successful") else False
        except Exception:
            success = False
        if dry_run or not allow_final_submit or not success:
            return
        _controlled_submit_result["value"] = await try_controlled_final_submit(browser, static_facts, resume_path)

    if dry_run:
        run_mode_instructions = (
            "DRY RUN ONLY. Do not submit or send the final application. Stop at the review/final page and "
            "report what is ready for human review. If all required fields are filled and the only remaining "
            "action is Submit/Submit application/Send application, call done with success=true instead of clicking it. "
            "On Lever, Greenhouse, Ashby, Workday, Oracle, and similar ATS pages, 'Submit application' is a final submission button, "
            "not a review-page navigation button.\n\n"
        )
    elif allow_final_submit:
        run_mode_instructions = (
            "SUBMIT MODE WITH CODE SAFETY GATE. Fill the application, but do not click the final Submit/Submit application/"
            "Send application/Finish application button yourself. Stop or call done with success=true when all required "
            "fields are filled and the only remaining action is final submission. A deterministic code gate will validate "
            "the page and perform the final click. If you click final submit early, the guard will block it and the code "
            "gate will decide whether it is actually safe to submit.\n\n"
        )
    else:
        run_mode_instructions = (
            "REVIEW MODE. Do not submit or send the final application. Stop at the review/final page and report what is "
            "ready for human review.\n\n"
        )

    agent = Agent(
        task=(
            run_mode_instructions +
            f"START_URL: {url}\n"
            f"EXPECTED_JOB: {title} at {company}\n"
            f"{preflight_context}"
            f"{apply_instructions}\n\n"
            f"HYBRID AUTOFILL DISCIPLINE:\n"
            f"- Static autofill runs in code after each step like a lightweight Simplify-style copilot. It owns stable fields like name, email, phone, address, "
            f"school, degree, graduation, LinkedIn URL, account email/password, confirm password, work authorization, "
            f"sponsorship, relocation, and resume upload.\n"
            f"- Static autofill fills common fields in order before you act. Your job is to resolve only the remaining fields marked "
            f"data-hybrid-needs-llm, data-hybrid-required-empty, or data-hybrid-invalid-field.\n"
            f"- If a 'LangHire paused — click Create Account/Sign In' banner is visible, take no action. The code has frozen "
            f"your run until the user performs that protected Workday account click.\n"
            f"- Never click a control marked data-hybrid-final-submit-blocked. That means the final-submit guard identified it as a final submission button.\n"
            f"- Never click buttons labeled Submit, Submit application, Send application, Finish application, or Complete application yourself. "
            f"Call done(success=true) if that final submit button is the only remaining action; the code gate handles the click in submit mode.\n"
            f"- Do not rewrite fields marked data-static-autofilled or fields that already contain the correct static value.\n"
            f"- Do not manually type static-owned fields like first name, last name, email, phone, address, school, GPA, "
            f"work authorization, OPT/CPT, sponsorship, veteran/disability, gender, race, or resume upload unless they "
            f"remain visibly empty after a static autofill pass.\n"
            f"- Before pressing Next/Continue, scan the visible page for required blanks and validation errors. If a field is "
            f"marked data-hybrid-required-empty or aria-invalid=true, fix that before moving forward.\n"
            f"- If the page shows an error message, read the exact error text and change strategy. For location/autocomplete "
            f"errors like 'not recognized' or 'select a valid option', clear the field, type the location again, then choose "
            f"the matching dropdown option; do not keep retyping free text.\n"
            f"- If the exact error says wrong/invalid email or password, stop and report a credential blocker immediately. "
            f"Do not reset the password or send recovery emails.\n"
            f"- If an input action for a static-owned field fails because the element index is stale/unavailable, do not "
            f"retry that same index more than once. Wait one step for static autofill or move to a different visible field.\n"
            f"- If the static layer leaves a visible textbox empty, handle it before moving on: use saved Q&A/memory/profile "
            f"first, then LLM reasoning only if no static answer exists.\n"
            f"- Never change a truthful static answer just to make it sound better.\n\n"
            f"PERSISTENCE & EFFICIENCY:\n"
            f"- Try at least 3 DIFFERENT approaches before reporting failure.\n"
            f"- If an element doesn't respond after 2-3 clicks, try a completely different method (keyboard, scrolling, different selector).\n"
            f"- Do NOT repeat the same failing action more than 3 times — switch strategies.\n"
            f"- If you've been stuck on the same form field for more than 5 steps, skip it or call done with success=false.\n"
            f"- You have a maximum of 70 steps total. Budget your steps wisely.\n"
            f"- Work through visible fields top-to-bottom in one pass. Before acting on a field, check if it already has a "
            f"value — if so, skip it and move to the next field below it. Never re-click or retype a field that already "
            f"has a value, and never bounce back to an earlier field once you've moved past it.\n\n"
            f"TRACKING: Include in memory field after submission:\n"
            f'@@JOB_APPLIED: {{"title": "{title}", "company": "{company}", "location": "{job.get("location", "")}"}}\n'
            f"For each form question: @@QUESTION: {{\"question\": \"...\", \"answer\": \"...\", \"type\": \"...\"}}"
        ),
        llm=llm,
        use_vision=True,
        llm_call_timeout=300,  # 5 minutes per step
        step_timeout=max(360, int(_human_click_timeout_seconds()) + 60),
        max_failures=10,
        loop_detection_enabled=True,
        loop_detection_window=5,
        browser_session=browser,
        initial_actions=initial_actions,
        sensitive_data=agent_sensitive_data,
        extend_system_message=memory,
        available_file_paths=[resume_path],
        include_attributes=[
            "id", "name", "type", "role", "aria-label", "placeholder", "autocomplete",
            "value", "required", "aria-required", "aria-invalid",
            "data-static-autofilled", "data-hybrid-needs-llm", "data-hybrid-needs-llm-reason",
            "data-hybrid-required-empty", "data-hybrid-invalid-field", "data-hybrid-visible-error",
            "data-hybrid-final-submit-blocked",
        ],
        save_conversation_path=str(LOGS_DIR / f"apply_{company.replace(' ', '_')}_{title.replace(' ', '_')[:30]}"),
        calculate_cost=True,
        register_new_step_callback=_on_step_with_limit,
        register_done_callback=_on_done_with_controlled_submit,
        register_should_stop_callback=_should_stop_for_static_blocker,
    )

    try:
        result = await agent.run()

        if _controlled_submit_result.get("value") is not None:
            final = _controlled_submit_result["value"]
            if final.get("submitted"):
                await save_job_status(url, "applied")
                print(f"  ✅ [W{worker_id}] Controlled submit: {title} at {company} ({final.get('label', 'submit')})")
                return "applied"
            reason = final.get("reason") or "controlled submit validation failed"
            await save_job_status(url, "failed", json.dumps(final, default=str)[:2000])
            print(f"  ❌ [W{worker_id}] Controlled submit blocked: {title} at {company} — {reason}")
            return "failed"

        if _controlled_submit_requested.get("value") and not dry_run and allow_final_submit:
            final = await try_controlled_final_submit(browser, static_facts, resume_path)
            if final.get("submitted"):
                await save_job_status(url, "applied")
                print(f"  ✅ [W{worker_id}] Controlled submit: {title} at {company} ({final.get('label', 'submit')})")
                return "applied"
            reason = final.get("reason") or "controlled submit validation failed"
            await save_job_status(url, "failed", json.dumps(final, default=str)[:2000])
            print(f"  ❌ [W{worker_id}] Controlled submit blocked: {title} at {company} — {reason}")
            return "failed"

        # Extract Q&A from history
        if _fatal_stop.get("reason"):
            print(f"  🛑 [W{worker_id}] Static blocker: {_fatal_stop['reason']}")
            return "dry_failed" if dry_run else "failed"

        # Extract Q&A from history
        _, new_questions = extract_from_history(result)
        domain = ""
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).hostname or ""
            if domain.startswith("www."):
                domain = domain[4:]
        except Exception:
            pass
        await save_new_qa(new_questions, source_domain=domain)

        # Determine success/failure
        success = result.is_successful()
        result_errors = [str(e) for e in (result.errors() or []) if e]
        judge = result.judgement() if hasattr(result, "judgement") else None
        if isinstance(judge, dict) and judge.get("verdict") is False:
            success = False
            judge_reason = judge.get("failure_reason") or judge.get("reasoning") or "Judge marked the run as failed"
            result_errors.append(str(judge_reason))

        # ── Memory extraction (self-learning) ─────────────────────────────
        mem_store = get_memory_store()
        # 1. Marker-based: extract @@LEARNING tags the agent emitted
        marker_learnings = extract_learnings_from_markers(result)
        if marker_learnings:
            store_learnings(mem_store, marker_learnings, url, success)
        # 2. LLM-based: use the configured LLM to summarise the run into procedural learnings
        llm_learnings = []
        try:
            def _llm_call(prompt):
                """Use the user's configured LLM for memory extraction."""
                import asyncio
                from browser_use.llm.messages import UserMessage
                extraction_llm = config.get_llm()
                loop = asyncio.new_event_loop()
                try:
                    resp = loop.run_until_complete(
                        asyncio.wait_for(
                            extraction_llm.ainvoke([UserMessage(content=prompt)]),
                            timeout=30,
                        )
                    )
                    return resp.completion if hasattr(resp, 'completion') else (resp.content if hasattr(resp, 'content') else str(resp))
                finally:
                    loop.close()

            if not dry_run:
                llm_learnings = extract_learnings_via_llm(
                    result, job_url=url, job_title=title, success=success,
                    llm_call=_llm_call,
                )
                if llm_learnings:
                    store_learnings(mem_store, llm_learnings, url, success)
        except Exception as mem_err:
            print(f"    ⚠️  [W{worker_id}] Memory extraction failed (non-fatal): {mem_err}")

        # ── Record metrics ────────────────────────────────────────────────
        run_finished_at = datetime.now(timezone.utc)
        memories_extracted_count = len(marker_learnings) + len(llm_learnings)
        try:
            MetricsStore().record_run(
                job_url=url, job_title=title, company=company,
                website_domain=domain, ats_platform=mem_store.detect_ats_platform(domain),
                success=success, started_at=run_started_at, finished_at=run_finished_at,
                step_count=len(result.history),
                memories_injected=memories_injected_count,
                memories_extracted=memories_extracted_count,
                error_message=(result_errors[-1][:2000] if result_errors else None) if not success else None,
            )
        except Exception as metrics_err:
            print(f"    ⚠️  [W{worker_id}] Metrics recording failed (non-fatal): {metrics_err}")

        if success:
            if dry_run:
                print(f"  🧪 [W{worker_id}] Dry-run reached success state: {title} at {company}")
                return "dry_success"
            elif allow_final_submit:
                final = await try_controlled_final_submit(browser, static_facts, resume_path)
                if final.get("submitted"):
                    await save_job_status(url, "applied")
                    print(f"  ✅ [W{worker_id}] Controlled submit: {title} at {company} ({final.get('label', 'submit')})")
                    return "applied"
                error_msg = json.dumps(final, default=str)[:2000]
                await save_job_status(url, "failed", error_msg)
                print(f"  ❌ [W{worker_id}] Controlled submit blocked: {title} at {company} — {final.get('reason', 'validation failed')}")
                return "failed"
            else:
                await save_job_status(url, "applied")
                print(f"  ✅ [W{worker_id}] Applied: {title} at {company}")
                return "applied"
        else:
            error_msg = result_errors[-1] if result_errors else "Agent reported failure"
            if dry_run:
                print(f"  🧪 [W{worker_id}] Dry-run failed: {title} at {company} — {error_msg[:100]}")
                return "dry_failed"
            await save_job_status(url, "failed", error_msg[:2000])
            print(f"  ❌ [W{worker_id}] Failed: {title} at {company} — {error_msg[:100]}")
            return "failed"

    except Exception as e:
        error_str = str(e)
        if (_controlled_submit_requested.get("value") or _blocked_submit["value"]) and not dry_run and allow_final_submit:
            final = await try_controlled_final_submit(browser, static_facts, resume_path)
            if final.get("submitted"):
                await save_job_status(url, "applied")
                print(f"  ✅ [W{worker_id}] Controlled submit after guard block: {title} at {company} ({final.get('label', 'submit')})")
                return "applied"
            await save_job_status(url, "failed", json.dumps(final, default=str)[:2000])
            print(f"  ❌ [W{worker_id}] Controlled submit blocked after guard block: {title} at {company} — {final.get('reason', 'validation failed')}")
            return "failed"
        if _fatal_stop.get("reason"):
            print(f"  🛑 [W{worker_id}] Static blocker: {_fatal_stop['reason']}")
            return "dry_failed" if dry_run else "failed"
        if _blocked_submit["value"] or "No-submit guard blocked" in error_str:
            print(f"  🛑 [W{worker_id}] Ready for review; submit blocked: {title} at {company}")
            return "ready_for_review" if dry_run else "failed"
        if "security token" in error_str.lower() or "expired" in error_str.lower():
            refresh_credentials()
            if not dry_run:
                await save_job_status(url, "pending", "credentials_expired_retry")
            print(f"  🔑 [W{worker_id}] Credentials expired on: {title} at {company} — refreshed, will retry")
            return "retry"
        if not dry_run:
            await save_job_status(url, "failed", error_str[:2000])
        print(f"  ❌ [W{worker_id}] Error: {title} at {company} — {error_str[:100]}")
        return "dry_failed" if dry_run else "failed"
    finally:
        try:
            # browser_use.close()/stop() only detaches from the browser process.
            # With our persistent login profile that leaves old LinkedIn/ATS tabs
            # around, and the next job inherits a pile of stale/broken tabs.
            await browser.kill()
        except Exception as close_err:
            print(f"    ⚠️  [W{worker_id}] Browser kill error: {close_err}")
            try:
                await browser.close()
            except Exception as final_close_err:
                print(f"    ⚠️  [W{worker_id}] Browser cleanup error: {final_close_err}")


async def worker(
    name: str,
    worker_id: int,
    queue: asyncio.Queue,
    profile: dict,
    qa: dict,
    applied_labels: list,
    easy_apply: bool,
    stats: dict,
    cancel_flag: dict | None = None,
    dry_run: bool | None = None,
):
    """Worker that pulls jobs from queue and applies."""
    while True:
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print(f"  🛑 [{name}] Stop requested — halting")
            break
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        job_easy_apply = job.get("easy_apply", easy_apply) if job.get("easy_apply") is not None else easy_apply
        status = await apply_to_job(job, profile, qa, applied_labels, job_easy_apply, worker_id, dry_run=dry_run)
        stats[status] = stats.get(status, 0) + 1

        # On retry, put back in queue
        if status == "retry":
            queue.put_nowait(job)

        queue.task_done()


async def main():
    parser = argparse.ArgumentParser(description="Apply to collected jobs")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent workers")
    parser.add_argument("--limit", type=int, help="Max jobs to process")
    parser.add_argument("--easy-apply", dest="easy_apply", action="store_true", default=True)
    parser.add_argument("--no-easy-apply", dest="easy_apply", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Fill/review applications but do not submit or update job statuses")
    args = parser.parse_args()

    jobs = load_json(JOBS_FILE, {})
    profile = load_json(CANDIDATE_PROFILE, {})
    qa = load_json(QA_FILE, {})
    LOGS_DIR.mkdir(exist_ok=True)

    # Filter pending jobs by type
    pending = [
        j for j in jobs.values()
        if j.get("status") == "pending"
        and (j.get("easy_apply") is True) == args.easy_apply
    ]

    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        print("No pending jobs to apply to. Run collect_jobs.py first.")
        return

    applied_labels = [
        f"{j.get('title','')} at {j.get('company','')}"
        for j in jobs.values() if j.get("status") == "applied"
    ]

    mode = "Easy Apply" if args.easy_apply else "Non-Easy Apply"
    dry = " (dry run; no submit/status update)" if args.dry_run else ""
    print(f"Applying to {len(pending)} {mode} jobs with {args.workers} worker(s){dry}\n")

    queue = asyncio.Queue()
    for job in pending:
        queue.put_nowait(job)

    stats = {}
    num_workers = min(args.workers, len(pending))

    # Background credential refresh every 14 min — auto-cancelled when workers finish
    cred_task = asyncio.create_task(credential_refresh_loop(14))

    workers = []
    for i in range(num_workers):
        if i > 0:
            await asyncio.sleep(5)  # stagger browser launches
        workers.append(
            asyncio.create_task(worker(
                f"W{i+1}",
                i+1,
                queue,
                profile,
                qa,
                applied_labels,
                args.easy_apply,
                stats,
                dry_run=args.dry_run,
            ))
        )
    await asyncio.gather(*workers)
    cred_task.cancel()

    print(f"\n{'='*60}")
    print(f"Results: {stats}")
    total_applied = sum(1 for j in load_json(JOBS_FILE, {}).values() if j.get("status") == "applied")
    print(f"Total applied across all runs: {total_applied}")

    # Memory stats
    mem_stats = get_memory_store().get_stats()
    print(f"🧠 Agent memory: {mem_stats['total_memories']} memories across {mem_stats['unique_domains']} domains")


if __name__ == "__main__":
    asyncio.run(main())
