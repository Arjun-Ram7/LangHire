"""
Script 1: Collect job links and descriptions from LinkedIn.
Searches each target job title, collects job URLs with metadata, then fetches
full job descriptions for each collected job. Saves everything to jobs.json.

Usage:
  uv run python collect_jobs.py                    # collect for all titles
  uv run python collect_jobs.py --title "Data Analyst"  # single title
  uv run python collect_jobs.py --resume           # skip already-collected titles
  uv run python collect_jobs.py --skip-descriptions # skip description fetching phase
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import BrowserSession

try:
    import core.shared_config as config
    from core.shared_config import (
        JOBS_FILE, CANDIDATE_PROFILE, LOGS_DIR, BASE_DIR, BROWSER_PROFILE_DIR,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        read_jobs, write_jobs, update_job, upsert_job as atomic_upsert_job,
        clear_stale_browser_session_state, update_jobs_bulk,
    )
    from core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
except ImportError:
    import backend.core.shared_config as config
    from backend.core.shared_config import (
        JOBS_FILE, CANDIDATE_PROFILE, LOGS_DIR, BASE_DIR, BROWSER_PROFILE_DIR,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        read_jobs, write_jobs, update_job, upsert_job as atomic_upsert_job,
        clear_stale_browser_session_state, update_jobs_bulk,
    )
    from backend.core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start


def load_jobs() -> dict:
    return read_jobs()


def save_jobs(jobs: dict):
    write_jobs(jobs)


_US_STATE_HINTS = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "united states", "usa", "u.s.", "us", "remote",
}

_US_STATE_ABBREVIATIONS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
    "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
    "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
    "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
}

_CLEAR_NON_US_LOCATION_HINTS = {
    "canada", "india", "united kingdom", "uk", "england", "ireland", "germany", "france",
    "spain", "italy", "netherlands", "poland", "portugal", "brazil", "mexico", "australia",
    "singapore", "dubai", "uae", "united arab emirates", "pakistan", "bangladesh",
    "philippines", "vietnam", "japan", "china", "hong kong", "taiwan", "south korea",
    "turkey", "south africa",
}

_NO_SPONSORSHIP_PATTERNS = [
    r"\b(?:no|not|unable to|cannot|can't|will not|won't|does not|do not)\s+(?:offer\s+)?(?:provide\s+)?(?:visa\s+)?sponsor(?:ship|ing)?\b",
    r"\b(?:sponsorship|immigration support)\s+(?:is\s+)?not\s+(?:available|offered|provided)\b",
    r"\bno sponsorship (?:is )?(?:available|offered|provided)\b",
    r"\bsponsorship (?:is )?unavailable\b",
    r"\bnot eligible for (visa|employment) sponsorship\b",
    r"\bwithout (?:current (?:and|or) future |current or future )?(?:visa|employer|employment) sponsorship\b",
    r"\b(?:must|should) not (?:now or (?:at any time )?in the future )?require\s+(?:visa|employment)?\s*sponsorship\b",
    r"\b(?:will not|won't|cannot|can't|unable to|do not|does not).{0,50}(?:now or in the future|currently or in the future).{0,30}\b(?:sponsor|sponsorship)\b",
    r"\bauthorized(?:\s+\w+){0,10}\s+without\s+.*sponsorship\b",
    r"\bmust be (?:legally )?authorized to work .* without .*sponsorship\b",
    r"\brequires? unrestricted work authorization\b",
    r"\bno immigration (?:support|assistance)\b",
    r"\bno (?:h[\s-]?1b|visa|employment) sponsorship\b",
    r"\b(?:h[\s-]?1b|visa|employment) sponsorship (?:is )?not (?:available|offered|provided)\b",
    r"\bnot eligible for h[\s-]?1b sponsorship\b",
    r"\b(?:do not|does not|cannot|can't|will not|won't) (?:hire|consider) candidates?.{0,40}\brequir(?:e|ing) sponsorship\b",
    r"\b(?:do not|does not|cannot|can't|will not|won't|unable to)\s+(?:offer|provide|support)\s+(?:an?\s+)?h[\s-]?1b\s+sponsorship\b",
    r"\b(?:no|not offering|not providing)\s+(?:immigration|visa)\s+(?:support|assistance|sponsorship)\b",
]

_US_CITIZEN_ONLY_PATTERNS = [
    r"\bmust be a (?:u\.?s\.?|united states) citizen\b",
    r"\b(?:u\.?s\.?|united states) citizenship is required\b",
    r"\bcitizenship required\b",
    r"\bcitizens? only\b",
    r"\b(?:must|need to) be (?:a )?u\.?s\.? persons?\b",
    r"\bu\.?s\.? persons? (?:only|required)\b",
    r"\b(?:citizens?|lawful permanent residents?|green card holders?)\s+only\b",
    r"\b(?:itar|ear|export control).{0,100}\bu\.?s\.? persons?\b",
]

_CLEARANCE_REQUIRED_PATTERNS = [
    r"\bactive\b.{0,20}\b(?:secret|top secret|ts/sci|security) clearance\b",
    r"\b(?:able|ability|eligib\w*|willingness)\b.{0,40}\bobtain\b.{0,20}\bsecurity clearance\b",
    r"\bsecurity clearances?\s+(?:may only|are only)\b.{0,10}granted to\b",
    r"\bclearance\b.{0,40}(?:to include|including)\s+u\.?s\.?\s*citizenship\b",
    r"\b(?:must|required to|requires?|need to)\b.{0,40}\b(?:possess|maintain|hold|obtain|have)\b.{0,25}\b(?:security|secret|top secret|ts/sci) clearance\b",
    r"\b(?:secret|top secret|ts/sci|security) clearance (?:is )?required\b",
]

_NO_OPT_CPT_PATTERNS = [
    r"\b(?:cannot|can't|unable to|will not|won't|does not|do not) (?:sponsor|support|accommodate|accept|consider).{0,40}\b(?:opt|cpt|f[\s-]?1)\b",
    r"\bno (?:opt|cpt|f[\s-]?1)(?:/cpt)? (?:candidates?|students?|sponsorship|support)\b",
    r"\b(?:opt|cpt|f[\s-]?1)(?:/cpt)? (?:candidates?|students?) (?:are )?not eligible\b",
    r"\b(?:student|temporary) (?:visas?|work authorization) (?:are |is )?not (?:accepted|eligible|supported)\b",
    r"\b(?:f[\s-]?1|opt|cpt)(?:/opt|/cpt)? (?:candidates?|students?).{0,30}(?:not eligible|not considered|cannot be considered|will not be considered)\b",
    r"\b(?:applicants?|candidates?).{0,30}(?:on|using|with)\s+(?:f[\s-]?1|opt|cpt).{0,35}(?:not eligible|not considered|cannot be considered|will not be considered|may not apply)\b",
    r"\b(?:f[\s-]?1|opt|cpt).{0,35}(?:not accepted|not supported|not permitted|ineligible|cannot apply|may not apply)\b",
    r"\b(?:do not|does not|cannot|can't|will not|won't)\s+(?:hire|employ|consider).{0,35}\b(?:f[\s-]?1|opt|cpt)\b",
]

_PERMANENT_AUTHORIZATION_PATTERNS = [
    r"\b(?:must|need to) (?:have|possess) permanent (?:u\.?s\.? )?work authorization\b",
    r"\bpermanent (?:u\.?s\.? )?work authorization (?:is )?required\b",
    r"\bpermanent authorization to work (?:in|within) the (?:u\.?s\.?|united states) (?:is )?required\b",
    r"\bauthorized to work (?:in|within) the (?:u\.?s\.?|united states) (?:on a )?permanent basis\b",
    r"\bwork authorization.{0,30}\bno (?:expiration|expiry)\b",
    r"\b(?:must|need to) (?:be able to )?work (?:in the (?:u\.?s\.?|united states) )?without (?:restriction|limitations?)\b",
    r"\b(?:citizen|citizenship|green card|permanent resident).{0,30}\brequired\b",
]

_POSITIVE_STUDENT_VISA_PATTERNS = [
    r"\b(?:visa|employment) sponsorship (?:is )?(?:available|provided|offered)\b",
    r"\b(?:we|company|employer) (?:can|may|will) sponsor\b",
    r"\b(?:eligible|considered) for (?:visa|employment) sponsorship\b",
    r"\bh[\s-]?1b sponsorship (?:is )?(?:available|provided|offered)\b",
    r"\bsponsor(?:s|ed|ing)? (?:an? )?h[\s-]?1b\b",
    r"\b(?:accept|welcome|consider|support)(?:s|ed|ing)?\s+(?:candidates? (?:on|with) )?(?:f[\s-]?1|opt|cpt)\b",
    r"\b(?:f[\s-]?1|opt|cpt)(?:/opt|/cpt)?\s+(?:candidates?|students?)\s+(?:are )?(?:eligible|welcome|accepted|considered)\b",
]

_INTERNSHIP_TITLE_RE = re.compile(
    r"\b(?:intern(?:ship|ships|s)?|co[\s-]?op)\b",
    re.IGNORECASE,
)

_TECH_INTERNSHIP_ROLE_RE = re.compile(
    r"\b(?:"
    r"software|swe|developer|development|frontend|front[\s-]?end|"
    r"backend|back[\s-]?end|full[\s-]?stack|web|mobile|ios|android|"
    r"machine\s+learning|ml|artificial\s+intelligence|ai|deep\s+learning|"
    r"data\s+(?:science|scientist|engineering|engineer|analytics|analyst)|"
    r"computer\s+vision|nlp|applied\s+scientist|research\s+engineer|"
    r"cloud|platform|devops|site\s+reliability|sre|systems?\s+engineer|"
    r"cybersecurity|cyber\s+security|security\s+engineer|robotics|"
    r"quantitative\s+(?:developer|researcher)|test\s+automation|qa\s+engineer|"
    r"embedded|firmware|computer\s+science|information\s+technology"
    r")\b",
    re.IGNORECASE,
)

_SOFTWARE_INTERNSHIP_ROLE_RE = re.compile(
    r"\b(?:software|swe|developer|development|frontend|front[\s-]?end|"
    r"backend|back[\s-]?end|full[\s-]?stack|web|mobile|ios|android|"
    r"cloud|platform|devops|site\s+reliability|sre|database|embedded|firmware)\b",
    re.IGNORECASE,
)

_AI_ML_INTERNSHIP_ROLE_RE = re.compile(
    r"\b(?:machine\s+learning|ml|artificial\s+intelligence|ai|deep\s+learning|"
    r"computer\s+vision|nlp|large\s+language\s+model|llm|generative\s+ai|"
    r"multimodal|applied\s+scientist|research\s+(?:scientist|engineer))\b",
    re.IGNORECASE,
)


def _is_internship_search(title: str) -> bool:
    """Return whether the requested search explicitly targets internships."""
    return bool(_INTERNSHIP_TITLE_RE.search(title or ""))


def _ensure_internship_search_title(title: str) -> str:
    """LangHire's collector is internship-only, including typed ad-hoc searches."""
    cleaned = " ".join((title or "").split())
    return cleaned if _is_internship_search(cleaned) else f"{cleaned} Intern".strip()


def _is_relevant_tech_internship_title(title: str) -> bool:
    """Keep actual internships in the software and AI/ML role families."""
    text = title or ""
    return bool(_INTERNSHIP_TITLE_RE.search(text) and _TECH_INTERNSHIP_ROLE_RE.search(text))


def _matches_requested_internship_role(search_title: str, job_title: str) -> bool:
    """Require a result to belong to the role family that was requested.

    LinkedIn mixes generic data, hardware, and robotics internships into SWE
    and AI/ML searches. A broad "tech internship" check is not enough: an
    AI/ML search must contain an AI/ML signal, while a software search must
    contain a software-development signal.
    """
    if not _is_relevant_tech_internship_title(job_title):
        return False
    requested = search_title or ""
    wants_ai_ml = bool(_AI_ML_INTERNSHIP_ROLE_RE.search(requested))
    wants_software = bool(_SOFTWARE_INTERNSHIP_ROLE_RE.search(requested))
    matches_ai_ml = bool(_AI_ML_INTERNSHIP_ROLE_RE.search(job_title or ""))
    matches_software = bool(_SOFTWARE_INTERNSHIP_ROLE_RE.search(job_title or ""))
    if wants_ai_ml and wants_software:
        return matches_ai_ml or matches_software
    if wants_ai_ml:
        return matches_ai_ml
    if wants_software:
        return matches_software
    return True


def _matches_any_requested_internship_role(
    search_title: str,
    job_title: str,
    allowed_titles: list[str] | None = None,
) -> bool:
    """Match any role family requested by the complete collection run."""
    requested_titles = allowed_titles or [search_title]
    return any(
        _matches_requested_internship_role(requested_title, job_title)
        for requested_title in requested_titles
    )


def _has_location_hint(text: str, hint: str) -> bool:
    """Match location hints as words/phrases, not arbitrary substrings."""
    return re.search(rf"(^|[^a-z]){re.escape(hint.lower())}([^a-z]|$)", text) is not None


def _build_search_url(title: str, profile: dict, filters: dict | None = None) -> str:
    """Build a LinkedIn search URL with the UI filters applied."""
    title = _ensure_internship_search_title(title)
    locations = profile.get("target_locations") or ["United States"]
    location = ", ".join(locations)

    linkedin_filter_params = {
        "date_posted": "f_TPR",
        "experience_level": "f_E",
        "work_type": "f_WT",
        "job_type": "f_JT",
    }
    effective_filters = {"date_posted": "r604800", **(filters or {})}
    if _is_internship_search(title):
        effective_filters["experience_level"] = "1"
        effective_filters["job_type"] = "I"
    params = {"keywords": title, "location": location}
    for key, value in effective_filters.items():
        if value and key in linkedin_filter_params:
            params[linkedin_filter_params[key]] = str(value)

    return f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"


def _linkedin_job_id(url: str) -> str:
    match = re.search(r"currentJobId=(\d{7,12})", url or "")
    if not match:
        match = re.search(r"/jobs/view/(?:[^?#]*?-)?(\d{7,12})(?:[/?#]|$)", url or "")
    return match.group(1) if match else ""


def _normalize_linkedin_job_url(url: str = "", job_id: str = "") -> str:
    job_id = job_id or _linkedin_job_id(url)
    return f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else ""


def _clean_job(job: dict, search_title: str) -> dict | None:
    """Normalize a raw DOM job dict into the jobs.json shape."""
    job_id = str(job.get("id") or _linkedin_job_id(job.get("url", "")) or "").strip()
    url = _normalize_linkedin_job_url(job.get("url", ""), job_id)
    if not url:
        return None

    cleaned = {
        "url": url,
        "title": (job.get("title") or "").strip() or "Unknown",
        "company": (job.get("company") or "").strip() or "Unknown",
        "location": (job.get("location") or "").strip(),
        "easy_apply": job.get("easy_apply") if job.get("easy_apply") in (True, False) else None,
        "description": (job.get("description") or "").strip(),
        "search_title": search_title,
    }
    return cleaned


def _classify_us_location(location: str, profile: dict) -> str:
    """Return ``us``, ``non_us``, or ``unknown`` for a US-targeted profile."""
    target_country = (profile.get("country") or profile.get("address", {}).get("country") or "US").upper()
    if target_country not in {"US", "USA", "UNITED STATES"}:
        return "us"

    location_l = (location or "").lower()
    if not location_l:
        return "unknown"
    explicit_us = any(_has_location_hint(location_l, hint) for hint in ("united states", "usa", "u.s.", "us"))
    if any(_has_location_hint(location_l, hint) for hint in _CLEAR_NON_US_LOCATION_HINTS) and not explicit_us:
        return "non_us"
    if explicit_us or any(_has_location_hint(location_l, hint) for hint in _US_STATE_HINTS - {"remote"}):
        return "us"
    if any(_has_location_hint(location_l, abbr) for abbr in _US_STATE_ABBREVIATIONS):
        return "us"
    return "unknown"


def _is_us_location(location: str, profile: dict) -> bool:
    """Compatibility helper: only explicitly foreign locations are rejected."""
    return _classify_us_location(location, profile) != "non_us"


def _profile_needs_student_visa_support(profile: dict) -> bool:
    def enabled(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "required", "needed"}
        return bool(value)

    if any(enabled(profile.get(key)) for key in (
        "visa_sponsorship_needed", "future_sponsorship_needed", "h1b_sponsorship_needed", "f1_visa_status", "f1_status"
    )):
        return True
    profile_text = " ".join(str(profile.get(key) or "") for key in (
        "work_authorization", "current_work_status", "visa_status", "immigration_status"
    ))
    return bool(re.search(r"\b(?:f[\s-]?1|opt|cpt|h[\s-]?1b)\b", profile_text, re.IGNORECASE))


def _authorization_assessment(text: str, profile: dict) -> tuple[str, str, str]:
    """Classify a posting for an F-1/CPT/OPT candidate needing future H-1B.

    ``compatible`` requires affirmative evidence, ``ineligible`` requires an
    explicit restriction, and everything else remains ``needs_review``.  This
    prevents silence in a posting from being represented as confirmed support.
    """
    if not _profile_needs_student_visa_support(profile):
        return "not_required", "", "Candidate profile does not require visa support"

    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    checks = (
        (_US_CITIZEN_ONLY_PATTERNS, "Filtered out: requires U.S. citizenship or U.S.-person status"),
        (_CLEARANCE_REQUIRED_PATTERNS, "Filtered out: requires security clearance"),
        (_NO_OPT_CPT_PATTERNS, "Filtered out: posting rejects F-1/OPT/CPT candidates"),
        (_PERMANENT_AUTHORIZATION_PATTERNS, "Filtered out: requires permanent U.S. work authorization"),
        (_NO_SPONSORSHIP_PATTERNS, "Filtered out: posting says no current or future visa sponsorship"),
    )
    for patterns, reason in checks:
        for pattern in patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                start = max(0, match.start() - 80)
                end = min(len(normalized), match.end() + 80)
                return "ineligible", reason, normalized[start:end]

    for pattern in _POSITIVE_STUDENT_VISA_PATTERNS:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            start = max(0, match.start() - 80)
            end = min(len(normalized), match.end() + 80)
            return "compatible", "", normalized[start:end]

    return "needs_review", "", "No explicit F-1/OPT/CPT or H-1B support statement found"


def _matches_authorization_constraints(text: str, profile: dict) -> tuple[bool, str]:
    """Backward-compatible boolean view of the richer authorization result."""
    assessment, reason, _ = _authorization_assessment(text, profile)
    return assessment != "ineligible", reason


def _parse_eval_json(raw: str, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


async def _wait_for_ready(page, timeout: float = 20.0):
    """Wait until document.readyState is at least interactive."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            raw = await page.evaluate(
                "() => JSON.stringify({ready: document.readyState, url: location.href, text: document.body?.innerText?.slice(0, 500) || ''})"
            )
            state = _parse_eval_json(raw, {})
            if state.get("ready") in {"interactive", "complete"}:
                return state
        except Exception:
            pass
        if asyncio.get_running_loop().time() >= deadline:
            return {}
        await asyncio.sleep(0.5)


def _linkedin_page_problem(state: dict) -> str:
    text = (state.get("text") or "").lower()
    if re.search(r"security verification|verify you are human|quick security check|captcha|unusual activity", text):
        return "security_challenge"
    if re.search(r"this page isn['’]t working|temporarily unavailable|too many requests|job is no longer available", text):
        return "error_page"
    return ""


async def _wait_for_linkedin_login(
    page,
    target_url: str,
    timeout: float = 300.0,
    cancel_flag: dict | None = None,
) -> bool:
    """Navigate to LinkedIn and wait for manual login if needed."""
    await page.goto(target_url)
    deadline = asyncio.get_running_loop().time() + timeout
    prompted = False
    attention_seen = False
    returned_to_target = False
    while True:
        if _cancel_requested(cancel_flag):
            print("    🛑 Stop requested while waiting for LinkedIn login")
            return False
        state = await _wait_for_ready(page, timeout=8)
        if not state:
            if asyncio.get_running_loop().time() >= deadline:
                print("    ❌ LinkedIn page disconnected or did not become ready")
                return False
            await asyncio.sleep(1)
            continue
        page_url = state.get("url", "")
        text = state.get("text", "").lower()
        problem = _linkedin_page_problem(state)
        if problem == "error_page":
            raise RuntimeError("LinkedIn returned an error or rate-limit page")
        logged_out = (
            "/login" in page_url
            or "/uas/login" in page_url
            or ("sign in" in text and "email or phone" in text)
            or ("join linkedin" in text and "sign in" in text)
        )
        needs_attention = logged_out or problem == "security_challenge"
        attention_seen = attention_seen or needs_attention
        if not needs_attention:
            # Login can finish on the feed or checkpoint page. Return to the
            # requested jobs URL once, after authentication, instead of
            # repeatedly navigating the login tab every five seconds.
            if attention_seen and "/jobs" not in page_url and not returned_to_target:
                returned_to_target = True
                await page.goto(target_url)
                continue
            return True
        if not prompted:
            action = "complete the security check" if problem == "security_challenge" else "log in"
            print(f"    🔐 LinkedIn requires attention. Please {action} in the browser window; collection will continue automatically.")
            prompted = True
        if asyncio.get_running_loop().time() >= deadline:
            print("    ❌ LinkedIn login was not completed in time")
            return False
        await asyncio.sleep(5)


_EXTRACT_CARDS_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const jobIdFromHref = (href) => {
    const value = String(href || '');
    const m = value.match(/[?&]currentJobId=(\d{7,12})/) || value.match(/\/jobs\/view\/(?:[^?#]*?-)?(\d{7,12})(?:[/?#]|$)/);
    return m ? m[1] : '';
  };
  const selectors = [
    'li[data-occludable-job-id]',
    'li.jobs-search-results__list-item',
    'div.job-card-container[data-job-id]',
    'div[data-job-id]',
    '.job-card-container'
  ];
  const seenElements = new Set();
  const elements = [];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      if (!seenElements.has(el)) {
        seenElements.add(el);
        elements.push(el);
      }
    }
  }
  for (const a of document.querySelectorAll('a[href*="/jobs/view/"]')) {
    const el = a.closest('li, .job-card-container, [data-job-id], .jobs-search-results__list-item');
    if (el && !seenElements.has(el)) {
      seenElements.add(el);
      elements.push(el);
    }
  }
  const jobs = [];
  const seenIds = new Set();
  for (const [index, el] of elements.entries()) {
    const anchor = Array.from(el.querySelectorAll('a[href*="/jobs/view/"]'))
      .find((a) => jobIdFromHref(a.href)) || el.querySelector('a[href*="/jobs/view/"]');
    const id = jobIdFromHref(anchor?.href || '')
      || [el.getAttribute('data-occludable-job-id'), el.getAttribute('data-job-id')]
        .find((value) => /^\d{7,12}$/.test(String(value || '')))
      || '';
    if (!id || seenIds.has(id)) continue;
    seenIds.add(id);
    const titleEl = el.querySelector('a.job-card-list__title--link, a.job-card-container__link, .job-card-list__title, .job-card-container__title, strong')
      || anchor;
    const companyEl = el.querySelector('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, .base-search-card__subtitle, [class*="company"]');
    const locationEl = el.querySelector('.job-card-container__metadata-item, .job-search-card__location, .base-search-card__metadata, [class*="location"]');
    const rawText = el.innerText || '';
    const text = clean(rawText);
    const lines = rawText.split(/\n+/).map(clean).filter(Boolean);
    const title = clean(titleEl?.innerText || anchor?.innerText || anchor?.getAttribute('aria-label') || anchor?.getAttribute('title') || lines[0] || '');
    const company = clean(companyEl?.innerText || lines.find((line) => line && line !== title && !/easy apply|promoted|viewed|actively recruiting/i.test(line)) || '');
    const location = clean(locationEl?.innerText || lines.find((line) => /remote|united states|usa|,\s*[A-Z]{2}\b/i.test(line)) || '');
    jobs.push({
      id,
      index,
      url: `https://www.linkedin.com/jobs/view/${id}/`,
      title,
      company,
      location,
      easy_apply: /(^|\s)Easy Apply($|\s)/i.test(text) ? true : null,
      visible_text: text.slice(0, 1200)
    });
  }
  return JSON.stringify(jobs);
}"""


_CLICK_CARD_JS = r"""(jobId) => {
  const idFromHref = (href) => {
    const value = String(href || '');
    const m = value.match(/[?&]currentJobId=(\d{7,12})/) || value.match(/\/jobs\/view\/(?:[^?#]*?-)?(\d{7,12})(?:[/?#]|$)/);
    return m ? m[1] : '';
  };
  const all = Array.from(document.querySelectorAll(
    'li[data-occludable-job-id], li.jobs-search-results__list-item, div.job-card-container[data-job-id], div[data-job-id], .job-card-container'
  ));
  const el = all.find((node) => {
    const anchor = node.querySelector('a[href*="/jobs/view/"]');
    return (/^\d{7,12}$/.test(node.getAttribute('data-occludable-job-id') || '') && node.getAttribute('data-occludable-job-id') === jobId)
      || (/^\d{7,12}$/.test(node.getAttribute('data-job-id') || '') && node.getAttribute('data-job-id') === jobId)
      || idFromHref(anchor?.href || '') === jobId;
  });
  if (!el) return JSON.stringify({ok: false, reason: 'card not found'});
  const anchor = Array.from(el.querySelectorAll('a[href*="/jobs/view/"]')).find((a) => idFromHref(a.href) === jobId)
    || el.querySelector('a[href*="/jobs/view/"]');
  const target = anchor || el;
  target.scrollIntoView({block: 'center', inline: 'nearest'});
  const opts = {bubbles: true, cancelable: true, view: window};
  target.dispatchEvent(new MouseEvent('mouseover', opts));
  target.dispatchEvent(new MouseEvent('mousedown', opts));
  target.dispatchEvent(new MouseEvent('mouseup', opts));
  target.click();
  return JSON.stringify({ok: true});
}"""


_EXTRACT_DETAILS_JS = r"""(expectedId) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const pageUrl = location.href;
  const idMatch = pageUrl.match(/[?&]currentJobId=(\d{7,12})/) || pageUrl.match(/\/jobs\/view\/(?:[^?#]*?-)?(\d{7,12})(?:[/?#]|$)/);
  const canonicalHref = document.querySelector('link[rel="canonical"]')?.href || '';
  const canonicalMatch = canonicalHref.match(/\/jobs\/view\/(?:[^?#]*?-)?(\d{7,12})(?:[/?#]|$)/);
  const selected = document.querySelector('[aria-current="true"][href*="/jobs/view/"], .jobs-search-results__list-item--active a[href*="/jobs/view/"]');
  const selectedHref = String(selected?.href || '');
  const selectedMatch = selectedHref.match(/[?&]currentJobId=(\d{7,12})/) || selectedHref.match(/\/jobs\/view\/(?:[^?#]*?-)?(\d{7,12})(?:[/?#]|$)/);
  // In LinkedIn's split view the address bar can lag behind the active card.
  // The selected card is therefore the authoritative ID when it is present.
  const observedId = (selectedMatch && selectedMatch[1]) || (idMatch && idMatch[1]) || (canonicalMatch && canonicalMatch[1]) || '';
  const root = document.querySelector('.jobs-search__job-details--container, .jobs-details__main-content, .job-view-layout');
  const pickText = (selectors) => {
    for (const selector of selectors) {
      const el = root?.querySelector(selector) || document.querySelector(selector);
      const text = clean(el?.innerText || el?.textContent || '');
      if (text) return text;
    }
    return '';
  };
  const buttonTexts = Array.from((root || document).querySelectorAll('button, a'))
    .map((el) => clean(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
    .filter(Boolean);
  const descriptionEl = root?.querySelector('.jobs-description__content, .jobs-box__html-content, #job-details, [class*="jobs-description"]')
    || document.querySelector('.jobs-description__content, .jobs-box__html-content, #job-details, [class*="jobs-description"]');
  const bodyText = clean(document.body?.innerText || '');
  const description = clean(descriptionEl?.innerText || '');
  const errorPage = /this page isn['’]t working|page not found|job is no longer available|this job is no longer available|something went wrong|temporarily unavailable/i.test(bodyText);
  const challengePage = /security verification|verify you are human|let['’]s do a quick security check|captcha|unusual activity/i.test(bodyText);
  const easy = buttonTexts.some((t) => /^easy apply$/i.test(t) || /easy apply/i.test(t));
  const hasApply = easy || buttonTexts.some((t) => /^(apply|apply now|apply on company site|continue to apply)$/i.test(t));
  return JSON.stringify({
    id: observedId,
    expected_id: expectedId || '',
    url: observedId ? `https://www.linkedin.com/jobs/view/${observedId}/` : pageUrl,
    page_url: pageUrl,
    title: pickText(['.job-details-jobs-unified-top-card__job-title', '.jobs-unified-top-card__job-title', '.jobs-details-top-card__job-title', '[class*="job-title"]']),
    company: pickText(['.job-details-jobs-unified-top-card__company-name', '.jobs-unified-top-card__company-name', '[class*="company-name"]']),
    location: pickText(['.job-details-jobs-unified-top-card__primary-description-container .tvm__text', '.jobs-unified-top-card__bullet', '[class*="job-location"]', '[class*="location"]']),
    easy_apply: easy ? true : (hasApply ? false : null),
    description: description.slice(0, 20000),
    button_texts: buttonTexts.slice(0, 30),
    has_job_root: Boolean(root),
    error_page: errorPage,
    challenge_page: challengePage,
    body_sample: bodyText.slice(0, 1000)
  });
}"""


_SCROLL_RESULTS_JS = r"""() => {
  const candidates = [
    document.querySelector('.jobs-search-results-list'),
    document.querySelector('.jobs-search-results-list__list'),
    document.querySelector('[aria-label*="Jobs search results"]'),
    document.querySelector('.scaffold-layout__list'),
    document.scrollingElement
  ].filter(Boolean);
  const scroller = candidates.find((el) => el.scrollHeight > el.clientHeight + 100) || document.scrollingElement;
  const before = scroller.scrollTop;
  const amount = Math.max(500, Math.floor((scroller.clientHeight || window.innerHeight) * 0.85));
  scroller.scrollBy(0, amount);
  return JSON.stringify({before, after: scroller.scrollTop, max: scroller.scrollHeight - scroller.clientHeight});
}"""


_GO_TO_NEXT_RESULTS_PAGE_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const disabled = (el) => {
    const cls = String(el.className || '');
    return el.disabled
      || el.getAttribute('aria-disabled') === 'true'
      || cls.includes('--disabled')
      || cls.includes('disabled');
  };
  const nextControls = Array.from(document.querySelectorAll('button, a'))
    .filter((el) => {
      const label = clean(el.getAttribute('aria-label') || el.innerText || el.textContent || '');
      return /(next|view next page)/i.test(label) && !disabled(el);
    });
  const direct = nextControls.find((el) => /view next page|next/i.test(clean(el.getAttribute('aria-label') || el.innerText || el.textContent || '')));
  if (direct) {
    direct.scrollIntoView({block: 'center', inline: 'nearest'});
    direct.click();
    return JSON.stringify({ok: true, method: 'button', url: location.href});
  }

  const url = new URL(location.href);
  const currentStart = Number(url.searchParams.get('start') || '0');
  const nextStart = currentStart + 25;
  if (nextStart > 975) return JSON.stringify({ok: false, reason: 'pagination cap reached'});
  url.searchParams.set('start', String(nextStart));
  location.href = url.toString();
  return JSON.stringify({ok: true, method: 'start-param', url: url.toString(), start: nextStart});
}"""


async def _extract_cards(page) -> list[dict]:
    return _parse_eval_json(await page.evaluate(_EXTRACT_CARDS_JS), [])


async def _extract_details(page, job_id: str) -> dict:
    return _parse_eval_json(await page.evaluate(_EXTRACT_DETAILS_JS, job_id), {})


_INVALID_PAGE_TITLE_RE = re.compile(
    r"^(?:this page isn['’]t working|page not found|linkedin|jobs?|sign in)$",
    re.IGNORECASE,
)


def _validate_linkedin_details(details: dict, expected_job_id: str) -> tuple[bool, str]:
    """Reject error/challenge pages and details belonging to another card."""
    if not details:
        return False, "empty job details"
    if details.get("challenge_page"):
        return False, "LinkedIn security challenge detected"
    if details.get("error_page"):
        return False, "LinkedIn error or expired-job page detected"
    observed_id = str(details.get("id") or "")
    if expected_job_id and observed_id and observed_id != expected_job_id:
        return False, f"wrong job opened (expected {expected_job_id}, got {observed_id})"
    if expected_job_id and not observed_id:
        return False, "could not verify the opened LinkedIn job ID"
    title = (details.get("title") or "").strip()
    if not details.get("has_job_root") or not title or _INVALID_PAGE_TITLE_RE.match(title):
        return False, "page does not contain a valid LinkedIn job panel"
    return True, ""


async def _wait_for_expected_job(page, job_id: str, timeout: float = 8.0) -> tuple[dict, str]:
    deadline = asyncio.get_running_loop().time() + timeout
    last_reason = "job details did not load"
    while asyncio.get_running_loop().time() < deadline:
        details = await _extract_details(page, job_id)
        valid, reason = _validate_linkedin_details(details, job_id)
        if valid:
            return details, ""
        last_reason = reason
        if details.get("challenge_page") or details.get("error_page"):
            break
        await asyncio.sleep(0.4)
    return {}, last_reason


async def _scroll_results(page) -> dict:
    return _parse_eval_json(await page.evaluate(_SCROLL_RESULTS_JS), {})


async def _go_to_next_results_page(page) -> dict:
    return _parse_eval_json(await page.evaluate(_GO_TO_NEXT_RESULTS_PAGE_JS), {})


def _has_usable_card_metadata(job: dict) -> bool:
    title = (job.get("title") or "").strip().lower()
    company = (job.get("company") or "").strip().lower()
    return bool(title and title != "unknown") or bool(company and company != "unknown")


_MIN_REAL_DESCRIPTION_LENGTH = 200


def _screening_fields(job: dict, profile: dict, *, description_complete: bool) -> dict:
    """Build auditable queue fields from location and visa screening."""
    checked_at = datetime.now(timezone.utc).isoformat()
    location_state = _classify_us_location(job.get("location", ""), profile)
    text = " ".join(str(job.get(key) or "") for key in ("title", "company", "location", "description"))
    auth_state, reason, evidence = _authorization_assessment(text, profile)

    fields = {
        "location_screening_status": location_state,
        "visa_screening_status": auth_state,
        "visa_screening_evidence": evidence,
        "screening_checked_at": checked_at,
    }
    if location_state == "non_us":
        fields.update(status="blocked", screening_status="ineligible", error=f"Filtered out: non-US location ({job.get('location', '')})")
    elif auth_state == "ineligible":
        fields.update(status="blocked", screening_status="ineligible", error=reason)
    elif not description_complete:
        fields.update(status="manual_review", screening_status="pending", error="Job description still needs visa screening")
    elif location_state == "unknown":
        fields.update(status="manual_review", screening_status="needs_review", error="Could not confirm this is a U.S.-based position")
    elif auth_state == "needs_review":
        fields.update(status="manual_review", screening_status="needs_review", error="Posting does not explicitly confirm F-1/OPT/CPT or future H-1B support")
    else:
        fields.update(status="pending", screening_status="compatible", error=None)
    return fields


def quarantine_legacy_unscreened_jobs(jobs: dict, profile: dict) -> int:
    """Move old collector records out of the apply queue until verified."""
    updates = {}
    collector_methods = {"deterministic_linkedin_dom", "speedyapply_github_table"}
    for url, job in jobs.items():
        if (
            job.get("collection_method") in collector_methods
            and job.get("status") in {"pending", "failed"}
            and job.get("screening_status") != "compatible"
        ):
            fields = _screening_fields(
                job,
                profile,
                description_complete=len(job.get("description") or "") >= _MIN_REAL_DESCRIPTION_LENGTH,
            )
            if len(job.get("description") or "") < _MIN_REAL_DESCRIPTION_LENGTH:
                fields.update(
                    status="manual_review",
                    screening_status="legacy_unverified",
                    error="Collected before strict F-1/H-1B screening; recollect or review manually",
                )
            if _INVALID_PAGE_TITLE_RE.match((job.get("title") or "").strip()):
                fields.update(
                    status="blocked",
                    screening_status="invalid_page",
                    error="Invalid job record captured from a LinkedIn error page",
                )
            updates[url] = fields
    changed = update_jobs_bulk(updates)
    for url, fields in updates.items():
        if url in jobs:
            jobs[url].update(fields)
    return changed


def _cancel_requested(cancel_flag) -> bool:
    return bool(cancel_flag and cancel_flag.get("cancel_requested"))


async def collect_for_title(
    title: str,
    existing_jobs: dict,
    profile: dict,
    max_jobs: int = 0,
    filters: dict | None = None,
    cancel_flag: dict | None = None,
    run_id: str = "",
    allowed_titles: list[str] | None = None,
) -> list[dict]:
    """Deterministically collect LinkedIn listings for one title.

    This intentionally avoids an LLM browser agent. The old collector could click
    Apply/Easy Apply or wander into company pages before saving the job. This
    version extracts LinkedIn job IDs from the DOM and verifies the selected
    details panel before any page-derived metadata is persisted.
    """
    title = _ensure_internship_search_title(title)
    refresh_credentials()
    clear_stale_browser_session_state()
    _agent_log_start("collect", title)

    browser = BrowserSession(**config.browser_session_kwargs())
    search_url = _build_search_url(title, profile, filters)
    seen_urls = set(existing_jobs.keys())
    found_urls: set[str] = set()
    found: list[dict] = []

    def save_candidate(raw_job: dict, *, fetch_error: str = "") -> bool:
        job = _clean_job(raw_job, title)
        if not job:
            return False
        url = job["url"]
        if _is_internship_search(title) and not _matches_any_requested_internship_role(
            title, job["title"], allowed_titles
        ):
            print(f"    ⏭️  Skipped non-target internship result: {job['title']}")
            return False
        now = datetime.now(timezone.utc).isoformat()
        description_complete = len(job.get("description") or "") >= _MIN_REAL_DESCRIPTION_LENGTH
        screening = _screening_fields(job, profile, description_complete=description_complete)
        if screening.get("screening_status") == "ineligible":
            print(f"    ⏭️  {screening.get('error')}: {job['title']} at {job['company']}")
            return False
        if fetch_error:
            # An unverified card is not part of a trustworthy requested count.
            # Keep the failure in the run log and continue looking for a fully
            # opened result rather than saving it as one of the user's jobs.
            return False
        fields = {
            **{k: v for k, v in job.items() if v not in ("", None, [])},
            "url": url,
            "search_title": title,
            "collected_at": now,
            "applied_at": None,
            "collection_method": "deterministic_linkedin_dom",
            **screening,
        }
        if run_id:
            fields["collection_run_id"] = run_id
        merged, is_new = atomic_upsert_job(url, fields)

        seen_urls.add(url)
        if is_new and url not in found_urls:
            found_urls.add(url)
            found.append(merged)
            print(f"    💾 Saved 1 new job (total this title: {len(found)}) — {merged['title']} at {merged['company']}")
        else:
            print(f"    🧾 Refined job details — {merged.get('title', 'Unknown')} at {merged.get('company', 'Unknown')}")
        if merged.get("status") == "blocked":
            print(f"    ⛔ {merged.get('error')}: {merged.get('title')} at {merged.get('company')}")
        elif merged.get("status") == "manual_review":
            print(f"    🟡 Visa review needed: {merged.get('title')} at {merged.get('company')}")
        else:
            print(f"    🟢 Visa-compatible posting: {merged.get('title')} at {merged.get('company')}")
        return is_new

    try:
        await browser.start()
        page = await browser.new_page("about:blank")
        print(f"    🔎 Opening LinkedIn search: {title}")
        if not await _wait_for_linkedin_login(page, search_url, cancel_flag=cancel_flag):
            return found

        max_scroll_rounds = 60 if max_jobs <= 0 else max(8, min(60, max_jobs * 4))
        max_result_pages = 20 if max_jobs <= 0 else max(2, min(20, (max_jobs // 25) + 4))
        result_page = 1
        stale_rounds = 0
        processed_ids: set[str] = set()

        for round_idx in range(max_scroll_rounds):
            if _cancel_requested(cancel_flag):
                print("    🛑 Stop requested — saving progress and leaving this search")
                return found
            page_state = await _wait_for_ready(page, timeout=10)
            problem = _linkedin_page_problem(page_state)
            if problem:
                raise RuntimeError(
                    "LinkedIn security challenge detected" if problem == "security_challenge"
                    else "LinkedIn returned an error or rate-limit page"
                )
            cards = await _extract_cards(page)
            new_cards = [c for c in cards if c.get("id") and c["id"] not in processed_ids]
            if new_cards:
                print(f"    📄 Found {len(new_cards)} visible unprocessed cards (scroll {round_idx + 1})")
                stale_rounds = 0
            else:
                stale_rounds += 1

            for card in new_cards:
                if _cancel_requested(cancel_flag):
                    print("    🛑 Stop requested — saving progress and leaving this search")
                    return found
                job_id = str(card.get("id") or "")
                processed_ids.add(job_id)
                card_url = _normalize_linkedin_job_url(card.get("url", ""), job_id)
                if not card_url or card_url in seen_urls:
                    continue

                try:
                    click_result = _parse_eval_json(await page.evaluate(_CLICK_CARD_JS, job_id), {})
                    if not click_result.get("ok"):
                        raise RuntimeError(click_result.get("reason") or "LinkedIn card click failed")
                    details, validation_error = await _wait_for_expected_job(page, job_id)
                    if not details:
                        raise RuntimeError(validation_error)
                    detail_fields = {
                        k: v for k, v in details.items()
                        if v not in ("", None, [])
                        and not (
                            k == "location"
                            and re.search(r"city,\s*state,\s*or\s*zip", str(v), re.IGNORECASE)
                        )
                    }
                    merged = {**card, **detail_fields}
                    save_candidate(merged)
                except Exception as e:
                    if _has_usable_card_metadata(card):
                        print(f"    ⚠️  Could not verify card {job_id}; not counted. Reason: {e}")
                        save_candidate(card, fetch_error=str(e))
                    else:
                        print(f"    ⚠️  Skipped card {job_id}: no metadata and details failed. Reason: {e}")

                if max_jobs > 0 and len(found) >= max_jobs:
                    print(f"    ✅ Reached {max_jobs} jobs — stopping collection for this title")
                    return found

                # LinkedIn rate-limits rapid split-panel clicks. A modest
                # delay is far cheaper than losing an entire title search.
                await asyncio.sleep(1.25)

                current_url = await page.get_url()
                if "linkedin.com" not in current_url or "/jobs" not in current_url:
                    print("    ↩️  Browser left LinkedIn during collection; returning to search results")
                    await page.goto(search_url)
                    await _wait_for_ready(page, timeout=12)

            if stale_rounds >= 4:
                if result_page >= max_result_pages:
                    print(f"    ✅ Reached LinkedIn pagination limit ({max_result_pages} pages)")
                    break
                next_page = await _go_to_next_results_page(page)
                if not next_page.get("ok"):
                    print("    ✅ No next LinkedIn results page found")
                    break
                result_page += 1
                stale_rounds = 0
                processed_ids.clear()
                print(f"    ➡️  Moving to LinkedIn results page {result_page} ({next_page.get('method', 'next')})")
                await asyncio.sleep(2.0)
                await _wait_for_ready(page, timeout=12)
                continue

            scroll_state = await _scroll_results(page)
            if scroll_state.get("after") == scroll_state.get("before"):
                stale_rounds += 1
            await asyncio.sleep(1.0)
    finally:
        try:
            await asyncio.wait_for(browser.close(), timeout=15)
        except Exception as close_err:
            print(f"    ⚠️  Browser cleanup error: {close_err}")

    return found


SPEEDYAPPLY_URL = "https://github.com/speedyapply/2027-SWE-College-Jobs"

_SPEEDYAPPLY_STOPWORDS = {
    "the", "for", "and", "or", "intern", "internship", "interns", "a", "an", "of", "to", "in", "with",
}

_EXTRACT_SPEEDYAPPLY_ROWS_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const rows = [];
  for (const table of document.querySelectorAll('table')) {
    const trs = Array.from(table.querySelectorAll('tr'));
    if (trs.length < 2) continue;
    const headers = Array.from(trs[0].querySelectorAll('th,td')).map((c) => clean(c.innerText).toLowerCase());
    const companyIdx = headers.indexOf('company');
    const positionIdx = headers.indexOf('position');
    const locationIdx = headers.indexOf('location');
    const postingIdx = headers.indexOf('posting');
    if (companyIdx < 0 || positionIdx < 0 || postingIdx < 0) continue;
    for (const tr of trs.slice(1)) {
      const cells = Array.from(tr.querySelectorAll('td'));
      if (!cells.length) continue;
      const company = clean(cells[companyIdx]?.querySelector('a')?.innerText || cells[companyIdx]?.innerText || '');
      const position = clean(cells[positionIdx]?.innerText || '');
      const location = locationIdx >= 0 ? clean(cells[locationIdx]?.innerText || '') : '';
      const url = cells[postingIdx]?.querySelector('a')?.href || '';
      if (!company || !position || !url) continue;
      rows.push({ company, position, location, url });
    }
  }
  return JSON.stringify(rows);
}"""


_EXTRACT_GENERIC_DETAILS_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const bodyText = clean(document.body?.innerText || '');
  let posting = null;
  for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const parsed = JSON.parse(node.textContent || '{}');
      const values = Array.isArray(parsed) ? parsed : (Array.isArray(parsed['@graph']) ? parsed['@graph'] : [parsed]);
      posting = values.find((value) => value && value['@type'] === 'JobPosting') || posting;
    } catch (_) {}
  }
  const descriptionNode = document.querySelector('[data-testid*="description" i], [class*="job-description" i], [class*="job_description" i], #job-description, #jobDescription, article');
  const htmlText = descriptionNode ? clean(descriptionNode.innerText || descriptionNode.textContent || '') : '';
  const structuredText = posting ? clean(String(posting.description || '').replace(/<[^>]+>/g, ' ')) : '';
  const title = clean(posting?.title || document.querySelector('meta[property="og:title"]')?.content || document.querySelector('h1')?.innerText || '');
  const company = clean(posting?.hiringOrganization?.name || document.querySelector('meta[property="og:site_name"]')?.content || '');
  const errorPage = /this page isn['’]t working|page not found|job is no longer available|position has been filled|something went wrong|temporarily unavailable/i.test(bodyText);
  const challengePage = /security verification|verify you are human|captcha|unusual activity|access denied/i.test(bodyText);
  return JSON.stringify({
    title,
    company,
    description: (structuredText || htmlText).slice(0, 20000),
    page_url: location.href,
    error_page: errorPage,
    challenge_page: challengePage,
    body_sample: bodyText.slice(0, 1000)
  });
}"""


def _title_matches_speedyapply_position(title: str, position: str) -> bool:
    """SpeedyApply's real position text is verbose and does not contain the
    target title as a literal substring (e.g. "Software Engineer: Cloud &
    Distributed Backend Intern Opportunities for University Students -
    Redmond" vs a target of "Software Engineer Intern"), so this matches on
    keyword overlap instead of a substring check. Every significant word of
    the target title must appear -- requiring only a majority let a shared
    generic word like "engineer" alone match "Mechanical Engineer Intern"
    against a "Software Engineer Intern" target. Word-boundary matching
    (not raw substring) keeps short but meaningful tokens like "ai" from
    firing inside unrelated words such as "email" or "training".
    """
    aliases = {"engineering": "engineer", "development": "developer"}
    title_words = {
        aliases.get(w, w)
        for w in re.findall(r"[a-z]+", title.lower())
        if w not in _SPEEDYAPPLY_STOPWORDS and len(w) >= 2
    }
    if not title_words:
        return True
    position_l = position.lower()
    position_words = {
        aliases.get(w, w) for w in re.findall(r"[a-z]+", position_l)
    }
    return title_words.issubset(position_words)


async def collect_speedyapply(
    title: str,
    existing_jobs: dict,
    profile: dict,
    max_jobs: int = 0,
    filters: dict | None = None,
    cancel_flag: dict | None = None,
    run_id: str = "",
) -> list[dict]:
    """Scrape the SpeedyApply GitHub README's internship tables for postings
    matching `title`. This is a static HTML page with no login and no
    pagination, so unlike LinkedIn's scroll-and-click collector this is a
    single page load and one DOM read.
    """
    title = _ensure_internship_search_title(title)
    clear_stale_browser_session_state()
    browser = BrowserSession(**config.browser_session_kwargs())
    found: list[dict] = []
    found_urls: set[str] = set()

    try:
        await browser.start()
        page = await browser.new_page("about:blank")
        await page.goto(SPEEDYAPPLY_URL)
        await _wait_for_ready(page, timeout=15)
        rows = _parse_eval_json(await page.evaluate(_EXTRACT_SPEEDYAPPLY_ROWS_JS), [])
        print(f"    📄 Found {len(rows)} total rows across SpeedyApply's tables")

        jobs = read_jobs()
        for row in rows:
            if _cancel_requested(cancel_flag):
                print("    🛑 Stop requested — saving progress and leaving SpeedyApply")
                break
            if max_jobs > 0 and len(found) >= max_jobs:
                break
            position = row.get("position", "")
            if not _title_matches_speedyapply_position(title, position):
                continue
            if _is_internship_search(title) and not _matches_requested_internship_role(title, position):
                continue
            url = (row.get("url") or "").strip()
            company = (row.get("company") or "").strip() or "Unknown"
            location = (row.get("location") or "").strip()
            if not url or url in jobs or url in found_urls:
                continue
            if _classify_us_location(location, profile) == "non_us":
                print(f"    ⏭️  Skipped non-US location: {position} — {location}")
                continue

            now = datetime.now(timezone.utc).isoformat()
            job = {
                "url": url,
                "title": position,
                "company": company,
                "location": location,
                "easy_apply": False,
                "description": "",
                "search_title": title,
                "collected_at": now,
                "applied_at": None,
                "error": None,
                "collection_method": "speedyapply_github_table",
            }
            if run_id:
                job["collection_run_id"] = run_id
            job.update(_screening_fields(job, profile, description_complete=False))
            saved, created = atomic_upsert_job(url, job)
            if not created:
                continue
            found_urls.add(url)
            found.append(saved)
            print(f"    💾 Saved 1 new job (total this title: {len(found)}) — {position} at {company}")
    finally:
        try:
            await asyncio.wait_for(browser.close(), timeout=15)
        except Exception as close_err:
            print(f"    ⚠️  Browser cleanup error: {close_err}")

    return found


async def fetch_description_for_job(url: str, job: dict, page=None) -> str:
    """Visit a job page and extract a validated description without clicking Apply."""
    refresh_credentials()
    browser = None

    title = job.get("title", "Unknown")
    company = job.get("company", "unknown")

    try:
        if page is None:
            browser = BrowserSession(**config.browser_session_kwargs())
            await browser.start()
            page = await browser.new_page("about:blank")
        print(f"    🔎 Reading description without clicking Apply: {title} at {company}")
        linkedin_id = _linkedin_job_id(url)
        if linkedin_id:
            if not await _wait_for_linkedin_login(page, url, timeout=120):
                raise RuntimeError("LinkedIn login was not completed")
            details, validation_error = await _wait_for_expected_job(page, linkedin_id, timeout=12)
            if not details:
                raise RuntimeError(validation_error)
        else:
            await page.goto(url)
            await _wait_for_ready(page, timeout=15)
            details = _parse_eval_json(await page.evaluate(_EXTRACT_GENERIC_DETAILS_JS), {})
            if details.get("challenge_page"):
                raise RuntimeError("site security challenge detected")
            if details.get("error_page"):
                raise RuntimeError("expired or error page detected")
        description = (details.get("description") or "").strip()
        if len(description) < _MIN_REAL_DESCRIPTION_LENGTH:
            raise RuntimeError("no complete job description was found")
        updates = {}
        for key in ("title", "company", "location", "easy_apply"):
            value = details.get(key)
            if value not in ("", None, []) and not (
                key == "location"
                and re.search(r"city,\s*state,\s*or\s*zip", str(value), re.IGNORECASE)
            ):
                updates[key] = value
        if updates:
            update_job(_normalize_linkedin_job_url(url) or url, **updates)
        return description
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception as close_err:
                print(f"    ⚠️  Browser cleanup error: {close_err}")


def _apply_fetched_description(url: str, job: dict, description: str, profile: dict) -> dict | None:
    """Persist the description plus an auditable F-1/H-1B screening result."""
    if not description:
        return None
    screened_job = {**job, "description": description}
    screening = _screening_fields(screened_job, profile, description_complete=True)
    update_job(url, description=description, **screening)
    return screening


async def screen_jobs_individually(
    jobs: dict,
    profile: dict,
    cancel_flag: dict | None = None,
    progress_callback=None,
) -> dict:
    """Open every posting in one reusable tab and rerun strict visa screening.

    This path is deliberately read-only in the browser: it navigates to the
    posting, extracts its description, and never clicks Apply. Explicit visa,
    citizenship, U.S.-person, permanent-authorization, and clearance
    restrictions are blocked. Silence stays in Manual Review rather than being
    presented as confirmed F-1/H-1B support.
    """
    summary = {
        "checked": 0,
        "compatible": 0,
        "needs_review": 0,
        "ineligible": 0,
        "fetch_failed": 0,
    }
    if not jobs:
        print("No collected jobs are available for visa screening.")
        return summary

    clear_stale_browser_session_state()
    browser = BrowserSession(**config.browser_session_kwargs())
    try:
        await browser.start()
        page = await browser.new_page("about:blank")
        items = list(jobs.items())
        for index, (url, job) in enumerate(items, start=1):
            if _cancel_requested(cancel_flag):
                print("🛑 Stop requested — remaining jobs were not changed")
                break

            title = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            print(f"[{index}/{len(items)}] Checking F-1/H-1B eligibility: {title} at {company}")
            screening = None
            final_error = ""

            for attempt in range(3):
                try:
                    description = await asyncio.wait_for(
                        fetch_description_for_job(url, job, page=page), timeout=50
                    )
                    screening = _apply_fetched_description(url, job, description, profile)
                    final_error = ""
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    final_error = str(exc)
                    if attempt < 2 and not _cancel_requested(cancel_flag):
                        rate_limited = any(token in final_error.lower() for token in (
                            "429", "rate limit", "security challenge", "unusual activity",
                        ))
                        delay = 20 if rate_limited else 3 * (attempt + 1)
                        print(f"    ⚠️  Check failed ({final_error}); retrying in {delay}s")
                        for _ in range(delay):
                            if _cancel_requested(cancel_flag):
                                break
                            await asyncio.sleep(1)

            if screening:
                result = screening.get("screening_status", "needs_review")
                if result == "compatible":
                    summary["compatible"] += 1
                    print("    🟢 Explicit F-1/OPT/CPT or H-1B support found")
                elif result == "ineligible":
                    summary["ineligible"] += 1
                    evidence = (screening.get("visa_screening_evidence") or "")[:220]
                    print(f"    ⛔ Ineligible: {screening.get('error')} — evidence: {evidence}")
                else:
                    summary["needs_review"] += 1
                    print("    🟡 No explicit support or restriction — kept in Manual Review")
            else:
                summary["fetch_failed"] += 1
                update_job(
                    url,
                    status="manual_review",
                    screening_status="fetch_failed",
                    screening_checked_at=datetime.now(timezone.utc).isoformat(),
                    error=f"Visa check could not read posting: {final_error[:300]}",
                )
                result = "fetch_failed"
                print(f"    ❌ Could not verify posting: {final_error}")

            summary["checked"] += 1
            if progress_callback:
                progress_callback({**summary, "result": result, "url": url})
            if index < len(items) and not _cancel_requested(cancel_flag):
                await asyncio.sleep(1)
    finally:
        try:
            await asyncio.wait_for(browser.close(), timeout=15)
        except Exception as close_err:
            print(f"⚠️  Browser cleanup error: {close_err}")

    print(
        "Visa screening complete: "
        f"{summary['checked']} checked, {summary['compatible']} compatible, "
        f"{summary['needs_review']} need review, {summary['ineligible']} ineligible, "
        f"{summary['fetch_failed']} unreadable"
    )
    return summary


async def collect_descriptions(jobs: dict, profile: dict, cancel_flag: dict | None = None):
    """Fetch descriptions in one reusable, throttled browser session."""
    needs_desc = [
        (url, j) for url, j in jobs.items()
        if j.get("status") in {"pending", "manual_review"}
        and len(j.get("description") or "") < _MIN_REAL_DESCRIPTION_LENGTH
    ]

    if not needs_desc:
        print("All jobs already have descriptions.")
        return

    print(f"\n📋 Fetching descriptions for {len(needs_desc)} jobs...\n")

    clear_stale_browser_session_state()
    browser = BrowserSession(**config.browser_session_kwargs())
    try:
        await browser.start()
        page = await browser.new_page("about:blank")
        for i, (url, job) in enumerate(needs_desc):
            if _cancel_requested(cancel_flag):
                print("  🛑 Stop requested — remaining unscreened jobs stay in Manual Review")
                break
            title = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            print(f"  [{i+1}/{len(needs_desc)}] {title} at {company}...")

            final_error = ""
            for attempt in range(3):
                try:
                    description = await asyncio.wait_for(fetch_description_for_job(url, job, page=page), timeout=50)
                    _apply_fetched_description(url, job, description, profile)
                    print(f"    ✅ Got and screened description ({len(description)} chars)")
                    final_error = ""
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    final_error = str(e)
                    error_l = final_error.lower()
                    rate_limited = any(token in error_l for token in (
                        "http_response_code_failure", "429", "rate limit", "security challenge", "unusual activity"
                    ))
                    if "security token" in error_l or "credentials expired" in error_l:
                        refresh_credentials()
                    if attempt < 2:
                        delay = 20 if rate_limited else 3 * (attempt + 1)
                        print(f"    ⚠️  Fetch failed ({final_error}); retrying in {delay}s")
                        for _ in range(delay):
                            if _cancel_requested(cancel_flag):
                                break
                            await asyncio.sleep(1)
                        if _cancel_requested(cancel_flag):
                            break
            if final_error:
                update_job(
                    url,
                    status="manual_review",
                    screening_status="fetch_failed",
                    screening_checked_at=datetime.now(timezone.utc).isoformat(),
                    error=f"Description could not be verified: {final_error[:300]}",
                )
                print(f"    ❌ Kept in Manual Review: {final_error}")
            if i + 1 < len(needs_desc) and not _cancel_requested(cancel_flag):
                await asyncio.sleep(2)
    finally:
        try:
            await asyncio.wait_for(browser.close(), timeout=15)
        except Exception as close_err:
            print(f"    ⚠️  Browser cleanup error: {close_err}")


async def main():
    parser = argparse.ArgumentParser(description="Collect LinkedIn job listings")
    parser.add_argument("--title", help="Collect for a single job title")
    parser.add_argument("--resume", action="store_true", help="Skip titles already collected")
    parser.add_argument("--skip-descriptions", action="store_true", help="Skip description fetching phase")
    args = parser.parse_args()

    profile = load_json(CANDIDATE_PROFILE, {})
    jobs = load_jobs()
    LOGS_DIR.mkdir(exist_ok=True)

    if args.title:
        titles = [args.title]
    else:
        titles = profile.get("target_job_titles", [])

    # Track which titles have been collected
    collected_titles = set()
    if args.resume:
        for j in jobs.values():
            if "search_title" in j:
                collected_titles.add(j["search_title"])

    # Background credential refresh every 14 min
    cred_task = asyncio.create_task(credential_refresh_loop(14))
    collected_urls_this_run: set[str] = set()

    for i, title in enumerate(titles):
        if args.resume and title in collected_titles:
            print(f"[{i+1}/{len(titles)}] Skipping '{title}' (already collected)")
            continue

        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(titles)}] Collecting: {title}")
        print(f"{'='*60}")

        for attempt in range(3):
            try:
                found = await collect_for_title(title, jobs, profile)
                collected_urls_this_run.update(j.get("url") for j in found if j.get("url"))
                jobs = load_jobs()  # reload since step callback writes directly
                print(f"  Found {len(found)} new jobs (total: {len(jobs)})")
                break
            except Exception as e:
                error_str = str(e).lower()
                if "security token" in error_str or "expired" in error_str:
                    print(f"  🔑 Credentials expired (attempt {attempt+1}/3) — refreshing...")
                    refresh_credentials()
                    if attempt < 2:
                        continue
                print(f"  Error: {e}")
                break

    cred_task.cancel()

    # Phase 2: Fetch descriptions for all pending jobs without one
    if not args.skip_descriptions:
        jobs = load_jobs()  # reload latest
        if collected_urls_this_run:
            jobs = {url: job for url, job in jobs.items() if url in collected_urls_this_run}
        cred_task2 = asyncio.create_task(credential_refresh_loop(14))
        await collect_descriptions(jobs, profile)
        cred_task2.cancel()
        jobs = load_jobs()  # reload after descriptions

    # Summary
    jobs = load_jobs()
    has_desc = sum(1 for j in jobs.values() if j.get("description"))
    easy = sum(1 for j in jobs.values() if j.get("easy_apply"))
    non_easy = len(jobs) - easy
    pending = sum(1 for j in jobs.values() if j.get("status") == "pending")
    print(f"\n{'='*60}")
    print(f"Collection complete!")
    print(f"Total jobs: {len(jobs)} (Easy Apply: {easy}, Non-Easy Apply: {non_easy})")
    print(f"Jobs with descriptions: {has_desc}/{len(jobs)}")
    print(f"Pending applications: {pending}")


if __name__ == "__main__":
    asyncio.run(main())
