"""Static autofill facts and deterministic form filling helpers.

The LLM is good at weird, contextual questions. It is wasteful and brittle for
stable facts like name, email, phone, school, address, and password fields. This
module keeps those facts in a local text file and applies them directly in the
browser after every agent step.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import Any

try:
    from core.config import get_data_dir, load_settings
except ImportError:
    from backend.core.config import get_data_dir, load_settings


AUTOFILL_FACTS_FILE = get_data_dir() / "autofill_facts.txt"


_FACT_ORDER = [
    "full_name",
    "first_name",
    "last_name",
    "email",
    "account_email",
    "account_password",
    "confirm_password",
    "phone",
    "phone_country_code",
    "phone_full",
    "linkedin_url",
    "street_address",
    "city",
    "state",
    "postal_code",
    "country",
    "citizenship",
    "nationality",
    "country_of_residence",
    "current_location",
    "school",
    "degree",
    "major",
    "graduation",
    "graduation_date",
    "graduation_date_iso",
    "graduation_year",
    "graduation_term",
    "school_start_date",
    "education_start_date",
    "education_end_date",
    "gpa",
    "current_role",
    "current_employer",
    "years_of_experience",
    "prior_internships",
    "date_of_birth",
    "date_of_birth_iso",
    "date_of_birth_month",
    "date_of_birth_day",
    "date_of_birth_year",
    "age",
    "todays_date",
    "age_over_18",
    "work_authorization",
    "current_work_status",
    "authorized_to_work_us",
    "visa_sponsorship_needed",
    "future_sponsorship_needed",
    "h1b_sponsorship_needed",
    "visa_status",
    "immigration_status",
    "f1_visa_status",
    "f1_status",
    "visa_expiration_date",
    "visa_expiration_date_iso",
    "cpt_eligible",
    "cpt_status",
    "opt_eligible",
    "opt_status",
    "internship_availability",
    "earliest_start_date",
    "latest_end_date",
    "max_hours_per_week",
    "willing_to_relocate",
    "preferred_work_mode",
    "preferred_us_locations",
    "role_interest",
    "heard_about",
    "previously_worked_for_company",
    "resume_path",
    "github_url",
    "has_github",
    "portfolio_url",
    "veteran_status",
    "sexual_orientation",
    "disability_status",
    "gender",
    "pronouns",
    "race_ethnicity",
    "hispanic_latino",
    "driver_license",
    "accommodations_needed",
    "desired_pay",
    "job_title",
    "job_company",
    "interest_statement",
    "earliest_start_date_date",
    "earliest_start_date_iso",
    "internship_end_date",
    "internship_end_date_iso",
]


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).strip()


def _name_parts(full_name: str) -> tuple[str, str]:
    parts = [p for p in full_name.split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _major_from_degree(degree: str) -> str:
    if "computer science" in degree.lower():
        return "Computer Science"
    return ""


def _extract_year(value: str) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", value or "")
    return match.group(0) if match else ""


def _age_from_dob(date_of_birth: str, today: date | None = None) -> int | str:
    """Return the candidate's age in whole years, or "" if the date is unusable.

    Age has to be derived rather than stored: a written-down number is correct
    only until the next birthday, and applications ask for it directly.
    """
    parsed = _date_parts(date_of_birth)
    if not (parsed.get("year") and parsed.get("month") and parsed.get("day")):
        return ""
    try:
        born = date(int(parsed["year"]), int(parsed["month"]), int(parsed["day"]))
    except ValueError:
        return ""
    now = today or date.today()
    return now.year - born.year - ((now.month, now.day) < (born.month, born.day))


def _date_parts(value: str) -> dict[str, str]:
    """Return month/day/year strings for common US and ISO date formats."""
    text = _string(value)
    if not text:
        return {}
    iso = re.match(r"^\s*((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})\s*$", text)
    if iso:
        year, month, day = iso.groups()
        return {"year": year, "month": str(int(month)), "day": str(int(day))}
    us = re.match(r"^\s*(\d{1,2})[-/](\d{1,2})[-/]((?:19|20)\d{2})\s*$", text)
    if us:
        month, day, year = us.groups()
        return {"year": year, "month": str(int(month)), "day": str(int(day))}
    return {"year": _extract_year(text)}


def _graduation_date_parts(value: str) -> tuple[str, str]:
    """Return a deterministic graduation date for term/year answers."""
    text = _string(value)
    year = _extract_year(text)
    if not year:
        return "", ""
    lowered = text.lower()
    if "fall" in lowered or "winter" in lowered:
        month, day = "12", "15"
    elif "summer" in lowered:
        month, day = "08", "15"
    else:
        # Typical US spring graduation season.
        month, day = "05", "15"
    return f"{month}/{day}/{year}", f"{year}-{month}-{day}"


def _profile_value(profile: dict[str, Any], key: str, default: str = "") -> str:
    value = profile.get(key)
    if value in (None, ""):
        return default
    return _string(value)


def _default_facts(profile: dict[str, Any], settings: dict[str, Any] | None = None, resume_path: str = "") -> dict[str, str]:
    settings = settings or load_settings()
    sensitive = settings.get("sensitive_data") or {}
    address = profile.get("address") or {}
    education = profile.get("education") or {}

    full_name = _string(profile.get("name"))
    first_name, last_name = _name_parts(full_name)
    phone = _string(profile.get("phone"))
    country_code = _string(profile.get("phone_country_code"))
    degree = _string(education.get("degree"))
    graduation = _string(education.get("graduation"))
    graduation_date, graduation_date_iso = _graduation_date_parts(graduation)
    dob = _profile_value(profile, "date_of_birth")
    dob_iso = _profile_value(profile, "date_of_birth_iso")
    dob_parts = _date_parts(dob_iso or dob)
    account_email = _string(sensitive.get("email"))
    account_password = _string(sensitive.get("password"))
    profile_email = _string(profile.get("email"))
    city = _string(address.get("city"))
    state = _string(address.get("state"))

    if not account_email:
        account_email = profile_email

    facts = {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": profile_email or account_email,
        "account_email": account_email,
        # Passwords are loaded from Settings at runtime. The text file can also
        # override them if the user intentionally adds a local value.
        "account_password": account_password,
        "confirm_password": account_password,
        "phone": phone,
        "phone_country_code": country_code,
        "phone_full": f"{country_code}{phone}" if country_code and phone else phone,
        "linkedin_url": _string(profile.get("linkedin_url") or profile.get("linkedin")),
        "street_address": _string(address.get("street")),
        "city": city,
        "state": state,
        "postal_code": _string(address.get("zip") or address.get("postal_code")),
        "country": _string(address.get("country") or profile.get("country")),
        "citizenship": _profile_value(profile, "citizenship") or _profile_value(profile, "nationality"),
        "nationality": _profile_value(profile, "nationality") or _profile_value(profile, "citizenship"),
        "country_of_residence": _profile_value(profile, "country_of_residence"),
        "current_location": ", ".join(x for x in (city, state) if x),
        "school": _string(education.get("school")),
        "degree": degree,
        "major": _string(education.get("major")) or _major_from_degree(degree),
        "graduation": graduation,
        "graduation_date": graduation_date,
        "graduation_date_iso": graduation_date_iso,
        "graduation_year": _extract_year(graduation),
        "graduation_term": _profile_value(profile, "graduation_term"),
        "school_start_date": _profile_value(profile, "school_start_date"),
        "education_start_date": _profile_value(profile, "education_start_date") or _profile_value(profile, "school_start_date"),
        "education_end_date": _profile_value(profile, "education_end_date") or graduation,
        "gpa": _profile_value(profile, "gpa"),
        "current_role": _string(profile.get("current_role")),
        "current_employer": _string(profile.get("current_employer")) or _string(education.get("school")),
        "years_of_experience": _string(profile.get("years_of_experience")),
        "prior_internships": _profile_value(profile, "prior_internships", "0"),
        "date_of_birth": dob,
        "date_of_birth_iso": dob_iso,
        "date_of_birth_month": dob_parts.get("month", ""),
        "date_of_birth_day": dob_parts.get("day", ""),
        "date_of_birth_year": dob_parts.get("year", ""),
        "age": _profile_value(profile, "age"),
        # EEO self-identification forms ask the candidate to date their
        # signature, which is always the day the form is filled in.
        "todays_date": date.today().strftime("%m/%d/%Y"),
        "age_over_18": _profile_value(profile, "age_over_18"),
        "work_authorization": _string(profile.get("work_authorization")),
        "current_work_status": _profile_value(profile, "current_work_status"),
        "authorized_to_work_us": "yes" if _string(profile.get("work_authorization")) else "",
        "visa_sponsorship_needed": _string(profile.get("visa_sponsorship_needed")),
        "future_sponsorship_needed": _profile_value(profile, "future_sponsorship_needed") or _string(profile.get("visa_sponsorship_needed")),
        "h1b_sponsorship_needed": _profile_value(profile, "h1b_sponsorship_needed") or _string(profile.get("visa_sponsorship_needed")),
        "visa_status": _profile_value(profile, "visa_status") or _profile_value(profile, "f1_visa_status"),
        "immigration_status": _profile_value(profile, "immigration_status") or _profile_value(profile, "current_work_status"),
        "f1_visa_status": _profile_value(profile, "f1_visa_status"),
        "f1_status": _profile_value(profile, "f1_status") or _profile_value(profile, "f1_visa_status"),
        "visa_expiration_date": _profile_value(profile, "visa_expiration_date"),
        "visa_expiration_date_iso": _profile_value(profile, "visa_expiration_date_iso"),
        "cpt_eligible": _profile_value(profile, "cpt_eligible"),
        "cpt_status": _profile_value(profile, "cpt_status") or _profile_value(profile, "cpt_eligible"),
        "opt_eligible": _profile_value(profile, "opt_eligible"),
        "opt_status": _profile_value(profile, "opt_status") or _profile_value(profile, "opt_eligible"),
        "internship_availability": _profile_value(profile, "internship_availability"),
        "earliest_start_date": _profile_value(profile, "earliest_start_date"),
        "earliest_start_date_date": _profile_value(profile, "earliest_start_date_date"),
        "earliest_start_date_iso": _profile_value(profile, "earliest_start_date_iso"),
        "internship_end_date": _profile_value(profile, "internship_end_date") or _profile_value(profile, "latest_end_date"),
        "internship_end_date_iso": _profile_value(profile, "internship_end_date_iso"),
        "latest_end_date": _profile_value(profile, "latest_end_date"),
        "max_hours_per_week": _profile_value(profile, "max_hours_per_week"),
        "willing_to_relocate": _string(profile.get("willing_to_relocate")),
        "preferred_work_mode": _string(profile.get("preferred_work_mode")),
        "preferred_us_locations": ", ".join(profile.get("target_locations") or []) or _profile_value(profile, "preferred_us_locations"),
        "role_interest": _profile_value(profile, "role_interest", "Software Engineering, Backend, Platform, Data, Infrastructure"),
        "heard_about": _profile_value(profile, "heard_about", "LinkedIn"),
        "previously_worked_for_company": _profile_value(profile, "previously_worked_for_company", "no"),
        "resume_path": _string(resume_path or settings.get("resume_path")),
        "github_url": _string(profile.get("github_url") or profile.get("github")),
        "has_github": _profile_value(profile, "has_github"),
        "portfolio_url": _string(profile.get("portfolio_url") or profile.get("portfolio") or profile.get("website")),
        "veteran_status": _string(profile.get("veteran_status")) or "Prefer not to answer",
        "sexual_orientation": _string(profile.get("sexual_orientation")) or "Prefer not to answer",
        "disability_status": _string(profile.get("disability_status")) or "Prefer not to answer",
        "gender": _string(profile.get("gender")) or "Prefer not to say",
        "pronouns": _string(profile.get("pronouns")) or ("He/Him/His" if "male" in _string(profile.get("gender")).lower() else ""),
        "race_ethnicity": _string(profile.get("race_ethnicity")) or "Prefer not to say",
        "hispanic_latino": _profile_value(profile, "hispanic_latino"),
        "driver_license": _profile_value(profile, "driver_license"),
        "accommodations_needed": _profile_value(profile, "accommodations_needed"),
        "desired_pay": _profile_value(profile, "desired_pay"),
    }
    return facts


def _parse_facts_file(path: Path = AUTOFILL_FACTS_FILE) -> dict[str, str]:
    facts: dict[str, str] = {}
    if not path.exists():
        return facts
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = re.sub(r"[^a-zA-Z0-9_]", "_", key.strip()).lower()
        if key:
            facts[key] = value.strip()
    return facts


def _facts_template(defaults: dict[str, str]) -> str:
    lines = [
        "# Static autofill facts for job applications.",
        "# Format: key=value",
        "# The app reads this before using the LLM, then locks these values in the browser.",
        "# Keep account_password blank if you prefer to store it only in Settings.",
        "",
    ]
    for key in _FACT_ORDER:
        value = defaults.get(key, "")
        if key in {"account_password", "confirm_password"}:
            value = ""
        lines.append(f"{key}={value}")
    lines.append("")
    lines.append("# Add new stable answers below. Existing non-empty keys override profile/settings.")
    return "\n".join(lines) + "\n"


def ensure_autofill_facts_file(profile: dict[str, Any], resume_path: str = "") -> Path:
    defaults = _default_facts(profile, resume_path=resume_path)
    AUTOFILL_FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not AUTOFILL_FACTS_FILE.exists():
        AUTOFILL_FACTS_FILE.write_text(_facts_template(defaults), encoding="utf-8")
    else:
        existing = _parse_facts_file(AUTOFILL_FACTS_FILE)
        missing = [key for key in _FACT_ORDER if key not in existing]
        if missing:
            with AUTOFILL_FACTS_FILE.open("a", encoding="utf-8") as f:
                f.write("\n# Added by app after profile/schema update.\n")
                for key in missing:
                    value = "" if key in {"account_password", "confirm_password"} else defaults.get(key, "")
                    f.write(f"{key}={value}\n")

    try:
        os.chmod(AUTOFILL_FACTS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return AUTOFILL_FACTS_FILE


def load_autofill_facts(profile: dict[str, Any], resume_path: str = "") -> dict[str, str]:
    settings = load_settings()
    defaults = _default_facts(profile, settings=settings, resume_path=resume_path)
    ensure_autofill_facts_file(profile, resume_path=resume_path)
    file_facts = _parse_facts_file()

    merged = dict(defaults)
    for key, value in file_facts.items():
        if value:
            merged[key] = value

    # Confirm password should mirror account password unless explicitly supplied.
    if merged.get("account_password") and not merged.get("confirm_password"):
        merged["confirm_password"] = merged["account_password"]

    if merged.get("graduation") and not merged.get("graduation_year"):
        merged["graduation_year"] = _extract_year(merged["graduation"])
    if merged.get("school_start_date") and not merged.get("education_start_date"):
        merged["education_start_date"] = merged["school_start_date"]
    if merged.get("graduation") and not merged.get("education_end_date"):
        merged["education_end_date"] = merged["graduation"]
    if merged.get("current_work_status") and not merged.get("immigration_status"):
        merged["immigration_status"] = merged["current_work_status"]
    if merged.get("f1_visa_status") and not merged.get("f1_status"):
        merged["f1_status"] = merged["f1_visa_status"]
    if merged.get("f1_status") and not merged.get("visa_status"):
        merged["visa_status"] = merged["f1_status"]
    if merged.get("cpt_eligible") and not merged.get("cpt_status"):
        merged["cpt_status"] = merged["cpt_eligible"]
    if merged.get("opt_eligible") and not merged.get("opt_status"):
        merged["opt_status"] = merged["opt_eligible"]
    dob_parts = _date_parts(merged.get("date_of_birth_iso") or merged.get("date_of_birth") or "")
    for part_key, merged_key in {
        "month": "date_of_birth_month",
        "day": "date_of_birth_day",
        "year": "date_of_birth_year",
    }.items():
        if dob_parts.get(part_key) and not merged.get(merged_key):
            merged[merged_key] = dob_parts[part_key]
    # Age is derived last and overrides whatever was stored, because a written
    # down age silently goes wrong on the candidate's next birthday.
    derived_age = _age_from_dob(merged.get("date_of_birth_iso") or merged.get("date_of_birth") or "")
    if derived_age != "":
        merged["age"] = str(derived_age)
        merged["age_over_18"] = "yes" if int(derived_age) >= 18 else "no"
    facts = {k: v for k, v in merged.items() if v}
    # Answers the candidate has already given for questions no rule covers.
    # These fill deterministically, before the LLM is involved at all.
    banked = _load_banked_answers()
    if banked:
        facts["__qa__"] = banked
    return facts


def _load_banked_answers() -> dict[str, str]:
    """Return answered Q&A pairs from the repository, empty if unavailable."""
    try:
        from core.shared_config import get_memory_store
    except ImportError:
        try:
            from backend.core.shared_config import get_memory_store
        except ImportError:
            return {}
    try:
        store = get_memory_store()
        if not store:
            return {}
        return {q: a for q, a in (store.qa_get_all_for_prompt() or {}).items() if a}
    except Exception:
        return {}


def format_facts_for_prompt(facts: dict[str, str]) -> str:
    safe_lines = []
    for key in _FACT_ORDER:
        if key not in facts:
            continue
        if "password" in key:
            safe_lines.append(f"- {key}: <secret>password</secret>")
        else:
            safe_lines.append(f"- {key}: {facts[key]}")
    if not safe_lines:
        return ""
    return (
        "STATIC AUTOFILL FACTS:\n"
        + "\n".join(safe_lines)
        + "\n\nRules:\n"
        "- Static autofill runs in code after every step and locks these values.\n"
        "- Do not clear, rewrite, or second-guess fields already filled with these facts.\n"
        "- Only use the LLM for empty custom questions that cannot be answered from static facts or saved Q&A.\n"
    )


def _autofill_script(facts: dict[str, str]) -> str:
    payload = json.dumps(facts)
    return f"""
(async () => {{
  const facts = {payload};
  const result = {{
    filled: 0,
    restored: 0,
    unknownTextboxes: 0,
    passwordFieldsWithoutPassword: 0,
    verificationCodeRequired: false,
    requiredEmpty: 0,
    invalidFields: 0,
    credentialError: false,
    selects: 0,
    choices: 0,
    locked: 0,
    abandoned: 0,
    url: location.href,
    needsLlm: [],
    abandonedLabels: [],
    openQuestions: [],
    requiredEmptyLabels: [],
    visibleErrors: [],
    matches: [],
    debugInputs: []
  }};
  window.__STATIC_AUTOFILL_LOCKS = window.__STATIC_AUTOFILL_LOCKS || {{}};
  window.__STATIC_CHOICE_LOCKS = window.__STATIC_CHOICE_LOCKS || {{}};
  if (!window.__STATIC_CHOICE_CLICK_GUARD?.installed) {{
    window.__STATIC_CHOICE_CLICK_GUARD = {{ installed: true, blocked: 0, bypass: false }};
    const lockedChoiceTarget = (target) => {{
      if (!target?.closest) return null;
      const direct = target.closest('[data-static-choice-locked="true"]');
      if (direct) return direct;
      const label = target.closest('label');
      if (label?.querySelector?.('input[data-static-choice-locked="true"]')) return label;
      const candidate = target.closest('button, [role="radio"], [role="checkbox"], [aria-checked], span, div');
      if (candidate?.querySelector?.('input[data-static-choice-locked="true"]')) return candidate;
      return null;
    }};
    const blockLockedChoiceClick = (event) => {{
      if (window.__STATIC_CHOICE_CLICK_GUARD?.bypass) return;
      const target = lockedChoiceTarget(event.target);
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      window.__STATIC_CHOICE_CLICK_GUARD.blocked += 1;
    }};
    for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {{
      document.addEventListener(eventName, blockLockedChoiceClick, true);
    }}
  }}

  const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const has = (text, words) => words.some((w) => text.includes(w));
  const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
  const boolYes = (v) => ['true', 'yes', 'y', '1'].includes(norm(v));
  const factBool = (key) => boolYes(facts[key]);
  const hasAny = (text, words) => words.some((w) => text.includes(w));
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const pushLimited = (list, value, limit = 8) => {{
    const cleaned = clean(value).slice(0, 180);
    if (cleaned && !list.includes(cleaned) && list.length < limit) list.push(cleaned);
  }};

  function allElements(selector, root = document) {{
    const found = [];
    try {{ found.push(...Array.from(root.querySelectorAll(selector))); }} catch (_) {{}}
    const nodes = [];
    try {{ nodes.push(...Array.from(root.querySelectorAll('*'))); }} catch (_) {{}}
    for (const node of nodes) {{
      if (node.shadowRoot) found.push(...allElements(selector, node.shadowRoot));
    }}
    return found;
  }}

  function visible(el) {{
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && rect.width > 1
      && rect.height > 1
      && !el.disabled
      && !el.readOnly;
  }}

  // Greenhouse (and others) wrap each custom-question <input> in its own
  // open shadow root, with the actual visible question text rendered
  // outside it in the light DOM. `.closest()`, `.previousElementSibling`,
  // and `.parentElement` all stop dead at a shadow boundary by spec — they
  // do not climb into the light DOM around the shadow host. Any label-text
  // lookup that walks ancestors/siblings from `el` directly therefore reads
  // almost nothing for these inputs (just id/name attributes), and every
  // downstream keyword/fuzzy match effectively runs on garbage — which is
  // how "How did you hear about this job?" ended up locked in as
  // linkedin_url and "restrictive covenants" as current_employer on a real
  // Greenhouse form. Climbing out to the shadow root's host element first
  // (repeated for nested shadow roots) puts the traversal back in the
  // light DOM where the real question text actually lives.
  function escapeShadowBoundary(el) {{
    let current = el;
    let guard = 0;
    while (current && guard < 8) {{
      const root = current.getRootNode?.();
      if (root && root.host && root !== document) {{
        current = root.host;
        guard += 1;
      }} else {{
        break;
      }}
    }}
    return current || el;
  }}

  const QUESTION_CONTAINER_SELECTOR = [
    '.application-question',
    '[class*="application-question"]',
    '[class*="question"]',
    '[class*="Question"]',
    '[data-testid*="question"]',
    'fieldset',
    'li',
  ].join(',');

  // The question as a person would read it, for banking so the candidate can
  // answer it once and have it reused. fieldSummary is unusable here: it is
  // built for matching, so it carries UUIDs, CSS class names, repeated label
  // fragments and text bled in from neighbouring questions.
  function questionText(el) {{
    const candidates = [];
    const host = escapeShadowBoundary(el);
    // Ashby commonly renders a question as a sibling immediately before the
    // field wrapper instead of using a <label>. Prefer that local text over
    // the broad matching summary, which can contain neighbouring questions.
    const hostPrevious = host.previousElementSibling;
    if (hostPrevious) candidates.push(clean(hostPrevious.innerText || hostPrevious.textContent || ''));
    const hostParentPrevious = host.parentElement?.previousElementSibling;
    if (hostParentPrevious) candidates.push(clean(hostParentPrevious.innerText || hostParentPrevious.textContent || ''));
    const container = host.closest(QUESTION_CONTAINER_SELECTOR);
    if (container) {{
      // The question sits in the container but outside the control's own
      // wrapper, so remove the wrapper's text rather than the whole container's.
      const own = clean(container.innerText || '');
      const field = host.closest('[class*="field"], [class*="Field"], [class*="input"], [class*="Input"]');
      const fieldText = field && field !== container ? clean(field.innerText || '') : '';
      let containerText = fieldText && own.startsWith(fieldText) === false ? own.replace(fieldText, ' ') : own;
      // A button's own visible text (e.g. Workday's "Select One" trigger
      // label) is never part of the question, but it does sit inside the
      // question container's innerText -- without stripping it, "Highest
      // level of education?" banked to Q&A as "Highest level of education?
      // Select One". Selects are excluded: their .textContent concatenates
      // every <option>, which could strip real label words that happen to
      // overlap with an option's text.
      if (host.tagName === 'BUTTON') {{
        const ownButtonText = clean(host.innerText || host.textContent || '');
        if (ownButtonText) containerText = clean(containerText.replace(ownButtonText, ' '));
      }}
      candidates.push(containerText);
    }}
    if (el.id) {{
      const root = el.getRootNode?.() || document;
      try {{
        root.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`).forEach((l) => candidates.push(clean(l.innerText)));
      }} catch (_) {{}}
    }}
    const wrappingLabel = host.closest('label');
    if (wrappingLabel) candidates.push(clean(wrappingLabel.innerText));
    candidates.push(clean(el.getAttribute('aria-label') || ''));
    for (let text of candidates) {{
      if (!text) continue;
      text = text
        .replace(/[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}/gi, ' ')
        .replace(/\\bcards?\\[[^\\]]*\\]/gi, ' ')
        .replace(/\\b[a-z-]*(?:card-field-input|select__input|input__single-line)[a-z-]*\\b/gi, ' ')
        .replace(/\\s+/g, ' ')
        .trim();
      // Labels are frequently duplicated by the surrounding markup.
      const half = text.slice(0, Math.floor(text.length / 2)).trim();
      if (half && text.slice(Math.floor(text.length / 2)).trim() === half) text = half;
      if (text.length >= 8 && text.length <= 300) return text;
    }}
    return '';
  }}

  function labelText(el) {{
    const pieces = [
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.getAttribute('autocomplete'),
      el.getAttribute('data-testid'),
      el.getAttribute('data-qa'),
      el.getAttribute('class'),
      el.getAttribute('data-field'),
      el.getAttribute('data-field-name'),
      el.getAttribute('data-automation-id'),
    ];
    if (el.id) {{
      const root = el.getRootNode?.() || document;
      try {{
        root.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`).forEach((label) => pieces.push(label.innerText));
      }} catch (_) {{}}
    }}
    const host = escapeShadowBoundary(el);
    const wrappingLabel = host.closest('label');
    if (wrappingLabel) pieces.push(wrappingLabel.innerText);
    let sibling = host.previousElementSibling;
    for (let i = 0; sibling && i < 4; i += 1, sibling = sibling.previousElementSibling) {{
      const siblingText = clean(sibling.innerText || sibling.textContent || '');
      if (siblingText && siblingText.length <= 500) pieces.push(siblingText);
    }}
    const parent = host.closest('[data-testid], [data-qa], .form-group, .field, [class*="field"], [class*="Field"], .application-question, [class*="question"], [class*="Question"], [role="group"], li, fieldset, div');
    if (parent) {{
      const parentText = clean(parent.innerText || parent.textContent || '');
      if (parentText.length <= 900) pieces.push(parentText);
      let parentSibling = parent.previousElementSibling;
      for (let i = 0; parentSibling && i < 4; i += 1, parentSibling = parentSibling.previousElementSibling) {{
        const siblingText = clean(parentSibling.innerText || parentSibling.textContent || '');
        if (siblingText && siblingText.length <= 700) pieces.push(siblingText);
      }}
    }}
    // The lookup above lists a bare "div", and closest() returns the nearest
    // match, so it stops at whatever wrapper immediately encloses the control.
    // Lever/Ashby keep the question text one level further out (
    // li.application-question > div.application-label + div.application-field),
    // so look again for a question container specifically. Additive: whatever
    // the generic lookup already found is still included.
    const questionParent = host.closest(QUESTION_CONTAINER_SELECTOR);
    if (questionParent && questionParent !== parent) {{
      const questionText = clean(questionParent.innerText || questionParent.textContent || '');
      if (questionText && questionText.length <= 900) pieces.push(questionText);
    }}
    return norm(pieces.filter(Boolean).join(' '));
  }}

  function labelTextRaw(el) {{
    const pieces = [
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.getAttribute('autocomplete'),
      el.getAttribute('data-testid'),
      el.getAttribute('data-qa'),
      el.getAttribute('class'),
      el.getAttribute('data-field'),
      el.getAttribute('data-field-name'),
      el.getAttribute('data-automation-id'),
    ];
    if (el.id) {{
      const root = el.getRootNode?.() || document;
      try {{
        root.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`).forEach((label) => pieces.push(label.innerText));
      }} catch (_) {{}}
    }}
    const host = escapeShadowBoundary(el);
    const wrappingLabel = host.closest('label');
    if (wrappingLabel) pieces.push(wrappingLabel.innerText);
    let sibling = host.previousElementSibling;
    for (let i = 0; sibling && i < 4; i += 1, sibling = sibling.previousElementSibling) {{
      const siblingText = clean(sibling.innerText || sibling.textContent || '');
      if (siblingText && siblingText.length <= 500) pieces.push(siblingText);
    }}
    const parent = host.closest('[data-testid], [data-qa], .form-group, .field, [class*="field"], [class*="Field"], .application-question, [class*="question"], [class*="Question"], [role="group"], li, fieldset, div');
    if (parent) {{
      const parentText = clean(parent.innerText || parent.textContent || '');
      if (parentText.length <= 900) pieces.push(parentText);
      let parentSibling = parent.previousElementSibling;
      for (let i = 0; parentSibling && i < 4; i += 1, parentSibling = parentSibling.previousElementSibling) {{
        const siblingText = clean(parentSibling.innerText || parentSibling.textContent || '');
        if (siblingText && siblingText.length <= 700) pieces.push(siblingText);
      }}
    }}
    // The lookup above lists a bare "div", and closest() returns the nearest
    // match, so it stops at whatever wrapper immediately encloses the control.
    // Lever/Ashby keep the question text one level further out (
    // li.application-question > div.application-label + div.application-field),
    // so look again for a question container specifically. Additive: whatever
    // the generic lookup already found is still included.
    const questionParent = host.closest(QUESTION_CONTAINER_SELECTOR);
    if (questionParent && questionParent !== parent) {{
      const questionText = clean(questionParent.innerText || questionParent.textContent || '');
      if (questionText && questionText.length <= 900) pieces.push(questionText);
    }}
    return clean(pieces.filter(Boolean).join(' '));
  }}

  function primaryLabelCandidates(el) {{
    const pieces = [
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.getAttribute('autocomplete'),
      el.getAttribute('data-field'),
      el.getAttribute('data-field-name'),
      el.getAttribute('data-automation-id'),
    ];
    if (el.id) {{
      const root = el.getRootNode?.() || document;
      try {{
        root.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`).forEach((label) => pieces.push(label.innerText));
      }} catch (_) {{}}
    }}
    const wrappingLabel = escapeShadowBoundary(el).closest('label');
    if (wrappingLabel) pieces.push(wrappingLabel.innerText);
    return [...new Set(pieces.map(norm).filter((piece) => piece.length >= 3 && piece.length <= 100))];
  }}

  function preferPrimaryFieldText(el, fallback) {{
    const primary = primaryLabelCandidates(el).join(' ');
    const semanticWords = [
      'name', 'email', 'phone', 'mobile', 'address', 'city', 'state', 'province', 'postal', 'zip',
      'country', 'citizen', 'nationality', 'residence', 'school', 'university', 'college', 'degree',
      'major', 'graduation', 'gpa', 'employer', 'company', 'job title', 'experience', 'birth', 'age',
      'visa', 'status', 'authorization', 'sponsor', 'cpt', 'opt', 'availability', 'available', 'start',
      'end date', 'hours', 'salary', 'pay', 'linkedin', 'github', 'portfolio', 'pronoun', 'gender',
      'race', 'ethnicity', 'hispanic', 'latino', 'veteran', 'disability', 'accommodation', 'driver',
      'license', 'licence', 'relocate', 'work mode', 'remote', 'hybrid', 'onsite'
    ];
    return primary && hasAny(primary, semanticWords) ? primary : fallback;
  }}

  function editSimilarity(left, right) {{
    const a = norm(left);
    const b = norm(right);
    if (!a || !b) return 0;
    if (a === b) return 1;
    if ((a.includes(b) || b.includes(a)) && Math.min(a.length, b.length) >= 5) return 0.96;
    const previous = Array.from({{ length: b.length + 1 }}, (_, index) => index);
    for (let i = 1; i <= a.length; i += 1) {{
      const current = [i];
      for (let j = 1; j <= b.length; j += 1) {{
        current[j] = Math.min(
          current[j - 1] + 1,
          previous[j] + 1,
          previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)
        );
      }}
      previous.splice(0, previous.length, ...current);
    }}
    return 1 - (previous[b.length] / Math.max(a.length, b.length));
  }}

  function pickFuzzyTextFact(el) {{
    const definitions = [
      ['first_name', facts.first_name, ['first name', 'given name', 'legal first name'], ['text']],
      ['last_name', facts.last_name, ['last name', 'family name', 'surname', 'legal last name'], ['text']],
      ['full_name', facts.full_name, ['full name', 'legal full name'], ['text']],
      ['email', facts.email, ['email address', 'contact email', 'personal email'], ['text', 'email']],
      ['phone_full', facts.phone_full || facts.phone, ['phone number', 'mobile number', 'cell phone'], ['text', 'tel']],
      ['street_address', facts.street_address, ['street address', 'address line 1', 'home address'], ['text']],
      ['city', facts.city, ['city', 'town'], ['text']],
      ['state', facts.state, ['state province', 'state or province', 'province region'], ['text']],
      ['postal_code', facts.postal_code, ['postal code', 'zip code', 'postcode'], ['text']],
      ['country', facts.country, ['address country', 'mailing country'], ['text']],
      ['citizenship', facts.citizenship, ['country of citizenship', 'citizenship country', 'citizenship'], ['text']],
      ['nationality', facts.nationality, ['nationality', 'country of nationality'], ['text']],
      ['country_of_residence', facts.country_of_residence, ['country of residence', 'legal residence country'], ['text']],
      ['school', facts.school, ['school name', 'university name', 'college name'], ['text']],
      ['degree', facts.degree, ['degree', 'degree type', 'qualification'], ['text']],
      ['major', facts.major, ['major', 'field of study', 'academic discipline'], ['text']],
      ['gpa', facts.gpa, ['gpa', 'grade point average'], ['text', 'number']],
      ['graduation_date_iso', facts.graduation_date_iso || facts.graduation_date, ['graduation date', 'expected graduation date'], ['date', 'text']],
      ['graduation_year', facts.graduation_year, ['graduation year', 'expected graduation year'], ['text', 'number']],
      ['earliest_start_date_iso', facts.earliest_start_date_iso || facts.earliest_start_date_date, ['earliest start date', 'available start date'], ['date', 'text']],
      ['internship_end_date_iso', facts.internship_end_date_iso || facts.internship_end_date, ['internship end date', 'available end date', 'latest end date'], ['date', 'text']],
      ['visa_expiration_date_iso', facts.visa_expiration_date_iso || facts.visa_expiration_date, ['visa expiration date', 'visa expiry date', 'status expiration date'], ['date', 'text']],
    ];
    const inputType = norm(el.type || 'text') || 'text';
    const candidates = primaryLabelCandidates(el);
    let best = null;
    let secondBestScore = 0;
    for (const [field, value, aliases, allowedTypes] of definitions) {{
      if (!value) continue;
      if (!allowedTypes.includes(inputType) && !(inputType === 'search' && allowedTypes.includes('text'))) continue;
      let score = 0;
      for (const candidate of candidates) {{
        for (const alias of aliases) score = Math.max(score, editSimilarity(candidate, alias));
      }}
      if (!best || score > best.score) {{
        secondBestScore = best?.score || secondBestScore;
        best = {{ field, value, score }};
      }} else if (score > secondBestScore) {{
        secondBestScore = score;
      }}
    }}
    if (!best || best.score < 0.80 || best.score - secondBestScore < 0.05) return [null, null];
    el.dataset.staticMatchSource = 'fuzzy';
    el.dataset.staticMatchConfidence = best.score.toFixed(2);
    return [best.field, best.value];
  }}

  function fieldSummary(el) {{
    return clean(labelTextRaw(el) || el.getAttribute('name') || el.getAttribute('id') || el.getAttribute('placeholder') || el.tagName).slice(0, 180);
  }}

  function signature(el) {{
    return [
      el.tagName,
      el.type || '',
      el.name || '',
      el.id || '',
      el.getAttribute('class') || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('placeholder') || '',
      labelText(el).slice(0, 160)
    ].join('|');
  }}

  function setNativeValue(el, value) {{
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    const previous = el.value;
    try {{ el.focus(); }} catch (_) {{}}
    setter ? setter.call(el, value) : (el.value = value);
    // React tracks the last DOM value separately. Resetting the tracker to the
    // previous value makes its delegated input handler observe this update.
    try {{ el._valueTracker?.setValue(previous); }} catch (_) {{}}
    try {{
      el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: String(value) }}));
    }} catch (_) {{
      el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    }}
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    try {{ el.blur(); }} catch (_) {{ el.dispatchEvent(new Event('blur', {{ bubbles: true }})); }}
    el.dataset.staticReactSynced = 'true';
  }}

  function looksLikeComboboxWidget(el) {{
    const role = norm(el.getAttribute?.('role'));
    const cls = norm(el.className || '');
    return role.includes('combobox')
      || !!el.getAttribute?.('aria-autocomplete')
      || !!el.getAttribute?.('aria-controls')
      || cls.includes('select')
      || cls.includes('autocomplete')
      || !!el.getAttribute?.('list');
  }}

  // Autocomplete/react-select dropdown suggestions (role="listbox" popups,
  // react-select-style "__option"/"__menu" classes) are handled exclusively
  // by selectAutocompleteOption(). The generic choice-pill scanner below is
  // for persistent radio/checkbox-style pill UIs and must never also touch
  // these transient suggestion items — its own group-text lookup falls back
  // to matching keywords anywhere in the page body, so without this guard it
  // will "steal" and click a live autocomplete option whenever the page
  // happens to mention a matching keyword (e.g. "race") anywhere else, racing
  // against and overriding whatever selectAutocompleteOption already picked.
  function isInsideAutocompletePopup(el) {{
    return !!el.closest?.(
      '[role="listbox"], [role="option"], .select__menu, .select__option, ' +
      '[class*="menu-list"], [class*="MenuList"], [class*="__menu"], [class*="__option"], ' +
      '.autocomplete-option, .pac-item, .location-results, ' +
      '[data-automation-id*="promptOption"], [data-automation-id*="menuItem"], [data-automation-id*="selectOption"], ' +
      '[data-automation-id="activeListContainer"]'
    );
  }}

  function lock(el, value, field) {{
    const sig = signature(el);
    window.__STATIC_AUTOFILL_LOCKS[sig] = {{ value, field }};
    el.dataset.staticAutofilled = field;
    // Locked plain text fields are correct; dropping them from the agent's
    // own interactive-element index stops it wasting steps re-typing an
    // already-right value. readOnly alone does not do that -- a live run
    // left "First Name" readOnly but still offered to the agent at a
    // stable index, which retried it to failure seven times before the
    // loop boundary gave up on the whole job. pointer-events:none +
    // aria-disabled + tabindex=-1 is the pattern used elsewhere in this
    // file for exactly this reason; never `disabled`, which drops the
    // value from the real FormData submission.
    // Combobox-style widgets are excluded: some (e.g. Workday's "How did you
    // hear about us") need a second setNativeValue()+search pass on the same
    // input to resolve a nested suggestion list, which this lock would block.
    if (!looksLikeComboboxWidget(el)) {{
      try {{
        el.style.pointerEvents = 'none';
        el.setAttribute('aria-disabled', 'true');
        el.setAttribute('tabindex', '-1');
      }} catch (_) {{}}
    }}
    result.locked += 1;
  }}

  function fillText(el, value, field) {{
    if (!value || !visible(el)) return false;
    const current = clean(el.value);
    if (current === value) {{
      let resynced = false;
      if (el.dataset.staticReactSynced !== 'true') {{
        setNativeValue(el, value);
        result.restored += 1;
        resynced = true;
      }}
      lock(el, value, field);
      return resynced;
    }}
    const sig = signature(el);
    const wasLocked = window.__STATIC_AUTOFILL_LOCKS[sig];
    if (!current || el.dataset.staticAutofilled || wasLocked || current !== value) {{
      setNativeValue(el, value);
      lock(el, value, field);
      // Count what stuck, not what was attempted. React-controlled inputs
      // discard a programmatic value and re-render their own, so writing is not
      // evidence of filling. Comparing loosely rather than for equality keeps
      // inputs that reformat what they accept (phone masks) counted.
      const settled = clean(el.value);
      if (!settled) return false;
      if (wasLocked && current) result.restored += 1;
      else result.filled += 1;
      return true;
    }}
    return false;
  }}

  function isEffectivelyEmpty(el) {{
    const type = norm(el.type || el.tagName);
    if (type === 'checkbox' || type === 'radio') return !el.checked;
    if (el.tagName === 'SELECT') {{
      const option = el.selectedOptions?.[0];
      const text = norm(option?.textContent || '');
      const value = norm(el.value || '');
      return !value || !text || has(text, ['select', 'choose', 'please select']);
    }}
    const selectedSummary = norm(labelText(el));
    if (/\\b[1-9]\\d*\\s+items?\\s+selected\\b/.test(selectedSummary)) return false;
    return !clean(el.value);
  }}

  function isRequiredControl(el, text = labelText(el)) {{
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (el.getAttribute('data-required') === 'true') return true;
    if (el.closest?.('[aria-required="true"], [data-required="true"], .required, [class*="required"], [class*="Required"]')) return true;
    return has(text, ['required', 'please provide', 'must enter', 'must select']);
  }}

  // Static autofill re-runs after every agent step, so a control that is still
  // unresolved on this pass has just survived one more attempt. Three attempts
  // is the cap: past that the control is abandoned and dropped from
  // browser_use's interactive-element index, which is what actually stops the
  // agent looping on one field. Prompt instructions alone were not enough —
  // the cleanup model keeps returning to a field it cannot answer.
  const ABANDON_AFTER_ATTEMPTS = 3;

  function isAbandoned(el) {{
    return el.dataset.staticAbandoned === 'true';
  }}

  function abandonField(el) {{
    el.dataset.staticAbandoned = 'true';
    result.abandoned += 1;
    pushLimited(result.abandonedLabels, fieldSummary(el));
    recordOpenQuestion(el);
    // Deliberately not setting `disabled`: browsers omit disabled controls from
    // FormData, which would strip a partially typed answer from the real
    // submission. This combination blocks interaction without that side effect.
    try {{
      el.style.pointerEvents = 'none';
      el.setAttribute('aria-disabled', 'true');
      el.setAttribute('tabindex', '-1');
    }} catch (_) {{}}
  }}

  // A control can reach markNeedsLlm twice in one pass (once as required-empty,
  // once as unknown-text). Count one attempt per control per pass.
  const countedThisPass = new Set();

  function markNeedsLlm(el, reason) {{
    if (isAbandoned(el)) {{
      if (!countedThisPass.has(el)) {{
        countedThisPass.add(el);
        result.abandoned += 1;
        pushLimited(result.abandonedLabels, fieldSummary(el));
      }}
      recordOpenQuestion(el);
      return;
    }}
    // A deferred field is not interactive yet, so this pass was never an
    // attempt on it. Counting it would abandon a question before the agent
    // could reach it.
    if (!countedThisPass.has(el) && el.dataset.staticDeferred !== 'true') {{
      countedThisPass.add(el);
      const attempts = Number(el.dataset.staticUnresolvedPasses || 0) + 1;
      el.dataset.staticUnresolvedPasses = String(attempts);
      if (attempts >= ABANDON_AFTER_ATTEMPTS) {{
        abandonField(el);
        return;
      }}
    }}
    el.dataset.hybridNeedsLlm = 'true';
    if (reason) el.dataset.hybridNeedsLlmReason = reason;
    pushLimited(result.needsLlm, `${{reason || 'empty'}}: ${{fieldSummary(el)}}`);
    recordOpenQuestion(el);
  }}

  function controlKind(el) {{
    if (el.tagName === 'SELECT') return 'select';
    if (norm(el.getAttribute('role')) === 'combobox' || norm(el.className || '').includes('select')) return 'select';
    if (el.tagName === 'TEXTAREA') return 'textarea';
    return norm(el.type) || 'text';
  }}

  // Answers the candidate has given once before, keyed by question text. Used
  // only where no fact rule matched, so a known fact always wins.
  const QA_BANK = (() => {{
    const raw = facts.__qa__ || {{}};
    const byNormalized = {{}};
    for (const [question, answer] of Object.entries(raw)) {{
      if (!answer) continue;
      byNormalized[normalizeQuestion(question)] = String(answer);
    }}
    return byNormalized;
  }})();

  function normalizeQuestion(text) {{
    return String(text || '').toLowerCase().replace(/[^a-z0-9\\s]/g, '').replace(/\\s+/g, ' ').trim();
  }}

  // Question wording varies far more than it means. Comparing raw tokens
  // scored "What's ... you've faced" against "What is ... you have faced" at
  // 0.55, well under any safe threshold, so the bank never paid off. Comparing
  // only the words that carry the meaning fixes that.
  const QUESTION_FILLER = new Set([
    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'do', 'does', 'did',
    'have', 'has', 'had', 'you', 'your', 'yours', 'youve', 'youre', 'we', 'our', 'us',
    'i', 'my', 'me', 'what', 'whats', 'which', 'who', 'why', 'how', 'when', 'where',
    'to', 'of', 'in', 'on', 'at', 'for', 'with', 'and', 'or', 'if', 'that', 'this',
    'it', 'its', 'as', 'any', 'please', 'tell', 'describe', 'share', 'about', 'would',
    'will', 'can', 'could', 'should', 'ever', 'most', 'much', 'many', 'some',
  ]);

  function contentTokens(question) {{
    return new Set(question.split(' ').filter((token) => token && !QUESTION_FILLER.has(token)));
  }}

  function bankedAnswer(el) {{
    const question = normalizeQuestion(questionText(el));
    if (!question) return '';
    if (QA_BANK[question]) return QA_BANK[question];
    const asked = contentTokens(question);
    // Below three content words the remaining tokens are too thin to tell two
    // questions apart ("able relocate" vs "able work weekends"), and answering
    // the wrong question is worse than leaving it blank.
    if (asked.size < 3) return '';
    for (const [known, answer] of Object.entries(QA_BANK)) {{
      const other = contentTokens(known);
      if (other.size < 3) continue;
      let shared = 0;
      for (const token of asked) if (other.has(token)) shared += 1;
      if (shared / Math.max(asked.size, other.size) > 0.85) return answer;
    }}
    return '';
  }}

  function recordOpenQuestion(el) {{
    const question = questionText(el);
    if (!question) return;
    if (result.openQuestions.some((item) => item.question === question)) return;
    if (result.openQuestions.length >= 100) return;
    result.openQuestions.push({{
      question,
      type: controlKind(el),
      required: isRequiredControl(el, labelText(el)),
    }});
  }}

  function markRequiredEmpty(el, reason = 'required-empty') {{
    el.dataset.hybridRequiredEmpty = 'true';
    markNeedsLlm(el, reason);
    pushLimited(result.requiredEmptyLabels, fieldSummary(el));
  }}

  function clickLikeHuman(el) {{
    if (!el || !visible(el)) return false;
    el.scrollIntoView({{ block: 'center', inline: 'center' }});
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {{
      try {{
        el.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, view: window }}));
      }} catch (_) {{}}
    }}
    try {{
      el.click();
    }} catch (_) {{
      try {{
        el.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true, view: window }}));
      }} catch (_) {{}}
    }}
    return true;
  }}

  function dispatchKey(el, key) {{
    for (const type of ['keydown', 'keyup']) {{
      try {{
        el.dispatchEvent(new KeyboardEvent(type, {{ key, code: key, bubbles: true, cancelable: true }}));
      }} catch (_) {{}}
    }}
  }}

  function shouldResolveAutocomplete(el, field, text, value) {{
    if (!field || !value || !visible(el)) return false;
    if (el.dataset.staticAutocompleteSelected === field) return false;
    const attempts = Number(el.dataset.staticAutocompleteAttempts || 0);
    if (attempts >= 2) return false;
    const role = norm(el.getAttribute('role'));
    const ariaAuto = norm(el.getAttribute('aria-autocomplete'));
    const ariaControls = norm(el.getAttribute('aria-controls'));
    const classText = norm(el.className || '');
    const fieldLooksDropdown = role.includes('combobox')
      || ariaAuto
      || ariaControls
      || classText.includes('select')
      || classText.includes('autocomplete')
      || el.getAttribute('list');
    const dropdownFacts = ['city', 'state', 'country', 'current_location', 'preferred_us_locations', 'school', 'degree', 'major', 'heard_about', 'authorized_to_work_us', 'visa_sponsorship_needed', 'gender', 'race_ethnicity', 'education_end_month', 'education_start_month', 'graduation_year', 'veteran_status', 'disability_status', 'hispanic_latino'];
    if (field === 'preferred_us_locations' && !/^wherever\\b/i.test(value)) return true;
    if ((field === 'current_location' || field === 'preferred_us_locations') && hasAny(text, ['location'])) return true;
    return fieldLooksDropdown && dropdownFacts.includes(field)
      || (field === 'city' && hasAny(text, ['city', 'town', 'location']))
      || (field === 'school' && hasAny(text, ['school', 'university', 'college']));
  }}

  function stateAbbrev(value) {{
    const states = {{
      'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
      'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA',
      'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
      'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
      'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
      'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH',
      'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC',
      'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
      'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
      'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
      'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
      'wisconsin': 'WI', 'wyoming': 'WY'
    }};
    return states[norm(value)] || '';
  }}

  function monthAliases(value) {{
    const index = Number(value);
    if (!index || index < 1 || index > 12) return [];
    const names = [
      '', 'January', 'February', 'March', 'April', 'May', 'June',
      'July', 'August', 'September', 'October', 'November', 'December'
    ];
    return [String(index), String(index).padStart(2, '0'), names[index], names[index].slice(0, 3)];
  }}

  function endMonthName(value) {{
    const raw = clean(value || '');
    if (!raw) return '';
    const monthNames = [
      'january', 'february', 'march', 'april', 'may', 'june',
      'july', 'august', 'september', 'october', 'november', 'december'
    ];
    const lower = norm(raw);
    const named = monthNames.find((name) => lower.includes(name) || lower.includes(name.slice(0, 3)));
    if (named) return named.charAt(0).toUpperCase() + named.slice(1);
    const numeric = raw.match(/\\b(0?[1-9]|1[0-2])[\\/\\-]/) || raw.match(/^(0?[1-9]|1[0-2])$/);
    if (numeric) {{
      const idx = Number(numeric[1]);
      if (idx >= 1 && idx <= 12) return monthNames[idx - 1].charAt(0).toUpperCase() + monthNames[idx - 1].slice(1);
    }}
    const isoNumeric = raw.match(/\\d{{4}}-(0[1-9]|1[0-2])\\b/);
    if (isoNumeric) {{
      const idx = Number(isoNumeric[1]);
      return monthNames[idx - 1].charAt(0).toUpperCase() + monthNames[idx - 1].slice(1);
    }}
    if (lower.includes('spring')) return 'May';
    if (lower.includes('summer')) return 'August';
    if (lower.includes('fall') || lower.includes('autumn')) return 'December';
    if (lower.includes('winter')) return 'December';
    return '';
  }}

  function autocompleteTerms(field, value) {{
    const terms = [clean(value)].filter(Boolean);
    if (field === 'city' && facts.city) {{
      terms.push(facts.city);
      if (facts.state) terms.push(`${{facts.city}}, ${{facts.state}}`);
      const abbr = stateAbbrev(facts.state);
      if (abbr) terms.push(`${{facts.city}}, ${{abbr}}`);
    }}
    if ((field === 'current_location' || field === 'preferred_us_locations') && (facts.current_location || facts.city)) {{
      if (facts.current_location) terms.push(facts.current_location);
      if (facts.city) terms.push(facts.city);
      if (facts.city && facts.state) terms.push(`${{facts.city}}, ${{facts.state}}`);
      const abbr = stateAbbrev(facts.state);
      if (facts.city && abbr) terms.push(`${{facts.city}}, ${{abbr}}`);
    }}
    if (field === 'state' && facts.state) {{
      terms.push(facts.state);
      const abbr = stateAbbrev(facts.state);
      if (abbr) terms.push(abbr);
    }}
    if (field === 'country') terms.push('United States', 'USA', 'US');
    if (field === 'heard_about') terms.push(facts.heard_about || 'LinkedIn', 'LinkedIn');
    if (field === 'gender') {{
      const g = norm(value);
      if (has(g, ['male']) && !has(g, ['female'])) terms.push('male', 'man', 'm');
      else if (has(g, ['female'])) terms.push('female', 'woman', 'w', 'f');
      else if (has(g, ['non-binary', 'nonbinary', 'non binary'])) terms.push('non-binary', 'nonbinary', 'genderqueer', 'genderfluid', 'agender', 'gender non-conforming', 'gender nonconforming');
      else if (has(g, ['transgender', 'trans '])) terms.push('transgender', 'trans');
      else if (has(g, ['two-spirit', 'two spirit'])) terms.push('two-spirit', 'two spirit');
      else if (has(g, ['prefer not', 'decline', 'not to say'])) terms.push('prefer not to say', 'decline to self identify', 'decline to answer', 'i prefer not to answer', 'prefer not to disclose');
    }}
    if (field === 'race_ethnicity') {{
      const r = norm(value);
      if (has(r, ['asian indian', 'indian'])) terms.push('asian', 'asian indian', 'south asian', 'asian (not hispanic or latino)', 'east asian');
      else if (has(r, ['asian'])) terms.push('asian', 'asian (not hispanic or latino)', 'east asian', 'south asian');
      if (has(r, ['white', 'caucasian'])) terms.push('white', 'white (not hispanic or latino)', 'caucasian');
      if (has(r, ['black', 'african american'])) terms.push('black', 'black or african american', 'african american', 'black (not hispanic or latino)');
      if (has(r, ['hispanic', 'latino', 'latina', 'latinx'])) terms.push('hispanic', 'hispanic or latino', 'latino', 'latina', 'latinx', 'hispanic/latino');
      if (has(r, ['native american', 'american indian', 'alaska native'])) terms.push('american indian or alaska native', 'native american', 'indigenous');
      if (has(r, ['pacific islander', 'native hawaiian'])) terms.push('native hawaiian or other pacific islander', 'pacific islander');
      if (has(r, ['middle eastern', 'north african'])) terms.push('middle eastern or north african', 'middle eastern', 'mena');
      if (has(r, ['two or more', 'mixed', 'multiracial', 'biracial'])) terms.push('two or more races', 'mixed race', 'multiracial', 'biracial', 'two or more races (not hispanic or latino)');
      if (has(r, ['prefer not', 'decline', 'not to say'])) terms.push('prefer not to say', 'decline to self identify', 'decline to answer', 'i do not wish to answer');
    }}
    if (field === 'education_end_month' || field === 'education_start_month') {{
      const m = norm(value);
      if (m) {{
        terms.push(m, m.slice(0, 3));
        const monthNames = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'];
        const idx = monthNames.indexOf(m);
        if (idx >= 0) terms.push(String(idx + 1), String(idx + 1).padStart(2, '0'));
      }}
    }}
    return [...new Set(terms.map(norm).filter(Boolean))];
  }}

  function optionText(el) {{
    return clean([
      el.innerText,
      el.textContent,
      el.getAttribute?.('aria-label'),
      el.getAttribute?.('title'),
      el.getAttribute?.('data-automation-label'),
      el.getAttribute?.('data-value'),
      el.getAttribute?.('value'),
    ].filter(Boolean).join(' '));
  }}

  async function selectAutocompleteOption(el, value, field) {{
    if (!shouldResolveAutocomplete(el, field, labelText(el), value)) return false;
    el.dataset.staticAutocompleteAttempts = String(Number(el.dataset.staticAutocompleteAttempts || 0) + 1);
    try {{ el.focus(); }} catch (_) {{}}
    setNativeValue(el, value);
    await sleep(field === 'current_location' || field === 'preferred_us_locations' ? 650 : 250);

    const terms = autocompleteTerms(field, value);
    const selector = [
      '[role="option"]',
      '[role="listbox"] [role="option"]',
      '[role="listbox"] li',
      '[role="listbox"] div',
      '[data-automation-id*="promptOption"]',
      '[data-automation-id*="menuItem"]',
      '[data-automation-id*="selectOption"]',
      '.select__option',
      '.autocomplete-option',
      '.pac-item',
      '.location-result',
      '.location-results li',
      '.location-results div',
      '[class*="result"]',
      '[class*="Result"]',
      '[class*="suggest"]',
      '[class*="Suggest"]',
      '[class*="autocomplete"] li',
      '[class*="Autocomplete"] li',
      '[class*="option"]',
      '[class*="Option"]'
    ].join(',');
    const collectOptions = () => allElements(selector)
      .filter(visible)
      .map((candidate) => {{
        const clickable = candidate.closest?.('button,[role="option"],li,[data-automation-id],[class*="option"],[class*="Option"]') || candidate;
        return {{ el: clickable, text: norm(optionText(candidate)) }};
      }})
      .filter((item) => item.text && item.text.length < 220);

    let options = collectOptions();
    let exact = options.find((item) => terms.some((term) => item.text === term));
    let loose = exact || options.find((item) => terms.some((term) => item.text.includes(term) || term.includes(item.text)));
    if (!loose && field === 'heard_about') {{
      const workdaySourceGroup = options.find((item) => has(item.text, ['external career site sources', 'external sources']));
      if (workdaySourceGroup && clickLikeHuman(workdaySourceGroup.el)) {{
        await sleep(250);
        try {{ el.focus(); }} catch (_) {{}}
        setNativeValue(el, value);
        await sleep(350);
        options = collectOptions();
        exact = options.find((item) => terms.some((term) => item.text === term));
        loose = exact || options.find((item) => terms.some((term) => item.text.includes(term) || term.includes(item.text)));
      }}
    }}
    // heard_about is the sole exception: Workday's "How did you hear about
    // us" needs a second setNativeValue()+search pass on this same input, so
    // it must stay interactive after the first match. Every other resolved
    // combobox gets hard-disabled — a weak vision model reading a page full
    // of legalese (e.g. the veteran-status radio block) will not reliably
    // notice a data-staticAutocompleteSelected hint and will keep re-clicking
    // an already-correct answer; pointer-events:none plus the aria/tabindex
    // changes below drop it out of browser_use's own interactive-element
    // index, so there is nothing left for the agent to click at all.
    const finalizeSelection = () => {{
      el.dataset.staticAutocompleteSelected = field;
      result.selects += 1;
      if (field !== 'heard_about') {{
        // Not setting `disabled` here: a disabled form control is dropped
        // from FormData entirely, which would silently strip this answer
        // from the real submission. pointer-events:none + aria-disabled +
        // tabindex block further interaction without touching that.
        try {{
          el.style.pointerEvents = 'none';
          el.setAttribute('aria-disabled', 'true');
          el.setAttribute('tabindex', '-1');
        }} catch (_) {{}}
      }}
      return true;
    }};
    if (loose && clickLikeHuman(loose.el)) return finalizeSelection();

    // Taking whatever the widget happens to highlight is only safe where the
    // suggestion list is a search result for text we typed and the exact
    // spelling is unknowable (a city, a school). Everywhere else the highlight
    // is just the first item in a fixed list, and committing it invents an
    // answer: this is how a Virginia Tech candidate ended up with "Aalborg
    // University". For other fields, leaving the field for the human is right.
    const FIRST_SUGGESTION_FIELDS = ['current_location', 'preferred_us_locations', 'school'];
    if (!FIRST_SUGGESTION_FIELDS.includes(field)) {{
      dispatchKey(el, 'Enter');
      return false;
    }}

    dispatchKey(el, 'ArrowDown');
    await sleep(100);
    const activeId = el.getAttribute('aria-activedescendant');
    if (activeId) {{
      const root = el.getRootNode?.() || document;
      const active = root.getElementById?.(activeId) || document.getElementById(activeId);
      if (active && clickLikeHuman(active)) return finalizeSelection();
    }}
    // Exact/loose text matching is brittle for city and school names (format
    // and spelling variants abound). For these fields, a visible suggestion
    // list means the typed text was recognized — the first option is a
    // reasonable pick rather than leaving the field blank for the LLM.
    if (['current_location', 'preferred_us_locations', 'school'].includes(field) && options.length) {{
      const first = options[0];
      if (clickLikeHuman(first.el)) return finalizeSelection();
    }}
    dispatchKey(el, 'Enter');
    return false;
  }}

  function pickTextFact(el, text) {{
    text = preferPrimaryFieldText(el, text);
    const type = norm(el.type);
    if (el.tagName === 'TEXTAREA') {{
      // Never put short identity/contact facts into a long-answer box. A live
      // Ashby form placed the phone number into "How did you hear about us?"
      // because the broad label matcher also picked up the preceding Phone
      // question. Only two deterministic long-answer rules are safe here;
      // everything else is answered from Q&A or handed to the AI/user.
      const question = norm(questionText(el)) || text;
      if (has(question, ['how did you hear', 'hear about us', 'application source', 'candidate source'])) {{
        return ['heard_about', facts.heard_about || 'LinkedIn'];
      }}
      if (has(question, ['why do you want to work', 'why are you interested', 'why this company', 'why join', 'interest in this role', 'interested in this role'])) {{
        return ['interest_statement', facts.interest_statement];
      }}
      return [null, null];
    }}
    const page = norm(location.href + ' ' + document.title);
    const preferExistingAccount = ['yes', 'true', '1'].includes(norm(facts.prefer_existing_account));
    const createAccountSurface = preferExistingAccount
      && !!document.querySelector('[data-automation-id="signInLink"]')
      && has(norm(document.body?.innerText || ''), ['create account', 'already have an account']);
    const addressish = hasAny(text, ['address', 'mailing', 'home', 'residence', 'location', 'city', 'zip', 'postal', 'country']);
    const statusish = hasAny(text, ['opt', 'cpt', 'f 1', 'f1', 'visa', 'work authorization', 'employment authorization', 'work status', 'student status', 'sponsorship']);
    if (type === 'password' || has(text, ['password', 'passwd', 'pwd'])) {{
      if (createAccountSurface) return [null, null];
      return ['account_password', has(text, ['confirm', 'retype', 'repeat', 'verify']) ? facts.confirm_password || facts.account_password : facts.account_password];
    }}
    if (type === 'date') {{
      if (has(text, ['birth', 'dob', 'date of birth'])) return ['date_of_birth_iso', facts.date_of_birth_iso || facts.date_of_birth];
      if (has(text, ['visa expiration', 'visa expiry', 'status expiration', 'status expiry', 'i 20 expiration', 'i-20 expiration'])) return ['visa_expiration_date_iso', facts.visa_expiration_date_iso || facts.visa_expiration_date];
      if (has(text, ['graduation', 'graduate', 'expected grad', 'completion'])) return ['graduation_date_iso', facts.graduation_date_iso || facts.graduation_date];
      if (has(text, ['internship end', 'available end', 'availability end', 'latest end', 'end of availability'])) return ['internship_end_date_iso', facts.internship_end_date_iso || facts.internship_end_date || facts.latest_end_date];
      if (has(text, ['start', 'available from', 'availability start', 'begin', 'pick date'])) return ['earliest_start_date_iso', facts.earliest_start_date_iso || facts.earliest_start_date_date];
      return [null, null];
    }}
    const pageTextForFacts = norm(document.body?.innerText || '');
    if (has(pageTextForFacts, ['when are you available to start'])
      && isRequiredControl(el, text)
      && has(text, ['customquestions', 'custom questions', 'field 84', 'field-84'])) {{
      return ['earliest_start_date_date', facts.earliest_start_date_date || facts.earliest_start_date_iso || facts.earliest_start_date];
    }}
    if (has(text, ['birth', 'dob', 'date of birth'])) {{
      if (has(text, ['month', 'mm'])) return ['date_of_birth_month', facts.date_of_birth_month];
      if (has(text, ['day', 'dd'])) return ['date_of_birth_day', facts.date_of_birth_day];
      if (has(text, ['year', 'yyyy'])) return ['date_of_birth_year', facts.date_of_birth_year];
      return ['date_of_birth', facts.date_of_birth || facts.date_of_birth_iso];
    }}
    const idAndName = norm(`${{el.id || ''}} ${{el.name || ''}}`);
    if (has(idAndName, ['firstyearattended', 'first year attended'])) {{
      const startYear = clean(facts.education_start_date || facts.school_start_date || '').match(/\\b(?:19|20)\\d{{2}}\\b/)?.[0];
      return ['education_start_date', startYear];
    }}
    if (has(idAndName, ['lastyearattended', 'last year attended'])) {{
      const endYear = facts.graduation_year || clean(facts.education_end_date || facts.graduation || '').match(/\\b(?:19|20)\\d{{2}}\\b/)?.[0];
      return ['graduation_year', endYear];
    }}
    // idAndName is built from norm(), which replaces every non-alphanumeric
    // character (hyphens, underscores) with a space — so "end-month--0"
    // becomes "end month 0" here, never the literal "end-month".
    if (has(idAndName, ['end month', 'endmonth']) && !has(idAndName, ['start'])) {{
      return ['education_end_month', endMonthName(facts.education_end_date || facts.graduation)];
    }}
    if (has(idAndName, ['start month', 'startmonth'])) {{
      return ['education_start_month', endMonthName(facts.education_start_date || facts.school_start_date)];
    }}
    if (has(idAndName, ['end year', 'endyear']) && !has(idAndName, ['start'])) {{
      const endYear = facts.graduation_year || clean(facts.education_end_date || facts.graduation || '').match(/\\b(?:19|20)\\d{{2}}\\b/)?.[0];
      return ['graduation_year', endYear];
    }}
    if (has(idAndName, ['start year', 'startyear'])) {{
      const startYear = clean(facts.education_start_date || facts.school_start_date || '').match(/\\b(?:19|20)\\d{{2}}\\b/)?.[0];
      return ['education_start_date', startYear];
    }}
    if (type === 'email' || has(text, ['email', 'e mail', 'username', 'user name', 'user id', 'userid', 'login id'])) {{
      const loginish = has(text + ' ' + page, ['login', 'sign in', 'signup', 'sign up', 'register', 'create account', 'password']);
      if (loginish && createAccountSurface) return [null, null];
      return [loginish ? 'account_email' : 'email', (loginish ? facts.account_email : facts.email) || facts.email || facts.account_email];
    }}
    if (has(text, ['phone extension', 'phone ext', 'extension'])) {{
      const currentDigits = clean(el.value || '').replace(/\\D/g, '');
      const knownDigits = clean(facts.phone_full || facts.phone || '').replace(/\\D/g, '');
      if (currentDigits && knownDigits && (currentDigits === knownDigits || currentDigits === knownDigits.replace(/^1/, ''))) {{
        delete window.__STATIC_AUTOFILL_LOCKS[signature(el)];
        setNativeValue(el, '');
        delete el.dataset.staticAutofilled;
      }}
      return [null, null];
    }}
    if (has(text, ['country phone code', 'phone country code', 'calling code'])) return [null, null];
    if (type === 'tel' || has(text, ['phone number', 'mobile number', 'cell number'])) {{
      let nationalPhone = clean(facts.phone || facts.phone_full || '').replace(/\\D/g, '');
      if (nationalPhone.length === 11 && nationalPhone.startsWith('1')) nationalPhone = nationalPhone.slice(1);
      return ['phone', nationalPhone];
    }}
    if (type === 'url' || has(text, ['linkedin', 'linked in'])) return ['linkedin_url', facts.linkedin_url];
    if (has(text, ['github'])) return ['github_url', facts.github_url];
    if (has(text, ['portfolio', 'website', 'personal site'])) return ['portfolio_url', facts.portfolio_url];
    if (has(text, ['pronoun'])) return ['pronouns', facts.pronouns];
    if (has(text, ['gender identity', 'gender']) && !has(text, ['engender'])) return ['gender', facts.gender];
    if (has(text, ['race', 'ethnicity', 'racial'])) return ['race_ethnicity', facts.race_ethnicity];
    // These three existed only on the <select> path, so the combobox form of
    // the same EEO questions was never matched at all.
    if (has(text, ['veteran'])) return ['veteran_status', facts.veteran_status];
    if (has(text, ['disability', 'disabled'])) return ['disability_status', facts.disability_status];
    if (has(text, ['sexual orientation', 'orientation'])) return ['sexual_orientation', facts.sexual_orientation];
    if (has(text, ['date of birth', 'birth date', 'dob'])) return ['date_of_birth', facts.date_of_birth];
    if (has(text, ['gpa', 'grade point'])) return ['gpa', facts.gpa];
    if (has(text, ['age']) && !has(text, ['page', 'stage', 'grade average'])) return ['age', facts.age];
    if (has(text, ['how did you hear', 'hear about us', 'application source', 'candidate source'])) return ['heard_about', facts.heard_about || 'LinkedIn'];
    if (has(text, ['visa expiration', 'visa expiry', 'status expiration', 'status expiry', 'i 20 expiration', 'i-20 expiration'])) return ['visa_expiration_date', facts.visa_expiration_date || facts.visa_expiration_date_iso];
    if (has(text, ['cpt'])) return ['cpt_status', facts.cpt_status || facts.cpt_eligible || 'Eligible for CPT'];
    if (has(text, ['opt'])) return ['opt_status', facts.opt_status || facts.opt_eligible || 'Eligible for OPT'];
    if (has(text, ['f 1', 'f1', 'f-1', 'visa type', 'visa status', 'immigration status', 'student status'])) return ['visa_status', facts.visa_status || facts.immigration_status || facts.current_work_status || 'F-1 student visa'];
    if (has(text, ['legally authorized', 'authorized to work', 'eligible to work', 'work lawfully']) && !has(text, ['sponsor'])) return ['authorized_to_work_us', factBool('authorized_to_work_us') ? 'Yes' : 'No'];
    if (has(text, ['require sponsorship', 'need sponsorship', 'needs sponsorship', 'visa sponsorship', 'sponsor you', 'sponsorship for employment', 'sponsorship to work'])) return ['visa_sponsorship_needed', (factBool('visa_sponsorship_needed') || factBool('future_sponsorship_needed') || factBool('h1b_sponsorship_needed')) ? 'Yes' : 'No'];
    if (has(text, ['work status', 'employment authorization'])) return ['current_work_status', facts.current_work_status || facts.work_authorization];
    if (has(text, ['work authorization', 'authorization status'])) return ['work_authorization', facts.work_authorization];
    // A date field must never fall through to the location/address rules below.
    // A MM/DD/YYYY signature date matched "location" through neighbouring label
    // text and was filled with "Blacksburg, Virginia".
    const dateish = has(text, ['mm dd yyyy', 'dd mm yyyy', 'yyyy mm dd', 'signature date', 'date signed'])
      || (has(text, ['date']) && !hasAny(text, ['candidate', 'update', 'validate', 'mandate']));
    if (dateish && has(text, ['signature', 'signed', 'today'])) return ['todays_date', facts.todays_date];
    if (dateish) return [null, null];
    // A school question can name a country without asking for one ("we recruit
    // from universities across the country"), so school wins over country.
    if (has(text, ['university', 'college', 'school']) && !has(text, ['school district', 'high school'])) return ['school', facts.school];
    if (has(text, ['preferred name', 'preferred first name', 'nickname', 'what would you like us to call you', 'go by'])) return ['first_name', facts.preferred_name || facts.first_name];
    if (has(text, ['legal address', 'mailing address', 'street address', 'residential address'])) return ['street_address', facts.street_address];
    if (has(text, ['first name', 'firstname', 'given name', 'givenname'])) return ['first_name', facts.first_name];
    if (has(text, ['last name', 'lastname', 'family name', 'familyname', 'surname'])) return ['last_name', facts.last_name];
    if (has(text, ['full name', 'legal full name']) && !has(text, ['company name', 'employer name', 'school name'])) return ['full_name', facts.full_name];
    if (has(text, ['current location', 'where are you located', 'location']) && !has(text, ['preferred location', 'job location', 'work location'])) return ['current_location', facts.current_location || clean((facts.city || '') + ', ' + (facts.state || '')).replace(/^,|,$/g, '')];
    if (has(text, ['street', 'address line 1', 'address 1', 'home address'])) return ['street_address', facts.street_address];
    if (has(text, ['city', 'town'])) return ['city', facts.city];
    if (!statusish && (addressish || has(text, ['state province', 'state/province', 'province region', 'address state', 'location state'])) && has(text, ['state', 'province', 'region'])) return ['state', facts.state];
    if (has(text, ['zip', 'postal'])) return ['postal_code', facts.postal_code];
    if (has(text, ['country of citizenship', 'citizenship country', 'citizenship'])) return ['citizenship', facts.citizenship || facts.nationality];
    if (has(text, ['nationality', 'country of nationality'])) return ['nationality', facts.nationality || facts.citizenship];
    if (has(text, ['country of residence', 'legal residence country', 'residency country'])) return ['country_of_residence', facts.country_of_residence];
    if (has(text, ['country'])) return ['country', facts.country];
    if (has(text, ['school start', 'started school', 'education start', 'education begin', 'start date']) && has(text, ['school', 'university', 'college', 'education'])) return ['education_start_date', facts.education_start_date || facts.school_start_date];
    if (has(text, ['school end', 'education end', 'end date', 'completion date']) && has(text, ['school', 'university', 'college', 'education'])) return ['education_end_date', facts.education_end_date || facts.graduation];
    if (has(text, ['university', 'college', 'school'])) return ['school', facts.school];
    if (has(text, ['degree', 'qualification'])) return ['degree', facts.degree];
    if (has(text, ['major', 'field of study', 'field study', 'discipline'])) return ['major', facts.major];
    if (has(text, ['graduation year', 'grad year', 'expected graduation year'])) return ['graduation_year', facts.graduation_year];
    if (has(text, ['graduation term', 'expected grad term'])) return ['graduation_term', facts.graduation_term || facts.graduation];
    if (has(text, ['graduation date', 'grad date', 'expected graduation date', 'completion date'])) return ['graduation_date', facts.graduation_date || facts.graduation];
    if (has(text, ['graduation', 'graduate', 'expected grad', 'completion year', 'end date'])) return ['graduation', facts.graduation_term || facts.graduation];
    // A screening question can mention an employer without asking for one
    // ("...job duties for a company by any restrictive covenants..."). Treating
    // it as the current-employer field both answers it wrongly and consumes the
    // control, so it never reaches the LLM either.
    const screeningQuestion = hasAny(text, [
      'are you', 'do you', 'have you', 'will you', 'did you',
      'restrictive covenant', 'non compete', 'noncompete', 'non solicitation',
      'prohibited', 'confidentiality agreement',
    ]);
    if (!screeningQuestion && has(text, ['current employer', 'recent employer', 'most recent employer', 'employer', 'current company', 'company'])) return ['current_employer', facts.current_employer];
    if (has(text, ['current role', 'current title', 'job title'])) return ['current_role', facts.current_role];
    if (has(text, ['years of experience', 'experience years'])) return ['years_of_experience', facts.years_of_experience];
    if (has(text, ['prior internship', 'previous internship', 'number of internships', 'how many internships'])) return ['prior_internships', facts.prior_internships || '0'];
    if (has(text, ['work status', 'visa status', 'employment status', 'current status'])) return ['current_work_status', facts.current_work_status || facts.work_authorization];
    if (has(text, ['why do you want to work', 'why are you interested', 'why this company', 'why join', 'interest in this role', 'interested in this role'])) return ['interest_statement', facts.interest_statement];
    if (has(text, ['pick date']) && isRequiredControl(el, text)) return ['earliest_start_date_date', facts.earliest_start_date_date || facts.earliest_start_date_iso];
    if (has(text, ['internship end', 'available through', 'available until', 'available end', 'latest end', 'end of availability'])) return ['internship_end_date', facts.internship_end_date || facts.latest_end_date];
    if (has(text, ['availability', 'available to start', 'start date', 'internship dates'])) return ['internship_availability', facts.internship_availability || facts.earliest_start_date];
    if (has(text, ['hours per week', 'weekly hours', 'maximum hours'])) return ['max_hours_per_week', facts.max_hours_per_week];
    if (has(text, ['desired pay', 'desired salary', 'salary expectation', 'compensation', 'hourly rate', 'pay rate'])) return ['desired_pay', facts.desired_pay];
    if (has(text, ['preferred location', 'location preference'])) return ['preferred_us_locations', facts.preferred_us_locations];
    return pickFuzzyTextFact(el);
  }}

  // Common nickname -> official-name mappings for schools with well-known
  // short names. Checked with priority for the 'school' field before any
  // generic substring/fuzzy matching, because a naive fuzzy match on a
  // common word (e.g. "Virginia") in a 3000+ option world-university list
  // can land on a *different, unrelated* school with the same word in its
  // name (a real, live failure: "Virginia Tech" fuzzy-matched to "Virginia
  // Commonwealth University" instead of "Virginia Polytechnic Institute and
  // State University") — actively wrong data is worse than leaving it blank
  // for manual review, so this map trades broad coverage for precision.
  const SCHOOL_ALIASES = {{
    'virginia tech': 'virginia polytechnic institute and state university',
    'georgia tech': 'georgia institute of technology',
    'cal poly': 'california polytechnic state university',
    'cal poly slo': 'california polytechnic state university',
    'mit': 'massachusetts institute of technology',
    'caltech': 'california institute of technology',
    'ut austin': 'university of texas at austin',
    'ohio state': 'the ohio state university',
    'penn state': 'pennsylvania state university',
    'psu': 'pennsylvania state university',
    'umass': 'university of massachusetts amherst',
    'umass amherst': 'university of massachusetts amherst',
    'unc': 'university of north carolina at chapel hill',
    'unc chapel hill': 'university of north carolina at chapel hill',
    'nc state': 'north carolina state university',
    'texas a&m': 'texas a&m university',
    'texas am': 'texas a&m university',
    'ucla': 'university of california los angeles',
    'ucla los angeles': 'university of california los angeles',
    'uc berkeley': 'university of california berkeley',
    'berkeley': 'university of california berkeley',
    'uva': 'university of virginia',
    'gt': 'georgia institute of technology',
    'rpi': 'rensselaer polytechnic institute',
    'rit': 'rochester institute of technology',
    'nyu': 'new york university',
    'usc': 'university of southern california',
    'asu': 'arizona state university',
    'osu': 'the ohio state university',
    'lsu': 'louisiana state university',
    'ole miss': 'university of mississippi',
    'vcu': 'virginia commonwealth university',
    'jmu': 'james madison university',
    'odu': 'old dominion university',
    'gmu': 'george mason university',
  }};

  function optionValueFor(select, field, value) {{
    if (!value) return null;
    const targets = [norm(value)];
    if (field === 'school') {{
      const alias = SCHOOL_ALIASES[norm(value)];
      if (alias) targets.unshift(alias);
    }}
    if (field === 'state') {{
      const abbr = stateAbbrev(value);
      if (abbr) targets.push(norm(abbr));
    }}
    if (field === 'country') targets.push('united states', 'usa', 'us', 'united states of america');
    if (field === 'citizenship' || field === 'nationality') targets.push('india', 'indian');
    if (field === 'country_of_residence') targets.push('united arab emirates', 'uae', 'u a e');
    if (field === 'degree') targets.push('bachelor', 'bachelors', 'bs', 'b s', 'undergraduate');
    if (field === 'pronouns') targets.push('he him', 'he him his');
    if (field === 'veteran_status' && has(norm(value), ['not a veteran', 'not protected', 'no'])) targets.push('i am not a protected veteran', 'not a protected veteran', 'not a veteran');
    if (field === 'disability_status' && has(norm(value), ['no', 'do not have'])) targets.push('no i do not have a disability', 'no i don t have a disability', 'no disability');
    if (['visa_status', 'immigration_status', 'f1_status', 'f1_visa_status'].includes(field)) targets.push('f-1', 'f1', 'f 1', 'student visa', 'f-1 student', 'f1 student', 'student');
    if (['cpt_status', 'cpt_eligible'].includes(field)) targets.push('cpt', 'curricular practical training', 'eligible for cpt', 'yes');
    if (['opt_status', 'opt_eligible'].includes(field)) targets.push('opt', 'optional practical training', 'eligible for opt', 'yes');
    if (field === 'school_start_date' || field === 'education_start_date') targets.push('fall 2024', 'august 2024', '08/2024', '2024');
    if (field === 'education_end_date' || field === 'graduation') {{
      const year = norm(value).match(/\\b(?:19|20)\\d{{2}}\\b/)?.[0];
      if (year) targets.push(year);
      if (year && has(norm(value), ['fall', 'winter', 'december', '12'])) targets.push(`fall ${{year}}`, `december ${{year}}`, `12/${{year}}`);
      if (year && has(norm(value), ['spring', 'may', '05'])) targets.push(`spring ${{year}}`, `may ${{year}}`, `05/${{year}}`);
      if (year && has(norm(value), ['summer', 'august', '08'])) targets.push(`summer ${{year}}`, `august ${{year}}`, `08/${{year}}`);
    }}
    if (field === 'date_of_birth_month') targets.push(...monthAliases(value).map(norm));
    const options = Array.from(select.options || []).filter((o) => {{
      const text = norm(o.textContent);
      const optionValue = norm(o.value);
      if (!text && !optionValue) return false;
      if (has(text, ['select', 'choose', 'please select'])) return false;
      return true;
    }});
    let exact = options.find((o) => targets.some((target) => norm(o.textContent) === target || norm(o.value) === target));
    if (exact) return exact.value;
    let loose = options.find((o) => {{
      const text = norm(o.textContent);
      const optionValue = norm(o.value);
      return targets.some((target) => text && (text.includes(target) || target.includes(text) || optionValue.includes(target) || target.includes(optionValue)));
    }});
    return loose ? loose.value : null;
  }}

  function yesNoValue(select, answerYes) {{
    const options = Array.from(select.options || []).filter((o) => norm(o.textContent) || norm(o.value));
    const yesWords = ['yes', 'y', 'true'];
    const noWords = ['no', 'n', 'false'];
    const wanted = answerYes ? yesWords : noWords;
    const found = options.find((o) => wanted.includes(norm(o.textContent)) || wanted.includes(norm(o.value)));
    return found ? found.value : null;
  }}

  function setSelect(select, value, field) {{
    if (!value || !visible(select)) return false;
    if (select.value === value) return false;
    select.value = value;
    select.dataset.staticAutofilled = field;
    select.dispatchEvent(new Event('input', {{ bubbles: true }}));
    select.dispatchEvent(new Event('change', {{ bubbles: true }}));
    result.selects += 1;
    return true;
  }}

  const FIELD_FILL_ORDER = [
    'account_email',
    'email',
    'account_password',
    'confirm_password',
    'first_name',
    'last_name',
    'full_name',
    'phone_full',
    'phone',
    'linkedin_url',
    'pronouns',
    'street_address',
    'city',
    'state',
    'postal_code',
    'country',
    'citizenship',
    'nationality',
    'country_of_residence',
    'school',
    'degree',
    'major',
    'graduation_date_iso',
    'graduation_date',
    'graduation_year',
    'graduation_term',
    'graduation',
    'school_start_date',
    'gpa',
    'current_employer',
    'current_role',
    'years_of_experience',
    'date_of_birth_iso',
    'date_of_birth',
    'date_of_birth_month',
    'date_of_birth_day',
    'date_of_birth_year',
    'age',
    'work_authorization',
    'current_work_status',
    'visa_expiration_date_iso',
    'visa_expiration_date',
    'internship_availability',
    'earliest_start_date',
    'latest_end_date',
    'internship_end_date_iso',
    'internship_end_date',
    'max_hours_per_week',
    'preferred_us_locations',
    'desired_pay',
    'github_url',
    'portfolio_url'
  ];

  const textControls = [];
  for (const el of allElements('input, textarea')) {{
    if (!visible(el)) continue;
    const type = norm(el.type || el.tagName);
    if (['hidden', 'file', 'button', 'submit', 'reset', 'image', 'checkbox', 'radio'].includes(type)) continue;
    if (type === 'password' && !facts.account_password) {{
      el.dataset.hybridNeedsPassword = 'true';
      result.passwordFieldsWithoutPassword += 1;
      if (isRequiredControl(el)) markRequiredEmpty(el, 'password-missing');
      continue;
    }}
    const text = labelText(el);
    if (has(text, ['honeypot', 'honey pot', 'honey-pot'])) continue;
    const [field, value] = pickTextFact(el, text);
    if (result.debugInputs.length < 16) {{
      result.debugInputs.push({{
        type,
        id: clean(el.getAttribute('id') || '').slice(0, 80),
        name: clean(el.getAttribute('name') || '').slice(0, 80),
        autocomplete: clean(el.getAttribute('autocomplete') || '').slice(0, 80),
        cls: clean(el.getAttribute('class') || '').slice(0, 100),
        label: text.slice(0, 160),
        picked: field || '',
        has_value: !!clean(el.value || ''),
        disabled: !!el.disabled,
        read_only: !!el.readOnly,
      }});
    }}
    textControls.push({{ el, type, text, field, value, handled: false }});
  }}

  async function fillTextControl(item) {{
    if (!item?.field || !item?.value) return false;
    pushLimited(
      result.matches,
      `${{item.field}} | ${{item.el.dataset.staticMatchSource || 'rule'}} | ${{item.el.dataset.staticMatchConfidence || '1.00'}} | ${{fieldSummary(item.el)}}`,
      24
    );
    const didFill = fillText(item.el, item.value, item.field);
    await selectAutocompleteOption(item.el, item.value, item.field);
    item.handled = true;
    return didFill;
  }}

  for (const wantedField of FIELD_FILL_ORDER) {{
    for (const item of textControls) {{
      if (!item.handled && item.field === wantedField) await fillTextControl(item);
    }}
  }}

  for (const item of textControls) {{
    if (item.handled) continue;
    if (item.field && item.value) {{
      await fillTextControl(item);
    }} else if (item.field === 'github_url' && factBool('has_github') === false) {{
      item.handled = true;
      item.el.dataset.staticAutofillSkipped = 'no_github';
    }} else if (has(item.text, ['phone extension', 'phone ext', 'extension'])) {{
      item.handled = true;
      item.el.dataset.staticAutofillSkipped = 'optional_phone_extension';
    }} else if (isEffectivelyEmpty(item.el)) {{
      const optionalComboboxSearch = !isRequiredControl(item.el, item.text)
        && norm(item.el.getAttribute('role')) === 'combobox'
        && has(item.text, ['search', 'select', 'all departments', 'all job types', 'tree']);
      if (optionalComboboxSearch) {{
        item.handled = true;
        item.el.dataset.staticAutofillSkipped = 'optional_combobox_search';
        continue;
      }}
      // No fact rule covered this one, so try an answer the candidate has
      // already given for the same question on an earlier application.
      const saved = bankedAnswer(item.el);
      if (saved && fillText(item.el, saved, 'saved_answer')) {{
        item.field = 'saved_answer';
        item.value = saved;
        item.handled = true;
        pushLimited(result.matches, `saved_answer | bank | 1.00 | ${{fieldSummary(item.el)}}`, 24);
        continue;
      }}
      result.unknownTextboxes += 1;
      markNeedsLlm(item.el, isRequiredControl(item.el, item.text) ? 'required-unknown-text' : 'unknown-text');
    }}
  }}

  async function fillRipplingAvailabilityFallback() {{
    const bodyText = norm(document.body?.innerText || '');
    const page = norm(location.href + ' ' + document.title);
    const value = facts.earliest_start_date_date || facts.earliest_start_date_iso || facts.earliest_start_date || facts.internship_availability;
    if (!value || !has(page, ['rippling.com']) || !has(bodyText, ['when are you available to start'])) return false;
    const requiredBlanks = textControls.filter((item) => (
      !item.handled
      && visible(item.el)
      && isRequiredControl(item.el, item.text)
      && isEffectivelyEmpty(item.el)
    ));
    const direct = requiredBlanks.find((item) => has(item.text, ['available to start', 'field 84', 'field-84', 'customquestions', 'custom questions']));
    const chosen = direct || (requiredBlanks.length === 1 ? requiredBlanks[0] : null);
    if (!chosen) return false;
    fillText(chosen.el, value, 'earliest_start_date_date');
    chosen.field = 'earliest_start_date_date';
    chosen.value = value;
    chosen.handled = true;
    return true;
  }}

  await fillRipplingAvailabilityFallback();

  for (const item of textControls) {{
    if (isRequiredControl(item.el, item.text) && isEffectivelyEmpty(item.el)) {{
      result.requiredEmpty += 1;
      markRequiredEmpty(item.el);
    }}
    if (item.el.getAttribute('aria-invalid') === 'true') {{
      result.invalidFields += 1;
      item.el.dataset.hybridInvalidField = 'true';
      pushLimited(result.visibleErrors, `Invalid field: ${{fieldSummary(item.el)}}`);
    }}
  }}

  const pageText = norm(document.body?.innerText || '');
  const verificationWords = [
    'verification code',
    'verify your email',
    'email code',
    'security code',
    'one time code',
    'one time password',
    'one-time password',
    'otp',
    'passcode',
    'confirmation code'
  ];
  const pageLooksVerification = has(pageText, verificationWords);
  for (const el of allElements('input, textarea')) {{
    if (!visible(el)) continue;
    const type = norm(el.type || el.tagName);
    if (['hidden', 'file', 'button', 'submit', 'reset', 'image', 'checkbox', 'radio', 'password'].includes(type)) continue;
    const text = norm([
      labelText(el),
      el.getAttribute('id'),
      el.getAttribute('name'),
      el.getAttribute('autocomplete'),
      el.getAttribute('inputmode'),
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder')
    ].filter(Boolean).join(' '));
    const maxLength = Number(el.getAttribute('maxlength') || 0);
    const codeSized = !maxLength || maxLength <= 12;
    const idOrName = norm(`${{el.id || ''}} ${{el.name || ''}}`);
    const ordinaryCodeField = has(idOrName, ['postal code', 'postalcode', 'zip code', 'zipcode', 'country code', 'countrycode']);
    const pinCodeish = !ordinaryCodeField && (
      has(text, ['pin code', 'pincode', 'pin-code', 'one time code', 'otp', 'verification'])
      || /(^|[-_\\s])(pin|otp|verification|security|confirmation)[-_\\s]*(code)?($|[-_\\s])/i.test(idOrName)
      || /(^|[-_\\s])code($|[-_\\s])/i.test(idOrName)
    );
    if (pinCodeish || has(text, verificationWords) || (pageLooksVerification && codeSized)) {{
      el.dataset.hybridNeedsOtp = 'true';
      result.verificationCodeRequired = true;
    }}
  }}

  for (const select of allElements('select')) {{
    if (!visible(select)) continue;
    let text = labelText(select);
    text = preferPrimaryFieldText(select, text);
    let field = null, value = null;
    if (has(text, ['require sponsorship', 'need sponsorship', 'needs sponsorship', 'visa sponsorship', 'sponsor you', 'sponsorship for employment', 'sponsorship to work'])) [field, value] = ['visa_sponsorship_needed', yesNoValue(select, factBool('visa_sponsorship_needed') || factBool('future_sponsorship_needed') || factBool('h1b_sponsorship_needed'))];
    else if (has(text, ['legally authorized', 'authorized to work', 'eligible to work', 'work lawfully', 'work in the united states']) && !has(text, ['sponsor'])) [field, value] = ['authorized_to_work_us', yesNoValue(select, factBool('authorized_to_work_us'))];
    else if (has(text, ['birth', 'dob', 'date of birth']) && has(text, ['month', 'mm'])) [field, value] = ['date_of_birth_month', optionValueFor(select, 'date_of_birth_month', facts.date_of_birth_month)];
    else if (has(text, ['birth', 'dob', 'date of birth']) && has(text, ['day', 'dd'])) [field, value] = ['date_of_birth_day', optionValueFor(select, 'date_of_birth_day', facts.date_of_birth_day)];
    else if (has(text, ['birth', 'dob', 'date of birth']) && has(text, ['year', 'yyyy'])) [field, value] = ['date_of_birth_year', optionValueFor(select, 'date_of_birth_year', facts.date_of_birth_year)];
    // "Are you currently on an F-1 visa?" offers Yes/No, not a list of statuses.
    // Answering it with the visa_status text matched no option and left it blank.
    else if (has(text, ['f 1', 'f1', 'f-1']) && yesNoValue(select, true)) [field, value] = ['f1_visa_status', yesNoValue(select, factBool('f1_visa_status') !== false)];
    else if (has(text, ['visa type', 'visa status', 'immigration status', 'student status', 'f 1', 'f1', 'f-1'])) [field, value] = ['visa_status', optionValueFor(select, 'visa_status', facts.visa_status || facts.f1_status || facts.f1_visa_status || facts.current_work_status)];
    else if (has(text, ['country of citizenship', 'citizenship country', 'citizenship'])) [field, value] = ['citizenship', optionValueFor(select, 'citizenship', facts.citizenship || facts.nationality)];
    else if (has(text, ['nationality', 'country of nationality'])) [field, value] = ['nationality', optionValueFor(select, 'nationality', facts.nationality || facts.citizenship)];
    else if (has(text, ['country of residence', 'legal residence country', 'residency country'])) [field, value] = ['country_of_residence', optionValueFor(select, 'country_of_residence', facts.country_of_residence)];
    else if (has(text, ['country'])) [field, value] = ['country', optionValueFor(select, 'country', facts.country)];
    else if (!hasAny(text, ['opt', 'cpt', 'f 1', 'f1', 'visa', 'work status', 'sponsorship']) && hasAny(text, ['address', 'location', 'city', 'zip', 'postal', 'country', 'state province', 'state/province']) && has(text, ['state', 'province'])) [field, value] = ['state', optionValueFor(select, 'state', facts.state)];
    else if (has(text, ['degree'])) [field, value] = ['degree', optionValueFor(select, 'degree', facts.degree) || optionValueFor(select, 'degree', 'bachelor')];
    else if (has(text, ['education start', 'school start', 'started school']) || (has(text, ['start date']) && has(text, ['school', 'university', 'college', 'education']))) [field, value] = ['education_start_date', optionValueFor(select, 'education_start_date', facts.education_start_date || facts.school_start_date)];
    else if (has(text, ['education end', 'school end']) || (has(text, ['end date', 'completion date']) && has(text, ['school', 'university', 'college', 'education']))) [field, value] = ['education_end_date', optionValueFor(select, 'education_end_date', facts.education_end_date || facts.graduation)];
    // The school NAME itself, as a native <select> (e.g. a world-university
    // list) — this was missing entirely; only the start/end-date variants
    // above were handled, so a plain "University" dropdown fell through to
    // no field match at all and the LLM was left to guess (a real live
    // failure landed on the wrong, unrelated "Virginia Commonwealth
    // University" instead of "Virginia Tech"'s real entry).
    else if (has(text, ['university', 'college', 'school']) && !has(text, ['start', 'end', 'graduat'])) [field, value] = ['school', optionValueFor(select, 'school', facts.school)];
    else if (has(text, ['graduation', 'graduate'])) [field, value] = ['graduation', optionValueFor(select, 'graduation', facts.graduation_term || facts.graduation)];
    else if (has(text, ['authorized', 'authorization', 'eligible to work', 'work in the united states'])) [field, value] = ['authorized_to_work_us', yesNoValue(select, factBool('authorized_to_work_us'))];
    else if (has(text, ['sponsor', 'sponsorship', 'visa'])) [field, value] = ['visa_sponsorship_needed', yesNoValue(select, factBool('visa_sponsorship_needed'))];
    else if (has(text, ['cpt'])) [field, value] = ['cpt_status', optionValueFor(select, 'cpt_status', facts.cpt_status || facts.cpt_eligible) || yesNoValue(select, factBool('cpt_eligible'))];
    else if (has(text, ['opt'])) [field, value] = ['opt_status', optionValueFor(select, 'opt_status', facts.opt_status || facts.opt_eligible) || yesNoValue(select, factBool('opt_eligible'))];
    else if (has(text, ['18', 'age'])) [field, value] = ['age_over_18', yesNoValue(select, factBool('age_over_18'))];
    else if (has(text, ['relocate'])) [field, value] = ['willing_to_relocate', yesNoValue(select, factBool('willing_to_relocate'))];
    // Label text absorbs neighbouring questions on dense EEO forms, so the more
    // specific phrase has to be tested first. "How would you describe your
    // sexual orientation?" sitting under a "Disability status" question was
    // being answered as disability, matched no option, and stayed blank.
    else if (has(text, ['sexual orientation'])) [field, value] = ['sexual_orientation', optionValueFor(select, 'sexual_orientation', facts.sexual_orientation)];
    else if (has(text, ['veteran'])) [field, value] = ['veteran_status', optionValueFor(select, 'veteran_status', facts.veteran_status) || yesNoValue(select, false)];
    else if (has(text, ['disability'])) [field, value] = ['disability_status', optionValueFor(select, 'disability_status', facts.disability_status) || yesNoValue(select, false)];
    else if (has(text, ['orientation'])) [field, value] = ['sexual_orientation', optionValueFor(select, 'sexual_orientation', facts.sexual_orientation)];
    else if (has(text, ['pronoun'])) [field, value] = ['pronouns', optionValueFor(select, 'pronouns', facts.pronouns)];
    else if (has(text, ['gender', 'sex'])) [field, value] = ['gender', optionValueFor(select, 'gender', facts.gender)];
    else if (has(text, ['race', 'ethnicity'])) [field, value] = ['race_ethnicity', optionValueFor(select, 'race_ethnicity', facts.race_ethnicity) || optionValueFor(select, 'race_ethnicity', 'asian')];
    else if (has(text, ['hispanic', 'latino'])) [field, value] = ['hispanic_latino', yesNoValue(select, factBool('hispanic_latino')) || optionValueFor(select, 'hispanic_latino', facts.hispanic_latino)];
    else if (has(text, ['driver', 'license', 'licence'])) [field, value] = ['driver_license', yesNoValue(select, factBool('driver_license'))];
    else if (has(text, ['accommodation', 'accommodations'])) [field, value] = ['accommodations_needed', yesNoValue(select, factBool('accommodations_needed'))];
    else if (has(text, ['github'])) [field, value] = ['has_github', yesNoValue(select, factBool('has_github'))];
    else if (has(text, ['work mode', 'remote', 'hybrid', 'onsite'])) [field, value] = ['preferred_work_mode', optionValueFor(select, 'preferred_work_mode', facts.preferred_work_mode)];
    if (value) {{
      pushLimited(result.matches, `${{field}} | rule | 1.00 | ${{fieldSummary(select)}}`, 24);
      setSelect(select, value, field);
    }}
    if (isEffectivelyEmpty(select)) {{
      if (isRequiredControl(select, text)) {{
        result.requiredEmpty += 1;
        markRequiredEmpty(select, 'required-select-empty');
      }} else {{
        markNeedsLlm(select, 'optional-select-empty');
      }}
    }}
    if (select.getAttribute('aria-invalid') === 'true') {{
      result.invalidFields += 1;
      select.dataset.hybridInvalidField = 'true';
      pushLimited(result.visibleErrors, `Invalid select: ${{fieldSummary(select)}}`);
    }}
  }}

  // Workday renders some dropdowns as a <button aria-label="Select One
  // Required" ...> that opens a popup listbox on click, not a native
  // <select> or input[role=combobox] -- neither of which this scan visits.
  // A live run had exactly this markup for "Highest level of education?":
  // it sat blank with a visible validation error the whole run, invisible
  // to requiredEmpty/openQuestions/needsLlm because nothing ever looked at
  // <button> elements. This does not attempt to answer it -- the LLM
  // cleanup agent already does, and correctly resolving a Workday popup
  // listbox from here is out of scope -- it only makes sure a still-blank
  // one gets recorded so it reaches Q&A instead of vanishing silently.
  for (const trigger of allElements('button')) {{
    if (isAbandoned(trigger) || !visible(trigger)) continue;
    const buttonText = norm(trigger.innerText || trigger.textContent || '');
    const ariaLabel = norm(trigger.getAttribute('aria-label') || '');
    if (buttonText !== 'select one' && !ariaLabel.startsWith('select one')) continue;
    const text = labelText(trigger);
    if (isRequiredControl(trigger, text)) {{
      result.requiredEmpty += 1;
      markRequiredEmpty(trigger, 'required-workday-select-button-empty');
    }} else {{
      markNeedsLlm(trigger, 'optional-workday-select-button-empty');
    }}
    if (trigger.getAttribute('aria-invalid') === 'true') {{
      result.invalidFields += 1;
      trigger.dataset.hybridInvalidField = 'true';
      pushLimited(result.visibleErrors, `Invalid select: ${{fieldSummary(trigger)}}`);
    }}
  }}

  function choiceLockKey(field, groupText, optionText) {{
    return [field || 'choice', norm(groupText || '').slice(0, 220), norm(optionText || '').slice(0, 120)].join('|');
  }}

  function setChoice(input, field, lockKey = '') {{
    if (!visible(input) || input.checked) {{
      const labels = [...(input.labels || []), input.closest?.('label')].filter(Boolean);
      if (input.checked) labels.forEach((label) => {{ label.dataset.staticChoiceLocked = 'true'; }});
      return false;
    }}
    if (lockKey && window.__STATIC_CHOICE_LOCKS[lockKey]) return false;
    window.__STATIC_CHOICE_CLICK_GUARD.bypass = true;
    try {{
      input.click();
    }} finally {{
      window.__STATIC_CHOICE_CLICK_GUARD.bypass = false;
    }}
    input.dataset.staticAutofilled = field;
    input.dataset.staticChoiceLocked = 'true';
    const labels = [...(input.labels || []), input.closest?.('label')].filter(Boolean);
    labels.forEach((label) => {{ label.dataset.staticChoiceLocked = 'true'; }});
    if (lockKey) window.__STATIC_CHOICE_LOCKS[lockKey] = true;
    result.choices += 1;
    // A native radio/checkbox reliably shows its own checked state, but a
    // vision model reading a long, legalese-heavy question block (veteran
    // status, disability) can still miss that and re-click an already-locked
    // answer. pointer-events:none blocks further clicks and drops it from
    // browser_use's interactive-element index — deliberately NOT setting
    // `disabled`, which would exclude this control's value from the actual
    // form submission (browsers omit disabled fields from FormData).
    try {{
      input.style.pointerEvents = 'none';
      input.setAttribute('aria-disabled', 'true');
      input.setAttribute('tabindex', '-1');
    }} catch (_) {{}}
    return true;
  }}

  function valueMatchesFact(valueText, fact) {{
    const factText = norm(fact);
    if (!factText) return false;
    if (factText.includes('asian') || factText.includes('indian')) {{
      if (valueText.includes('american indian') || valueText.includes('alaskan native')) return false;
      if (valueText.includes('asian')) return true;
      if (factText.includes('asian indian') && valueText.includes('asian indian')) return true;
    }}
    // "female" contains "male", so substring comparison silently matched the
    // Female option for a male candidate and submitted the wrong answer.
    // Gender words are compared on whole words only, in both directions.
    const isFemaleWord = (text) => /\\bfemale\\b|\\bwoman\\b|\\bwomen\\b/.test(text);
    const isMaleWord = (text) => /\\bmale\\b|\\bman\\b|\\bmen\\b/.test(text) && !isFemaleWord(text);
    if (isMaleWord(factText) && isFemaleWord(valueText)) return false;
    if (isFemaleWord(factText) && isMaleWord(valueText)) return false;
    if (valueText === factText) return true;
    // "no" is a substring of "not", so a plain includes() matched "I do NOt
    // want to answer" for a candidate whose fact is "No" -- the opposite of
    // a real answer. Short tokens (yes/no/he and similar) require a whole-
    // word match; longer phrases keep the existing substring comparison.
    const wordBoundaryIncludes = (haystack, needle) => new RegExp(`\\b${{needle.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&')}}\\b`).test(haystack);
    const SHORT_TOKEN_LENGTH = 3;
    if (factText.length <= SHORT_TOKEN_LENGTH) {{
      if (wordBoundaryIncludes(valueText, factText)) return true;
    }} else if (valueText.length <= SHORT_TOKEN_LENGTH) {{
      if (wordBoundaryIncludes(factText, valueText)) return true;
    }} else if (valueText.includes(factText) || factText.includes(valueText)) {{
      return true;
    }}
    if (isMaleWord(factText) && isMaleWord(valueText)) return true;
    if (isFemaleWord(factText) && isFemaleWord(valueText)) return true;
    if (factText.includes('he') && valueText.includes('he') && valueText.includes('him')) return true;
    if (factText.includes('not a veteran') && valueText.includes('not') && valueText.includes('veteran')) return true;
    if (factText.includes('no') && valueText.includes('do not') && valueText.includes('disability')) return true;
    return false;
  }}

  function maybeSetYesNo(input, valueText, field, answerYes) {{
    const isYes = has(valueText, ['yes', 'true', 'authorized', 'i am']);
    const isNo = has(valueText, ['no', 'false', 'not']);
    if ((answerYes && isYes) || (!answerYes && isNo)) return setChoice(input, field);
    return false;
  }}

  function controlChecked(control) {{
    if (control.checked) return true;
    const aria = norm(control.getAttribute?.('aria-checked'));
    const state = norm(control.getAttribute?.('checkbox-state') || control.getAttribute?.('data-state') || control.getAttribute?.('data-checked'));
    return aria === 'true' || state === 'checked' || state === 'true' || state === 'selected';
  }}

  function setChoiceControl(control, field, lockKey = '') {{
    if (!visible(control) || controlChecked(control)) return false;
    const nestedNativeChoice = control.querySelector?.('input[type="radio"], input[type="checkbox"]');
    if (nestedNativeChoice?.checked) return false;
    if (lockKey && window.__STATIC_CHOICE_LOCKS[lockKey]) return false;
    window.__STATIC_CHOICE_CLICK_GUARD.bypass = true;
    try {{
      // A live run left this marked autofilled/locked, but the underlying
      // native checkbox still reported aria-checked="false" -- the click
      // had landed on a wrapper/label proxy that never toggled the real
      // input Workday's own validation reads. A custom-styled checkbox
      // often hides the real native input behind CSS (opacity, 1px sizing)
      // for accessibility, which would also fail clickLikeHuman's own
      // visibility gate -- so call the native input's own .click()
      // directly, bypassing that gate entirely, rather than routing
      // through the generic wrapper-click path.
      if (nestedNativeChoice) {{
        try {{ nestedNativeChoice.click(); }} catch (_) {{ clickLikeHuman(control); }}
      }} else {{
        clickLikeHuman(control);
      }}
    }} finally {{
      window.__STATIC_CHOICE_CLICK_GUARD.bypass = false;
    }}
    control.dataset.staticAutofilled = field;
    control.dataset.staticChoiceLocked = 'true';
    if (lockKey) window.__STATIC_CHOICE_LOCKS[lockKey] = true;
    result.choices += 1;
    // Same reasoning as setChoice()/finalizeSelection() above: block further
    // interaction so a vision model can't re-click an already-correct custom
    // choice pill, without using `disabled` (this control itself isn't a
    // named form field — it's a clickable proxy for one — but leave its
    // value-carrying behavior alone on the off chance it is).
    try {{
      control.style.pointerEvents = 'none';
      control.setAttribute('aria-disabled', 'true');
      control.setAttribute('tabindex', '-1');
    }} catch (_) {{}}
    return true;
  }}

  function ancestorTexts(control, limit = 10) {{
    const texts = [];
    let node = escapeShadowBoundary(control);
    for (let depth = 0; node && depth < limit; depth += 1, node = node.parentElement) {{
      const text = clean(node.innerText || node.textContent || '');
      if (text && text.length <= 1800) texts.push({{ node, text, normText: norm(text) }});
    }}
    return texts;
  }}

  function closestQuestionText(control) {{
    const host = escapeShadowBoundary(control);
    const explicit = host.closest?.('[data-testid], [data-qa], .form-group, .field, [class*="field"], [class*="Field"], .application-question, [class*="question"], [class*="Question"], [role="group"], fieldset');
    const explicitText = clean(explicit?.innerText || explicit?.textContent || '');
    if (explicitText && explicitText.length <= 1800) return norm(explicitText);

    const questionWords = [
      'prior internship', 'previous internship', 'how many internships', 'number of internships',
      'previously worked', 'former employee', 'current employee', 'current contractor',
      'worked with us before', 'worked here before', 'worked for us before',
      '18 years of age', 'age of 18', '18 or older', 'at least 18',
      'how did you hear', 'hear about', 'source',
      'type of role', 'role are you interested', 'role interest', 'interested in',
      'github', 'degree type', 'degree', 'education level',
      'pronoun', 'pronouns', 'gender', 'sex', 'race', 'ethnicity', 'hispanic', 'latino',
      'veteran', 'disability', 'authorized', 'authorization', 'eligible to work', 'work lawfully',
      'sponsor', 'sponsorship', 'visa status', 'visa type', 'immigration status', 'student status', 'employment visa', 'work visa',
      'f-1', 'f1', 'f 1', 'cpt', 'opt',
      'accommodation', 'accommodations', 'driver', 'license', 'licence',
      'terms of use', 'privacy agreement', 'privacy policy', 'applicant privacy', 'candidate privacy',
      'data privacy', 'terms and conditions', 'accept terms'
    ];
    const ancestor = ancestorTexts(control).find((item) => has(item.normText, questionWords));
    return ancestor ? ancestor.normText : '';
  }}

  function optionLabelText(control) {{
    const host = escapeShadowBoundary(control);
    const label = host.closest?.('label');
    if (label) return norm(label.innerText || label.textContent || '');
    const parent = host.parentElement;
    const parentText = clean(parent?.innerText || parent?.textContent || '');
    if (parentText && parentText.length <= 220) return norm(parentText);
    return norm([
      control.innerText,
      control.textContent,
      control.getAttribute?.('aria-label'),
      control.getAttribute?.('title'),
      control.getAttribute?.('value')
    ].filter(Boolean).join(' '));
  }}

  function compactOptionText(text, groupText = '') {{
    let optionText = norm(text);
    if (!groupText) return optionText;
    const group = norm(groupText);
    if (optionText.startsWith(group)) optionText = optionText.slice(group.length).trim();
    return optionText;
  }}

  function customChoiceTarget(control) {{
    const rawOwnText = String(control.innerText || control.textContent || '');
    const ownText = clean(rawOwnText);
    const ownLooksLikeQuestionGroup = ownText.length > 90 || rawOwnText.includes(String.fromCharCode(10)) || ownText.includes('?');
    if (ownLooksLikeQuestionGroup && has(norm(ownText), [
      'prior internship', 'previous internship', 'how many internships', 'number of internships',
      'previously worked', 'former employee', 'current employee', 'current contractor',
      'worked with us before', 'worked here before', 'worked for us before',
      '18 years of age', 'age of 18', '18 or older', 'at least 18',
      'how did you hear', 'hear about', 'source',
      'type of role', 'role are you interested', 'role interest', 'interested in',
      'github', 'degree type', 'education level',
      'pronoun', 'pronouns', 'gender', 'sex', 'race', 'ethnicity', 'hispanic', 'latino',
      'veteran', 'disability', 'authorized', 'authorization', 'eligible to work', 'work lawfully',
      'sponsor', 'sponsorship', 'visa status', 'employment visa', 'work visa',
      'accommodation', 'accommodations', 'driver', 'license', 'licence',
      'terms of use', 'privacy agreement', 'privacy policy', 'applicant privacy', 'candidate privacy',
      'data privacy', 'terms and conditions', 'accept terms'
    ])) return null;

    const choiceHost = escapeShadowBoundary(control);
    const preferred = choiceHost.closest?.(
      'label, button, [role="radio"], [role="checkbox"], [aria-checked], ' +
      '[data-automation-id*="radio"], [data-automation-id*="checkbox"], ' +
      '[data-testid*="radio"], [data-testid*="checkbox"], [data-testid*="option"], ' +
      '[data-qa*="radio"], [data-qa*="checkbox"], [data-qa*="option"], ' +
      '[class*="radio"], [class*="Radio"], [class*="checkbox"], [class*="Checkbox"], [class*="option"], [class*="Option"]'
    );
    if (preferred && visible(preferred)) return preferred;

    let best = visible(control) ? control : null;
    let node = choiceHost.parentElement;
    for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {{
      const text = clean(node.innerText || node.textContent || '');
      const nodeNorm = norm(text);
      if (has(nodeNorm, [
        'prior internship', 'previous internship', 'how many internships', 'number of internships',
        'previously worked', 'former employee', 'current employee', 'current contractor',
        'worked with us before', 'worked here before', 'worked for us before',
        '18 years of age', 'age of 18', '18 or older', 'at least 18',
        'how did you hear', 'hear about', 'source',
        'type of role', 'role are you interested', 'role interest', 'interested in',
        'github', 'degree type', 'education level',
        'pronoun', 'pronouns', 'gender', 'sex', 'race', 'ethnicity', 'hispanic', 'latino',
        'veteran', 'disability', 'authorized', 'authorization', 'eligible to work', 'work lawfully',
        'sponsor', 'sponsorship', 'visa status', 'employment visa', 'work visa',
        'accommodation', 'accommodations', 'driver', 'license', 'licence'
      ])) break;
      if (text && text.length <= 120 && visible(node)) best = node;
      if (text && text.length > 120) break;
    }}
    return best || control;
  }}

  function rawCandidateOptionText(control, target, groupText) {{
    // innerText and textContent are nearly always identical for a plain-text
    // option cell -- joining both doubled the text. A 66-char answer like
    // "No, I do not have a disability and have not had one in the past"
    // became 132 chars, over the 120-char cap below, so it was silently
    // skipped entirely and never matched against any fact.
    const textOf = (el) => (el?.innerText || el?.textContent || '');
    const rawPieces = [
      textOf(control),
      control.getAttribute?.('aria-label'),
      control.getAttribute?.('title'),
      control.getAttribute?.('value'),
      target !== control ? textOf(target) : ''
    ].filter(Boolean).join(' ');
    return compactOptionText(rawPieces, groupText);
  }}

  function matchesPriorInternships(optionText, groupText) {{
    if (!has(groupText, ['prior internship', 'previous internship', 'how many internships', 'number of internships', 'internships have you had'])) return false;
    const wanted = norm(facts.prior_internships || '0');
    const compactOption = optionText.replace(/\\s+/g, '');
    const compactWanted = wanted.replace(/\\s+/g, '');
    if (!compactWanted) return false;
    if (optionText === wanted || compactOption === compactWanted) return true;
    if (compactWanted === '0') return /^0+$/.test(compactOption) || has(optionText, ['0 internships', 'zero', 'none', 'no internships']);
    if (compactWanted === '1') return has(optionText, ['1 internship', 'one internship', 'one']);
    if (compactWanted === '2') return has(optionText, ['2 internships', 'two internships', 'two']);
    if (compactWanted === '3') return has(optionText, ['3 internships', 'three internships', 'three']);
    return false;
  }}

  function matchesHeardAbout(optionText, groupText) {{
    return has(groupText, ['how did you hear', 'hear about', 'source'])
      && has(optionText, [norm(facts.heard_about || 'linkedin'), 'linkedin']);
  }}

  function matchesPreviouslyWorked(optionText, groupText) {{
    if (!has(groupText, [
      'previously worked', 'former employee', 'current employee', 'current contractor',
      'worked with us before', 'worked here before', 'worked for us before',
    ])) return false;
    const answerYes = factBool('previously_worked_for_company');
    if (answerYes) return has(optionText, ['yes', 'true']);
    return has(optionText, ['no', 'false']) && !has(optionText, ['not sure']);
  }}

  function matchesAgeOver18(optionText, groupText) {{
    if (!has(groupText, ['18 years of age', 'age of 18', '18 or older', 'at least 18'])) return false;
    const answerYes = factBool('age_over_18');
    if (answerYes) return has(optionText, ['yes', 'true']);
    return has(optionText, ['no', 'false']);
  }}

  function matchesState(optionText, groupText) {{
    if (!has(groupText, ['state', 'province', 'region']) || !facts.state) return false;
    const wanted = norm(facts.state);
    return optionText === wanted || optionText.startsWith(`${{wanted}} `);
  }}

  function matchesRoleInterest(optionText, groupText) {{
    if (!has(groupText, ['type of role', 'role are you interested', 'role interest', 'interested in'])) return false;
    const wanted = norm(facts.role_interest || 'software engineering backend platform data infrastructure');
    return ['software', 'backend', 'platform', 'data', 'infra', 'infrastructure'].some((word) => wanted.includes(word) && optionText.includes(word));
  }}

  function matchesNoGithub(optionText, groupText) {{
    return has(groupText, ['github'])
      && factBool('has_github') === false
      && has(optionText, ['no', 'none', 'n a', 'not applicable']);
  }}

  function matchesAuthorizedToWork(optionText, groupText) {{
    return has(groupText, ['authorized', 'authorization', 'eligible to work', 'work lawfully', 'work in the united states'])
      && factBool('authorized_to_work_us')
      && has(optionText, ['yes', 'i am', 'authorized']);
  }}

  function matchesVisaStatus(optionText, groupText) {{
    if (!has(groupText, ['visa status', 'visa type', 'immigration status', 'student status', 'current visa', 'current status', 'f-1', 'f1', 'f 1'])) return false;
    if (!factBool('f1_visa_status') && !factBool('f1_status') && !has(norm(facts.visa_status || facts.current_work_status || ''), ['f1', 'f 1', 'f-1'])) return false;
    return has(optionText, ['f1', 'f 1', 'f-1', 'student visa', 'student']);
  }}

  function matchesCptStatus(optionText, groupText) {{
    if (!has(groupText, ['cpt', 'curricular practical training'])) return false;
    if (has(optionText, ['not eligible', 'no', 'none'])) return false;
    return factBool('cpt_eligible') && has(optionText, ['yes', 'eligible', 'cpt', 'curricular practical training']);
  }}

  function matchesOptStatus(optionText, groupText) {{
    if (!has(groupText, ['opt', 'optional practical training'])) return false;
    if (has(optionText, ['not eligible', 'no', 'none'])) return false;
    return factBool('opt_eligible') && has(optionText, ['yes', 'eligible', 'opt', 'optional practical training']);
  }}

  function matchesSponsorship(optionText, groupText) {{
    if (!has(groupText, ['sponsor', 'sponsorship', 'visa status', 'employment visa', 'work visa'])) return false;
    if (has(optionText, ['forgot', 'reset', 'not require', 'no sponsorship', 'do not require'])) return false;
    if ((factBool('h1b_sponsorship_needed') || factBool('future_sponsorship_needed')) && has(optionText, ['h1b', 'h 1b', 'h-1b', 'h-1 b'])) return true;
    if (factBool('f1_visa_status') && has(optionText, ['f1', 'f 1', 'f-1'])) return true;
    if (factBool('cpt_eligible') && has(optionText, ['cpt'])) return true;
    if (factBool('opt_eligible') && has(optionText, ['opt'])) return true;
    if ((factBool('visa_sponsorship_needed') || factBool('future_sponsorship_needed') || factBool('h1b_sponsorship_needed')) && has(optionText, ['yes', 'require sponsorship', 'will require', 'need sponsorship', 'needs sponsorship'])) return true;
    if (!factBool('visa_sponsorship_needed') && has(optionText, ['no', 'not require', 'do not require'])) return true;
    return false;
  }}

  function matchesDegreeType(optionText, groupText) {{
    return has(groupText, ['degree type', 'degree', 'education level', 'level of education'])
      && has(optionText, ['undergraduate', 'bachelor', 'bachelors', 'bachelor s', 'bs', 'b s']);
  }}

  function matchesGender(optionText, groupText) {{
    return has(groupText, ['gender', 'sex'])
      && valueMatchesFact(optionText, facts.gender);
  }}

  function matchesPronouns(optionText, groupText) {{
    return has(groupText, ['pronoun'])
      && valueMatchesFact(optionText, facts.pronouns);
  }}

  function matchesRace(optionText, groupText) {{
    return has(groupText, ['race', 'ethnicity'])
      && valueMatchesFact(optionText, facts.race_ethnicity);
  }}

  function matchesHispanic(optionText, groupText) {{
    return has(groupText, ['hispanic', 'latino'])
      && maybeOptionMatchesYesNo(optionText, factBool('hispanic_latino'));
  }}

  function matchesVeteran(optionText, groupText) {{
    return has(groupText, ['veteran'])
      && valueMatchesFact(optionText, facts.veteran_status);
  }}

  function matchesDisability(optionText, groupText) {{
    return has(groupText, ['disability'])
      && valueMatchesFact(optionText, facts.disability_status);
  }}

  function matchesAccommodation(optionText, groupText) {{
    return has(groupText, ['accommodation', 'accommodations'])
      && maybeOptionMatchesYesNo(optionText, factBool('accommodations_needed'));
  }}

  function matchesDriverLicense(optionText, groupText) {{
    return has(groupText, ['driver', 'license', 'licence'])
      && maybeOptionMatchesYesNo(optionText, factBool('driver_license'));
  }}

  function matchesTermsAcceptance(optionText, groupText) {{
    const combined = norm(`${{groupText || ''}} ${{optionText || ''}}`);
    if (!has(combined, ['terms of use', 'privacy agreement', 'privacy policy', 'applicant privacy', 'candidate privacy', 'data privacy', 'terms and conditions', 'terms and privacy', 'accept terms'])) return false;
    if (has(combined, ['do not accept', 'don t accept', 'decline', 'reject', 'disagree'])) return false;
    return has(combined, ['accept', 'agree', 'i have read', 'terms of use', 'privacy agreement', 'privacy policy', 'applicant privacy', 'candidate privacy', 'data privacy']);
  }}

  function maybeOptionMatchesYesNo(optionText, answerYes) {{
    const isYes = has(optionText, ['yes', 'true', 'i am', 'authorized']);
    const isNo = has(optionText, ['no', 'false', 'not', 'do not', 'none']);
    return (answerYes && isYes) || (!answerYes && isNo);
  }}

  function customFieldMatch(optionText, groupText) {{
    if (matchesAuthorizedToWork(optionText, groupText)) return 'authorized_to_work_us';
    if (matchesVisaStatus(optionText, groupText)) return 'visa_status';
    if (matchesCptStatus(optionText, groupText)) return 'cpt_status';
    if (matchesOptStatus(optionText, groupText)) return 'opt_status';
    if (matchesSponsorship(optionText, groupText)) return 'visa_sponsorship_needed';
    if (matchesPriorInternships(optionText, groupText)) return 'prior_internships';
    if (matchesHeardAbout(optionText, groupText)) return 'heard_about';
    if (matchesPreviouslyWorked(optionText, groupText)) return 'previously_worked_for_company';
    if (matchesAgeOver18(optionText, groupText)) return 'age_over_18';
    if (matchesState(optionText, groupText)) return 'state';
    if (matchesRoleInterest(optionText, groupText)) return 'role_interest';
    if (matchesDegreeType(optionText, groupText)) return 'degree';
    if (matchesPronouns(optionText, groupText)) return 'pronouns';
    if (matchesGender(optionText, groupText)) return 'gender';
    if (matchesRace(optionText, groupText)) return 'race_ethnicity';
    if (matchesHispanic(optionText, groupText)) return 'hispanic_latino';
    if (matchesVeteran(optionText, groupText)) return 'veteran_status';
    if (matchesDisability(optionText, groupText)) return 'disability_status';
    if (matchesAccommodation(optionText, groupText)) return 'accommodations_needed';
    if (matchesDriverLicense(optionText, groupText)) return 'driver_license';
    if (matchesTermsAcceptance(optionText, groupText)) return 'terms_acceptance';
    if (matchesNoGithub(optionText, groupText)) return 'has_github';
    return '';
  }}

  function comboboxCurrentText(control) {{
    return norm([
      control.innerText,
      control.textContent,
      control.getAttribute?.('aria-label'),
      control.getAttribute?.('value')
    ].filter(Boolean).join(' '));
  }}

  function comboboxLooksEmpty(control) {{
    const text = comboboxCurrentText(control);
    if (!text) return true;
    if (/\\b0\\s+items?\\s+selected\\b/.test(text)) return true;
    if (has(text, ['select', 'choose', 'please select', 'no selection'])) return true;
    return false;
  }}

  async function fillCustomCombobox(control) {{
    if (!visible(control) || control.dataset.staticAutofilled) return false;
    if (!comboboxLooksEmpty(control)) return false;
    const groupText = closestQuestionText(control) || labelText(control);
    if (!groupText) return false;
    const beforeChoices = result.choices + result.selects;
    if (!clickLikeHuman(control)) return false;
    await sleep(250);
    const optionSelector = [
      '[role="option"]',
      '[role="listbox"] [role="option"]',
      '[role="listbox"] li',
      '[role="menuitem"]',
      '[id*="list-option"]',
      '[data-testid*="option"]',
      '[data-qa*="option"]',
      '[class*="option"]',
      '[class*="Option"]'
    ].join(',');
    const options = allElements(optionSelector)
      .filter(visible)
      .map((option) => {{
        const target = option.closest?.('[role="option"], [role="menuitem"], li, button, [data-testid], [data-qa], [class*="option"], [class*="Option"]') || option;
        const optionText = rawCandidateOptionText(option, target, groupText) || optionLabelText(option);
        const matchedField = customFieldMatch(optionText, groupText);
        return {{ target, optionText, matchedField }};
      }})
      .filter((item) => item.matchedField && item.optionText && item.optionText.length <= 220);
    const match = options[0];
    if (!match) return false;
    if (setChoiceControl(match.target, match.matchedField, choiceLockKey(match.matchedField, groupText, match.optionText))) {{
      control.dataset.staticAutofilled = match.matchedField;
      control.dataset.staticChoiceLocked = 'true';
      if (result.choices + result.selects === beforeChoices) result.selects += 1;
      return true;
    }}
    return false;
  }}

  for (const control of allElements(
    '[role="combobox"], [aria-haspopup="listbox"], button[aria-label*="Select"], button[aria-label*="select"], button[id*="countryRegion"]'
  )) {{
    await fillCustomCombobox(control);
  }}

  const choices = allElements('input[type="radio"], input[type="checkbox"]');
  for (const input of choices) {{
    const text = labelText(input);
    const valueText = norm(input.value + ' ' + (input.closest('label')?.innerText || ''));
    const groupText = closestQuestionText(input) || text;
    const optionText = rawCandidateOptionText(input, input.closest('label') || input, groupText) || valueText;
    const matchedField = customFieldMatch(optionText, groupText);
    if (matchedField) {{
      setChoice(input, matchedField);
      continue;
    }}
    if (has(text, ['authorized', 'authorization', 'eligible to work', 'work in the united states'])) {{
      maybeSetYesNo(input, valueText, 'authorized_to_work_us', factBool('authorized_to_work_us'));
    }} else if (has(text, ['visa type', 'visa status', 'immigration status', 'student status', 'f 1', 'f1', 'f-1'])) {{
      if (matchesVisaStatus(valueText, text)) setChoice(input, 'visa_status');
    }} else if (has(text, ['sponsor', 'sponsorship', 'visa'])) {{
      maybeSetYesNo(input, valueText, 'visa_sponsorship_needed', factBool('visa_sponsorship_needed'));
    }} else if (has(text, ['cpt'])) {{
      maybeSetYesNo(input, valueText, 'cpt_status', factBool('cpt_eligible'));
    }} else if (has(text, ['opt'])) {{
      maybeSetYesNo(input, valueText, 'opt_status', factBool('opt_eligible'));
    }} else if (has(text, ['18', 'age'])) {{
      maybeSetYesNo(input, valueText, 'age_over_18', factBool('age_over_18'));
    }} else if (has(text, ['relocate'])) {{
      maybeSetYesNo(input, valueText, 'willing_to_relocate', factBool('willing_to_relocate'));
    }} else if (has(text, ['driver', 'license', 'licence'])) {{
      maybeSetYesNo(input, valueText, 'driver_license', factBool('driver_license'));
    }} else if (has(text, ['accommodation', 'accommodations'])) {{
      maybeSetYesNo(input, valueText, 'accommodations_needed', factBool('accommodations_needed'));
    }} else if (has(text, ['github'])) {{
      maybeSetYesNo(input, valueText, 'has_github', factBool('has_github'));
    }} else if (has(text, ['veteran']) && valueMatchesFact(valueText, facts.veteran_status)) {{
      setChoice(input, 'veteran_status');
    }} else if (has(text, ['disability']) && valueMatchesFact(valueText, facts.disability_status)) {{
      setChoice(input, 'disability_status');
    }} else if (has(text, ['gender', 'sex']) && valueMatchesFact(valueText, facts.gender)) {{
      setChoice(input, 'gender');
    }} else if (has(text, ['race', 'ethnicity']) && valueMatchesFact(valueText, facts.race_ethnicity)) {{
      setChoice(input, 'race_ethnicity');
    }} else if (has(text, ['hispanic', 'latino'])) {{
      maybeSetYesNo(input, valueText, 'hispanic_latino', factBool('hispanic_latino'));
    }} else if (matchesPriorInternships(valueText, text)) {{
      setChoice(input, 'prior_internships');
    }} else if (matchesHeardAbout(valueText, text)) {{
      setChoice(input, 'heard_about');
    }} else if (matchesRoleInterest(valueText, text)) {{
      setChoice(input, 'role_interest');
    }} else if (matchesNoGithub(valueText, text)) {{
      setChoice(input, 'has_github');
    }} else if (matchesTermsAcceptance(valueText, text)) {{
      setChoice(input, 'terms_acceptance');
    }}
  }}

  for (const control of allElements('[role="checkbox"], [role="radio"], [aria-checked], span[checkbox-state], div[checkbox-state], button[aria-checked]')) {{
    if (!visible(control)) continue;
    if (control.matches?.('input[type="radio"], input[type="checkbox"]')) continue;
    if (isInsideAutocompletePopup(control)) continue;
    const groupText = closestQuestionText(control);
    const optionText = optionLabelText(control);
    const matchedField = customFieldMatch(optionText, groupText);
    if (matchedField) setChoiceControl(control, matchedField, choiceLockKey(matchedField, groupText, optionText));
  }}

  const genericChoiceSelector = [
    'label',
    'button',
    'span',
    'div',
    '[role="button"]',
    '[data-testid]',
    '[data-qa]',
    '[data-automation-id]'
  ].join(',');
  const clickedGenericTargets = new Set();
  for (const control of allElements(genericChoiceSelector)) {{
    if (!visible(control)) continue;
    if (control.matches?.('input, textarea, select, a')) continue;
    if (isInsideAutocompletePopup(control)) continue;
    const target = customChoiceTarget(control);
    if (!target || clickedGenericTargets.has(target)) continue;
    if (target.dataset.staticChoiceLocked === 'true' || target.closest?.('[data-static-choice-locked="true"]')) continue;
    const groupText = closestQuestionText(control);
    if (!groupText) continue;
    const optionText = rawCandidateOptionText(control, target, groupText);
    if (!optionText || optionText.length > 120) continue;
    const matchedField = customFieldMatch(optionText, groupText);
    if (matchedField === 'terms_acceptance') {{
      const accepted = allElements('input[type="checkbox"]').some((input) => {{
        if (!input.checked) return false;
        const acceptedGroup = closestQuestionText(input) || labelText(input);
        const acceptedOption = optionLabelText(input) || acceptedGroup;
        return matchesTermsAcceptance(acceptedOption, acceptedGroup);
      }});
      if (accepted) {{
        target.dataset.staticChoiceLocked = 'true';
        clickedGenericTargets.add(target);
        continue;
      }}
    }}
    if (matchedField && setChoiceControl(target, matchedField, choiceLockKey(matchedField, groupText, optionText))) clickedGenericTargets.add(target);
  }}

  async function acceptSuccessFactorsTerms() {{
    const page = norm(`${{location.href}} ${{document.title}} ${{document.body?.innerText || ''}}`);
    const successFactorsSite = /successfactors|career[0-9]*\\.successfactors|jobs[0-9]*\\.successfactors/i.test(location.hostname)
      || has(page, ['career opportunities create an account', 'sap successfactors']);
    if (!successFactorsSite) return false;
    let changed = false;
    const acceptButtons = allElements('button, input[type="button"], input[type="submit"], [role="button"]')
      .filter(visible)
      .map((el) => ({{ el, text: norm([el.innerText, el.textContent, el.value, el.getAttribute?.('aria-label'), el.getAttribute?.('title'), el.getAttribute?.('name'), el.getAttribute?.('id'), el.getAttribute?.('class')].filter(Boolean).join(' ')) }}))
      .filter((item) => item.text && has(item.text, ['accept']) && !has(item.text, ['decline', 'reject', 'do not', 'print']));
    const privacyAccept = acceptButtons.find((item) => {{
      const dialogText = norm(item.el.closest?.('[role="dialog"], .fd-dialog, .dialogBoxWrapper, [class*="dialog"], [id*="dialog"]')?.innerText || document.body?.innerText || '');
      return has(dialogText, ['data privacy', 'privacy consent', 'job applicant privacy', 'terms of use']);
    }});
    if (privacyAccept) {{
      clickLikeHuman(privacyAccept.el);
      changed = true;
      await sleep(500);
    }}

    if (!privacyAccept) {{
      const linkCandidates = allElements('a, button, [role="button"], [role="link"]')
        .filter(visible)
        .map((el) => ({{ el, text: norm([el.innerText, el.textContent, el.getAttribute?.('aria-label'), el.getAttribute?.('title')].filter(Boolean).join(' ')) }}))
        .filter((item) => item.text && has(item.text, ['data privacy agreement', 'privacy agreement', 'terms of use', 'terms and conditions']))
        .filter((item) => !has(item.text, ['create account', 'sign in', 'forgot password', 'show password']));
      for (const item of linkCandidates.slice(0, 1)) {{
        if (item.el.getAttribute('data-static-terms-opened') === 'true') continue;
        item.el.setAttribute('data-static-terms-opened', 'true');
        clickLikeHuman(item.el);
        changed = true;
        await sleep(400);
      }}
    }}

    const termControls = allElements('input[type="checkbox"], [role="checkbox"], [aria-checked], span[checkbox-state], div[checkbox-state], button[aria-checked]')
      .filter(visible);
    for (const control of termControls) {{
      const groupText = closestQuestionText(control) || labelText(control);
      const optionText = optionLabelText(control) || groupText;
      if (!matchesTermsAcceptance(optionText, groupText)) continue;
      if (control.matches?.('input[type="checkbox"]')) {{
        if (setChoice(control, 'terms_acceptance', choiceLockKey('terms_acceptance', groupText, optionText))) changed = true;
      }} else if (setChoiceControl(control, 'terms_acceptance', choiceLockKey('terms_acceptance', groupText, optionText))) {{
        changed = true;
      }}
    }}
    return changed;
  }}

  await acceptSuccessFactorsTerms();

  function collectVisibleErrors() {{
    const errorSelector = [
      '[role="alert"]',
      '[aria-live]',
      '[aria-invalid="true"]',
      '[data-automation-id*="error"]',
      '[data-testid*="error"]',
      '[data-qa*="error"]',
      '.error',
      '.field-error',
      '.form-error',
      '.invalid-feedback',
      '.validation-message',
      '[class*="Error"]',
      '[class*="error"]',
      '[id*="error"]'
    ].join(',');
    const errorWords = [
      'required', 'invalid', 'error', 'must', 'please enter', 'please select',
      'not recognized', 'select a valid', 'missing', 'wrong email', 'wrong password',
      'incorrect email', 'incorrect password', 'invalid email or password',
      'wrong email or password', 'couldn t find', 'could not find'
    ];
    const credentialWords = [
      'wrong email or password',
      'incorrect email or password',
      'invalid email or password',
      'wrong password',
      'incorrect password',
      'invalid password',
      'wrong email'
    ];
    function isBenignAssistiveText(lowered) {{
      if (/\\b\\d+\\s+results?\\s+available\\b/.test(lowered) && has(lowered, ['press', 'navigate', 'select'])) return true;
      if (/\\b\\d+\\s+options?\\s+available\\b/.test(lowered) && has(lowered, ['press', 'navigate', 'select'])) return true;
      if (has(lowered, ['use up and down arrow', 'press up and down arrow'])) return true;
      return false;
    }}
    for (const el of allElements(errorSelector)) {{
      if (!visible(el)) continue;
      const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
      if (text.length < 3 || text.length > 500) continue;
      const lowered = norm(text);
      if (isBenignAssistiveText(lowered)) continue;
      if (!has(lowered, errorWords)) continue;
      el.dataset.hybridVisibleError = 'true';
      pushLimited(result.visibleErrors, text);
      if (has(lowered, credentialWords)) result.credentialError = true;
    }}
    const pageText = norm(document.body?.innerText || '');
    if (has(pageText, credentialWords)) result.credentialError = true;
  }}

  collectVisibleErrors();

  // Hand the agent exactly one field at a time, in document order. Every
  // unresolved control below the topmost one is taken out of browser_use's
  // interactive-element index, so bouncing between fields is impossible rather
  // than merely discouraged by the prompt. Deferral is reversed as soon as the
  // field above is answered or abandoned, so each field gets its turn.
  function gateUnresolvedFieldsToDocumentOrder() {{
    const unresolved = allElements('input, select, textarea, [role="combobox"], [contenteditable="true"]')
      .filter((el) => visible(el)
        && !el.disabled
        && !el.readOnly
        && !isAbandoned(el)
        && el.dataset.staticAutocompleteSelected === undefined
        // A control static autofill deliberately passed over (an optional
        // search box, a phone extension) must not hold the single active slot
        // and stall every real question below it.
        && el.dataset.staticAutofillSkipped === undefined
        // File inputs read as permanently empty and are driven by the separate
        // resume-upload step, not by the agent.
        && norm(el.type) !== 'file'
        && norm(el.type) !== 'hidden'
        && isEffectivelyEmpty(el));
    unresolved.forEach((el, index) => {{
      if (index === 0) {{
        if (el.dataset.staticDeferred === 'true') {{
          delete el.dataset.staticDeferred;
          try {{
            el.style.pointerEvents = '';
            el.removeAttribute('aria-disabled');
            el.removeAttribute('tabindex');
          }} catch (_) {{}}
        }}
        return;
      }}
      if (el.dataset.staticDeferred === 'true') return;
      el.dataset.staticDeferred = 'true';
      try {{
        el.style.pointerEvents = 'none';
        el.setAttribute('aria-disabled', 'true');
        el.setAttribute('tabindex', '-1');
      }} catch (_) {{}}
    }});
  }}

  gateUnresolvedFieldsToDocumentOrder();

  return result;
}})();
"""


def _submit_guard_script() -> str:
    return r"""
(() => {
  const result = { installed: false, blocked: null, disabled: 0 };
  const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const hasAny = (text, words) => words.some((word) => text.includes(word));
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && rect.width > 1
      && rect.height > 1;
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
  const labelOf = (el) => {
    if (!el) return '';
    return norm([
      el.innerText,
      el.textContent,
      el.value,
      el.getAttribute?.('aria-label'),
      el.getAttribute?.('title'),
      el.getAttribute?.('name'),
      el.getAttribute?.('id'),
    ].filter(Boolean).join(' '));
  };
  const looksLikeApplicationPage = () => /\/apply\b|step=application|application/i.test(location.href);
  const looksLikeJobApplicationSurface = () => {
    const page = norm(document.body?.innerText || '');
    const fields = allElements('input, textarea, select');
    const hasActualForm = fields.filter((el) => visible(el)).length >= 2
      || fields.some((el) => norm(el.getAttribute?.('type')) === 'file');
    return (looksLikeApplicationPage() && hasActualForm)
      || (location.href.includes('ycombinator.com/companies') && hasAny(page, ['send message']))
      || (hasActualForm && hasAny(page, ['submit application', 'final review']));
  };
  const isDangerousSubmitLabel = (label) => {
    if (!label) return false;
    if (/^\s*apply\s*$/.test(label) && looksLikeJobApplicationSurface()) return true;
    if (/^\s*apply now\s*$/.test(label) && looksLikeJobApplicationSurface()) return true;
    if (/\bsend message\b/.test(label) && looksLikeJobApplicationSurface()) return true;
    if (/\bsubmit\b/.test(label)) return true;
    if (/\bsend\b/.test(label) && /\b(application|resume|profile)\b/.test(label)) return true;
    if (/\bfinish\b/.test(label) && /\b(application|apply|submission|profile)?\b/.test(label)) return true;
    if (/\bcomplete\b/.test(label) && /\b(application|apply|submission|profile)\b/.test(label)) return true;
    if (/\bfinal\b/.test(label) && /\b(apply|application|submit|submission)\b/.test(label)) return true;
    return false;
  };
  const recordBlock = (kind, el, label) => {
    const blocked = {
      kind,
      label,
      url: location.href,
      at: new Date().toISOString(),
      tag: el?.tagName || '',
      type: el?.getAttribute?.('type') || '',
    };
    window.__NO_FINAL_SUBMIT_GUARD = window.__NO_FINAL_SUBMIT_GUARD || {};
    window.__NO_FINAL_SUBMIT_GUARD.lastBlocked = blocked;
    return blocked;
  };
  const finalSubmitAllowed = () => {
    let releasedForReview = !!window.__NO_FINAL_SUBMIT_GUARD?.released;
    let pausedForUser = !!window.__LANGHIRE_PAUSE_CONTROL?.paused;
    try { releasedForReview ||= sessionStorage.getItem('__langhireReviewReleased') === 'true'; } catch (_) {}
    try { pausedForUser ||= sessionStorage.getItem('__langhireAiPaused') === 'true'; } catch (_) {}
    return releasedForReview || pausedForUser
      || Number(window.__NO_FINAL_SUBMIT_GUARD?.allowUntil || 0) > Date.now();
  };
  const protectDangerousControls = () => {
    if (finalSubmitAllowed()) return 0;
    let disabled = 0;
    const controls = allElements('button, input[type="submit"], input[type="button"], a, [role="button"]');
    for (const el of controls) {
      const label = labelOf(el);
      if (!isDangerousSubmitLabel(label)) {
        if (el.dataset.hybridFinalSubmitBlocked === 'true') {
          delete el.dataset.hybridFinalSubmitBlocked;
          if (el.getAttribute('title') === 'Blocked by LangHire dry-run final-submit guard') el.removeAttribute('title');
          if (el.getAttribute('aria-disabled') === 'true') el.removeAttribute('aria-disabled');
          if (el.style.pointerEvents === 'none') el.style.pointerEvents = '';
          if ('disabled' in el) el.disabled = false;
          if (el.tagName === 'A' && el.dataset.originalHref && !el.href) el.href = el.dataset.originalHref;
        }
        continue;
      }
      if (el.dataset.hybridFinalSubmitBlocked === 'true') {
        disabled += 1;
        continue;
      }
      el.dataset.hybridFinalSubmitBlocked = 'true';
      if (el.getAttribute('aria-disabled') !== 'true') el.setAttribute('aria-disabled', 'true');
      if (!el.getAttribute('title')) el.setAttribute('title', 'Blocked by LangHire dry-run final-submit guard');
      if (el.style.pointerEvents !== 'none') el.style.pointerEvents = 'none';
      if ('disabled' in el && !el.disabled) el.disabled = true;
      if (el.tagName === 'A' && el.href) {
        el.dataset.originalHref = el.dataset.originalHref || el.href;
        el.removeAttribute('href');
      }
      disabled += 1;
    }
    return disabled;
  };
  if (!window.__NO_FINAL_SUBMIT_GUARD?.installed) {
    window.__NO_FINAL_SUBMIT_GUARD = { installed: true, lastBlocked: null };
    const blockPointerEvent = (event) => {
      if (finalSubmitAllowed()) return;
      const target = event.target?.closest?.('button, input[type="submit"], input[type="button"], a, [role="button"]');
      if (!target) return;
      const label = labelOf(target);
      if (!isDangerousSubmitLabel(label)) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      recordBlock(event.type || 'click', target, label);
    };
    for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click', 'keydown']) {
      document.addEventListener(eventName, blockPointerEvent, true);
    }
    document.addEventListener('submit', (event) => {
      if (finalSubmitAllowed()) return;
      const submitter = event.submitter;
      const label = labelOf(submitter);
      if (!isDangerousSubmitLabel(label)) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      recordBlock('submit', submitter, label);
    }, true);
    try {
      window.__NO_FINAL_SUBMIT_GUARD.observer = new MutationObserver(() => protectDangerousControls());
      window.__NO_FINAL_SUBMIT_GUARD.observer.observe(document.documentElement, {
        childList: true,
        subtree: true
      });
    } catch (_) {}
    result.installed = true;
  }
  result.disabled = protectDangerousControls();
  result.blocked = window.__NO_FINAL_SUBMIT_GUARD?.lastBlocked || null;
  return result;
})();
"""


def _release_review_handoff_script() -> str:
    """Restore normal page interaction when automation hands control to a human."""
    return r"""
(() => {
  const result = { controls: 0, finalSubmits: 0, observer: false };
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

  try { sessionStorage.setItem('__langhireReviewReleased', 'true'); } catch (_) {}
  window.__STATIC_AUTOFILL_LOCKS = {};
  window.__STATIC_CHOICE_LOCKS = {};
  if (window.__STATIC_CHOICE_CLICK_GUARD) window.__STATIC_CHOICE_CLICK_GUARD.bypass = true;

  const automated = allElements(
    '[data-static-autofilled], [data-static-choice-locked="true"], ' +
    '[data-static-deferred="true"], [data-static-abandoned="true"]'
  );
  for (const el of automated) {
    delete el.dataset.staticAutofilled;
    delete el.dataset.staticChoiceLocked;
    delete el.dataset.staticDeferred;
    delete el.dataset.staticAbandoned;
    delete el.dataset.staticUnresolvedPasses;
    delete el.dataset.hybridNeedsLlm;
    delete el.dataset.hybridNeedsLlmReason;
    delete el.dataset.hybridRequiredEmpty;
    try {
      if (el.style.pointerEvents === 'none') el.style.pointerEvents = '';
      if (el.getAttribute('aria-disabled') === 'true') el.removeAttribute('aria-disabled');
      if (el.getAttribute('tabindex') === '-1') el.removeAttribute('tabindex');
    } catch (_) {}
    result.controls += 1;
  }

  const guard = window.__NO_FINAL_SUBMIT_GUARD = window.__NO_FINAL_SUBMIT_GUARD || {};
  guard.released = true;
  guard.allowUntil = Number.MAX_SAFE_INTEGER;
  try {
    guard.observer?.disconnect?.();
    result.observer = true;
  } catch (_) {}

  for (const el of allElements('[data-hybrid-final-submit-blocked="true"]')) {
    delete el.dataset.hybridFinalSubmitBlocked;
    if (el.getAttribute('title') === 'Blocked by LangHire dry-run final-submit guard') el.removeAttribute('title');
    if (el.getAttribute('aria-disabled') === 'true') el.removeAttribute('aria-disabled');
    if (el.style.pointerEvents === 'none') el.style.pointerEvents = '';
    if ('disabled' in el) el.disabled = false;
    if (el.tagName === 'A' && el.dataset.originalHref && !el.href) el.href = el.dataset.originalHref;
    result.finalSubmits += 1;
  }
  document.getElementById('__langhire-human-checkpoint')?.remove?.();
  window.__LANGHIRE_PAUSE_CONTROL?.host?.remove?.();
  try { sessionStorage.removeItem('__langhireAiPaused'); } catch (_) {}
  return result;
})();
"""


def _pause_control_overlay_script(force_paused: bool | None = None) -> str:
    """Install the in-page Pause/Resume control used during AI cleanup."""
    forced = "null" if force_paused is None else ("true" if force_paused else "false")
    return rf"""
(() => {{
  const forced = {forced};
  const storageKey = '__langhireAiPaused';
  const allElements = (selector, root = document, seen = new Set()) => {{
    const out = [];
    if (!root || seen.has(root)) return out;
    seen.add(root);
    try {{ out.push(...Array.from(root.querySelectorAll(selector))); }} catch (_) {{}}
    let nodes = [];
    try {{ nodes = Array.from(root.querySelectorAll('*')); }} catch (_) {{}}
    for (const node of nodes) if (node.shadowRoot) out.push(...allElements(selector, node.shadowRoot, seen));
    return out;
  }};
  const stored = () => {{
    try {{ return sessionStorage.getItem(storageKey) === 'true'; }} catch (_) {{ return false; }}
  }};
  const persist = (value) => {{
    try {{ sessionStorage.setItem(storageKey, value ? 'true' : 'false'); }} catch (_) {{}}
  }};

  let control = window.__LANGHIRE_PAUSE_CONTROL;
  if (!control) {{
    control = window.__LANGHIRE_PAUSE_CONTROL = {{
      paused: stored(),
      userChanged: false,
      updatedAt: Date.now(),
      host: null,
      panel: null,
      button: null,
      status: null,
    }};

    const unlockForUser = () => {{
      if (window.__STATIC_CHOICE_CLICK_GUARD) window.__STATIC_CHOICE_CLICK_GUARD.bypass = true;
      for (const el of allElements(
        '[data-static-autofilled], [data-static-choice-locked="true"], ' +
        '[data-static-deferred="true"], [data-static-abandoned="true"]'
      )) {{
        el.dataset.langhirePauseUnlocked = 'true';
        if (el.style.pointerEvents === 'none') el.style.pointerEvents = '';
        if (el.getAttribute('aria-disabled') === 'true') el.removeAttribute('aria-disabled');
        if (el.getAttribute('tabindex') === '-1') el.removeAttribute('tabindex');
      }}
      for (const el of allElements('[data-hybrid-final-submit-blocked="true"]')) {{
        el.dataset.langhirePauseFinalSubmit = 'true';
        delete el.dataset.hybridFinalSubmitBlocked;
        if (el.getAttribute('title') === 'Blocked by LangHire dry-run final-submit guard') el.removeAttribute('title');
        if (el.getAttribute('aria-disabled') === 'true') el.removeAttribute('aria-disabled');
        if (el.style.pointerEvents === 'none') el.style.pointerEvents = '';
        if ('disabled' in el) el.disabled = false;
        if (el.tagName === 'A' && el.dataset.originalHref && !el.href) el.href = el.dataset.originalHref;
      }}
    }};
    const prepareForAutomation = () => {{
      if (window.__STATIC_CHOICE_CLICK_GUARD) window.__STATIC_CHOICE_CLICK_GUARD.bypass = false;
      for (const el of allElements('[data-langhire-pause-unlocked="true"]')) {{
        delete el.dataset.langhirePauseUnlocked;
        if (el.matches(
          '[data-static-autofilled], [data-static-choice-locked="true"], ' +
          '[data-static-deferred="true"], [data-static-abandoned="true"]'
        )) {{
          el.style.pointerEvents = 'none';
          el.setAttribute('aria-disabled', 'true');
          el.setAttribute('tabindex', '-1');
        }}
      }}
      for (const el of allElements('[data-langhire-pause-final-submit="true"]')) {{
        delete el.dataset.langhirePauseFinalSubmit;
      }}
    }};
    const render = () => {{
      if (control.button) control.button.textContent = control.paused ? 'Resume AI' : 'Pause AI';
      if (control.status) {{
        control.status.textContent = control.paused ? 'AI paused — page is yours' : 'AI is working';
        control.status.style.color = control.paused ? '#166534' : '#475569';
      }}
      if (control.panel) {{
        control.panel.style.background = control.paused ? 'rgba(240,253,244,.99)' : 'rgba(255,255,255,.98)';
        control.panel.style.borderColor = control.paused ? '#16a34a' : '#94a3b8';
        control.panel.style.boxShadow = control.paused
          ? '0 10px 35px rgba(22,163,74,.35)'
          : '0 8px 28px rgba(15,23,42,.25)';
      }}
      if (control.button) control.button.style.background = control.paused ? '#15803d' : '#111827';
      if (control.host) control.host.style.opacity = '1';
    }};
    control.setPaused = (value, userChanged = false) => {{
      control.paused = !!value;
      control.userChanged = !!userChanged;
      control.updatedAt = Date.now();
      persist(control.paused);
      if (control.paused) unlockForUser();
      else prepareForAutomation();
      render();
      return control.snapshot();
    }};
    control.snapshot = () => ({{
      installed: true,
      paused: !!control.paused,
      userChanged: !!control.userChanged,
      updatedAt: Number(control.updatedAt || 0),
      url: location.href,
    }});

    const mount = () => {{
      if (control.host?.isConnected || !document.documentElement) return;
      const host = document.createElement('div');
      host.id = '__langhire-ai-control';
      host.setAttribute('data-langhire-overlay', 'true');
      host.style.cssText = 'position:fixed;left:50%;top:16px;transform:translateX(-50%);z-index:2147483647;opacity:0;';
      const shadow = host.attachShadow({{ mode: 'closed' }});
      const panel = document.createElement('div');
      panel.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 12px;background:rgba(255,255,255,.97);border:1px solid #cbd5e1;border-radius:12px;box-shadow:0 8px 28px rgba(15,23,42,.25);font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#0f172a;';
      const status = document.createElement('span');
      status.style.cssText = 'white-space:nowrap;font-weight:600;';
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('aria-label', 'LangHire pause or resume AI');
      button.style.cssText = 'appearance:none;border:0;border-radius:8px;background:#111827;color:white;padding:8px 12px;font:600 13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer;';
      button.addEventListener('pointerdown', (event) => {{ event.stopPropagation(); }}, true);
      button.addEventListener('click', (event) => {{
        event.preventDefault();
        event.stopPropagation();
        control.setPaused(!control.paused, true);
      }}, true);
      panel.append(status, button);
      shadow.append(panel);
      document.documentElement.append(host);
      control.host = host;
      control.panel = panel;
      control.button = button;
      control.status = status;
      render();
    }};
    mount();
    if (!control.host) document.addEventListener('DOMContentLoaded', mount, {{ once: true }});
    control.setPaused(control.paused, false);
  }}
  if (forced !== null) control.setPaused(forced, false);
  return control.snapshot();
}})();
"""


def _controlled_final_submit_script() -> str:
    return r"""
(() => {
  const result = {
    submitted: false,
    reason: '',
    label: '',
    requiredEmpty: [],
    invalidFields: [],
    visibleErrors: [],
    url: location.href
  };
  const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const hasAny = (text, words) => words.some((word) => text.includes(word));
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && rect.width > 1
      && rect.height > 1;
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
  const labelOf = (el) => norm([
    el?.innerText,
    el?.textContent,
    el?.value,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('title'),
    el?.getAttribute?.('name'),
    el?.getAttribute?.('id'),
  ].filter(Boolean).join(' '));
  const fieldLabel = (el) => clean([
    el?.labels?.[0]?.innerText,
    el?.closest?.('label')?.innerText,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('placeholder'),
    el?.getAttribute?.('name'),
    el?.id,
  ].filter(Boolean).join(' ')).slice(0, 160);
  const looksLikeApplicationPage = () => /\/apply\b|step=application|application/i.test(location.href);
  const looksLikeJobApplicationSurface = () => {
    const page = norm(document.body?.innerText || '');
    return looksLikeApplicationPage()
      || (location.href.includes('ycombinator.com/companies') && hasAny(page, ['send message']))
      || (hasAny(page, ['upload resume', 'resume']) && hasAny(page, ['linkedin', 'authorized to work', 'sponsorship']));
  };
  const isDangerousSubmitLabel = (label) => {
    if (!label) return false;
    if (/^\s*apply\s*$/.test(label) && looksLikeJobApplicationSurface()) return true;
    if (/^\s*apply now\s*$/.test(label) && looksLikeJobApplicationSurface()) return true;
    if (/\bsend message\b/.test(label) && looksLikeJobApplicationSurface()) return true;
    if (/\bsubmit\b/.test(label)) return true;
    if (/\bsend\b/.test(label) && /\b(application|resume|profile)\b/.test(label)) return true;
    if (/\bfinish\b/.test(label) && /\b(application|apply|submission|profile)?\b/.test(label)) return true;
    if (/\bcomplete\b/.test(label) && /\b(application|apply|submission|profile)\b/.test(label)) return true;
    if (/\bfinal\b/.test(label) && /\b(apply|application|submit|submission)\b/.test(label)) return true;
    return false;
  };
  const isEmptyControl = (el) => {
    if (!visible(el) || el.disabled || el.readOnly) return false;
    const tag = el.tagName;
    const type = norm(el.getAttribute('type') || '');
    if (type === 'hidden' || type === 'button' || type === 'submit') return false;
    if (type === 'checkbox' || type === 'radio') {
      if (!el.required && el.getAttribute('aria-required') !== 'true') return false;
      const name = el.getAttribute('name');
      const group = name ? allElements(`input[name="${CSS.escape(name)}"]`) : [el];
      return !group.some((item) => item.checked);
    }
    if (tag === 'SELECT') return !el.value || el.selectedIndex < 0;
    if (type === 'file') return !el.files || el.files.length === 0;
    return clean(el.value || '') === '';
  };
  for (const el of allElements('input, textarea, select')) {
    if (!visible(el) || el.disabled || el.readOnly) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true' || el.dataset.hybridRequiredEmpty === 'true';
    if (required && isEmptyControl(el)) result.requiredEmpty.push(fieldLabel(el) || el.tagName);
    if (el.getAttribute('aria-invalid') === 'true' || el.dataset.hybridInvalidField === 'true') {
      result.invalidFields.push(fieldLabel(el) || el.tagName);
    }
  }
  const errorSelector = [
    '[role="alert"]',
    '[aria-live]',
    '.error',
    '.errors',
    '.field-error',
    '.validation-error',
    '[class*="error"]',
    '[class*="Error"]',
    '[data-testid*="error"]',
    '[data-qa*="error"]'
  ].join(',');
  const errorWords = ['required', 'invalid', 'missing', 'select', 'choose', 'upload', 'enter', 'error', 'not recognized', 'must'];
  const isBenignAssistiveText = (lowered) => {
    if (/\b\d+\s+results?\s+available\b/.test(lowered) && ['press', 'navigate', 'select'].some((word) => lowered.includes(word))) return true;
    if (/\b\d+\s+options?\s+available\b/.test(lowered) && ['press', 'navigate', 'select'].some((word) => lowered.includes(word))) return true;
    if (lowered.includes('use up and down arrow') || lowered.includes('press up and down arrow')) return true;
    return false;
  };
  for (const el of allElements(errorSelector)) {
    if (!visible(el)) continue;
    const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
    if (text.length < 3 || text.length > 500) continue;
    const lowered = norm(text);
    if (isBenignAssistiveText(lowered)) continue;
    if (!errorWords.some((word) => lowered.includes(word))) continue;
    if (!result.visibleErrors.includes(text)) result.visibleErrors.push(text);
  }
  result.requiredEmpty = [...new Set(result.requiredEmpty)].slice(0, 10);
  result.invalidFields = [...new Set(result.invalidFields)].slice(0, 10);
  result.visibleErrors = [...new Set(result.visibleErrors)].slice(0, 10);
  if (result.requiredEmpty.length || result.invalidFields.length || result.visibleErrors.length) {
    result.reason = 'form_not_clean';
    return result;
  }
  const candidates = allElements('button, input[type="submit"], input[type="button"], a, [role="button"]')
    .filter((el) => {
      const label = labelOf(el);
      const tag = norm(el.tagName);
      const type = norm(el.getAttribute('type') || '');
      const nativeSubmit = looksLikeJobApplicationSurface() && (type === 'submit' || (tag === 'button' && !type));
      return visible(el) && (isDangerousSubmitLabel(label) || nativeSubmit);
    });
  const control = candidates[0];
  if (!control) {
    result.reason = 'no_final_submit_control_found';
    return result;
  }
  result.label = labelOf(control);
  window.__NO_FINAL_SUBMIT_GUARD = window.__NO_FINAL_SUBMIT_GUARD || {};
  window.__NO_FINAL_SUBMIT_GUARD.allowUntil = Date.now() + 5000;
  control.style.pointerEvents = '';
  control.removeAttribute('aria-disabled');
  if ('disabled' in control) control.disabled = false;
  if (control.tagName === 'A' && control.dataset.originalHref) control.href = control.dataset.originalHref;
  control.click();
  result.submitted = true;
  return result;
})();
"""


def _cookie_helper_script() -> str:
    return r"""
(() => {
  const result = { installed: false, clicked: false, blockedReject: null, label: null };
  const norm = (s) => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && rect.width > 1
      && rect.height > 1
      && !el.disabled;
  };
  const labelOf = (el) => norm([
    el?.innerText,
    el?.textContent,
    el?.value,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('title'),
    el?.getAttribute?.('name'),
    el?.getAttribute?.('id'),
  ].filter(Boolean).join(' '));
  const isCookieLabel = (label) => /\bcookie|consent|privacy preference|tracking\b/.test(label);
  const isReject = (label) => {
    if (!label) return false;
    return /\breject\b|\bdecline\b|\bdeny\b|\brefuse\b|\bdisagree\b/.test(label)
      && (isCookieLabel(label) || /\ball\b|\boptional\b/.test(label));
  };
  const isAccept = (label) => {
    if (!label || isReject(label)) return false;
    if (/\bmanage\b|\bpreference\b|\bsettings\b|\bcustomi[sz]e\b|\bnecessary\b|\bmore options\b/.test(label)) return false;
    return /\baccept\b|\bagree\b|\ballow all\b|\baccept all\b|\bok\b|\bgot it\b/.test(label)
      && (isCookieLabel(label) || /\b(all|cookies|consent)\b/.test(label));
  };
  if (!window.__COOKIE_HELPER_GUARD?.installed) {
    window.__COOKIE_HELPER_GUARD = { installed: true, lastBlockedReject: null };
    document.addEventListener('click', (event) => {
      const target = event.target?.closest?.('button, input[type="submit"], input[type="button"], a, [role="button"]');
      if (!target) return;
      const label = labelOf(target);
      if (!isReject(label)) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      window.__COOKIE_HELPER_GUARD.lastBlockedReject = {
        label,
        url: location.href,
        at: new Date().toISOString(),
      };
    }, true);
    result.installed = true;
  }
  const candidates = Array.from(document.querySelectorAll(
    'button, input[type="button"], input[type="submit"], a, [role="button"]'
  )).filter(visible);
  const accept = candidates.find((el) => isAccept(labelOf(el)));
  if (accept) {
    const label = labelOf(accept);
    try {
      accept.click();
      result.clicked = true;
      result.label = label;
    } catch (err) {
      result.error = String(err).slice(0, 200);
    }
  }
  result.blockedReject = window.__COOKIE_HELPER_GUARD?.lastBlockedReject || null;
  return result;
})();
"""


def _workday_human_checkpoint_script() -> str:
    """Return a probe that pauses on Workday's protected account action.

    Workday puts a click-filter/noCaptcha surface over the Create Account and
    Sign In submit controls.  Static autofill can prepare the account form, but
    the protected click is deliberately left to the candidate.
    """
    return r"""
(() => {
  const BANNER_ID = 'langhire-human-checkpoint';
  const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  const norm = (text) => clean(text).toLowerCase();
  const removeBanner = () => document.getElementById(BANNER_ID)?.remove();
  const finish = (reason, extra = {}) => {
    if (!extra.required) removeBanner();
    return {
      required: false,
      reason,
      action: '',
      key: '',
      url: location.href,
      title: document.title || '',
      ...extra,
    };
  };
  const host = norm(location.hostname);
  const isWorkday = /(^|\.)myworkdayjobs\.com$/.test(host)
    || /(^|\.)myworkdaysite\.com$/.test(host)
    || /(^|\.)workday\.com$/.test(host);
  if (!isWorkday) return finish('not_workday');

  const rendered = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && rect.width > 2
      && rect.height > 2;
  };
  const visible = (el) => rendered(el) && Number(getComputedStyle(el).opacity || 1) > 0;
  const automation = (el) => norm(el?.getAttribute?.('data-automation-id'));
  const labelOf = (el) => clean([
    el?.innerText,
    el?.textContent,
    el?.value,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('title'),
    el?.getAttribute?.('name'),
    el?.getAttribute?.('id'),
    el?.getAttribute?.('data-automation-id'),
  ].filter(Boolean).join(' '));

  const createButton = document.querySelector(
    'button[data-automation-id="createAccountSubmitButton"], input[data-automation-id="createAccountSubmitButton"]'
  );
  const signInButton = document.querySelector(
    'button[data-automation-id="signInSubmitButton"], input[data-automation-id="signInSubmitButton"]'
  );
  const filters = Array.from(document.querySelectorAll('[data-automation-id="click_filter"]'))
    .filter(rendered);
  const createFilter = filters.find((el) => norm(labelOf(el)) === 'create account'
    || norm(el.getAttribute('aria-label')) === 'create account');
  const signInFilter = filters.find((el) => ['sign in', 'log in'].includes(norm(labelOf(el)))
    || ['sign in', 'log in'].includes(norm(el.getAttribute('aria-label'))));

  let action = '';
  let button = null;
  let clickSurface = null;
  if (createButton && (visible(createButton) || createFilter)) {
    action = 'Create Account';
    button = createButton;
    clickSurface = createFilter || createButton;
  } else if (signInButton && (visible(signInButton) || signInFilter)) {
    action = 'Sign In';
    button = signInButton;
    clickSurface = signInFilter || signInButton;
  }
  // Navigation links with the same labels are intentionally ignored.  This
  // checkpoint applies only to the actual account-form submit controls.
  if (!button || !clickSurface) return finish('account_action_absent');

  const protectedSurface = button.closest?.('[data-automation-id="noCaptchaWrapper"]')
    || clickSurface.closest?.('[data-automation-id="noCaptchaWrapper"]')
    || button.parentElement?.closest?.('[data-automation-id="noCaptchaWrapper"]')
    || clickSurface.parentElement?.closest?.('[data-automation-id="noCaptchaWrapper"]');
  if (!protectedSurface && !filters.includes(clickSurface)) {
    return finish('account_action_not_protected');
  }

  const form = button.form || button.closest?.('form') || clickSurface.closest?.('form');
  if (!form) return finish('account_form_absent');

  const errorNodes = Array.from(form.querySelectorAll(
    '[role="alert"], [aria-live="assertive"], [data-automation-id*="error" i], [data-automation-id="errorMessage"]'
  )).filter(visible);
  const visibleErrors = [];
  for (const node of errorNodes) {
    const text = clean(node.innerText || node.textContent || node.getAttribute?.('aria-label'));
    if (!text || visibleErrors.some((known) => known === text || known.includes(text))) continue;
    visibleErrors.push(text.slice(0, 240));
  }
  if (visibleErrors.length) {
    return finish('visible_error', { visibleErrors: visibleErrors.slice(0, 4) });
  }

  const fields = Array.from(form.querySelectorAll('input, textarea, select'))
    .filter((el) => !el.disabled)
    .filter((el) => {
      const type = norm(el.getAttribute?.('type') || el.tagName);
      return !['hidden', 'button', 'submit', 'reset', 'image', 'file', 'checkbox', 'radio'].includes(type)
        && visible(el);
    });
  const fieldText = (el) => norm([
    el.labels?.[0]?.innerText,
    el.closest?.('label')?.innerText,
    el.getAttribute?.('aria-label'),
    el.getAttribute?.('placeholder'),
    el.getAttribute?.('autocomplete'),
    el.getAttribute?.('name'),
    el.getAttribute?.('id'),
    el.getAttribute?.('data-automation-id'),
  ].filter(Boolean).join(' '));
  const emailFields = fields.filter((el) => {
    const type = norm(el.getAttribute?.('type'));
    const text = fieldText(el);
    return type === 'email' || /(^|\b)(email|e mail|username|user name)(\b|$)/.test(text);
  });
  const passwordFields = fields.filter((el) => norm(el.getAttribute?.('type')) === 'password'
    || /(^|\b)(password|passwd|pwd)(\b|$)/.test(fieldText(el)));
  const requiredFields = fields.filter((el) => el.required || el.getAttribute?.('aria-required') === 'true');
  const missing = [];
  if (!emailFields.length) missing.push('email');
  if (!passwordFields.length) missing.push('password');
  for (const el of [...emailFields, ...passwordFields, ...requiredFields]) {
    if (clean(el.value)) continue;
    const label = fieldText(el) || norm(el.tagName);
    if (!missing.includes(label)) missing.push(label);
  }
  if (action === 'Create Account' && passwordFields.length > 1) {
    const values = passwordFields.map((el) => clean(el.value)).filter(Boolean);
    if (new Set(values).size > 1) missing.push('passwords do not match');
  }

  const choiceLabel = (el) => norm([
    el.labels?.[0]?.innerText,
    el.closest?.('label')?.innerText,
    el.parentElement?.innerText,
    el.innerText,
    el.textContent,
    el.getAttribute?.('aria-label'),
    el.getAttribute?.('name'),
    el.getAttribute?.('id'),
    el.getAttribute?.('data-automation-id'),
  ].filter(Boolean).join(' '));
  const checked = (el) => {
    const aria = norm(el.getAttribute?.('aria-checked'));
    const state = norm(el.getAttribute?.('checkbox-state')
      || el.getAttribute?.('data-state') || el.getAttribute?.('data-checked'));
    return !!el.checked || aria === 'true' || ['checked', 'true', 'selected'].includes(state);
  };
  const choices = Array.from(form.querySelectorAll(
    'input[type="checkbox"], [role="checkbox"], [aria-checked], span[checkbox-state], div[checkbox-state], button[aria-checked]'
  )).filter((el) => {
    const id = automation(el);
    const text = choiceLabel(el);
    return id === 'createaccountcheckbox'
      || /terms|privacy|candidate notice|applicant notice|agree|consent/.test(text);
  });
  const authoritativeChoices = choices.filter((choice) => choice.matches?.('input[type="checkbox"]'));
  for (const choice of authoritativeChoices.length ? authoritativeChoices : choices) {
    if (!checked(choice)) missing.push('terms/privacy checkbox');
  }
  if (missing.length) return finish('account_form_not_ready', { missing: [...new Set(missing)].slice(0, 8) });

  const banner = document.getElementById(BANNER_ID) || document.createElement('div');
  banner.id = BANNER_ID;
  banner.setAttribute('role', 'status');
  banner.setAttribute('aria-live', 'polite');
  banner.dataset.action = action;
  banner.textContent = `LangHire paused — click ${action} to continue`;
  banner.style.cssText = [
    'position:fixed',
    'right:18px',
    'bottom:18px',
    'z-index:2147483647',
    'max-width:320px',
    'padding:10px 13px',
    'border-radius:10px',
    'background:#111827',
    'color:#ffffff',
    'font:600 13px/1.35 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
    'box-shadow:0 8px 30px rgba(0,0,0,.28)',
    'pointer-events:none',
  ].join(';');
  if (!banner.isConnected) (document.body || document.documentElement).appendChild(banner);

  const key = `${action}:${location.origin}${location.pathname}${location.hash}`;
  return finish('workday_protected_account_action', {
    required: true,
    action,
    key,
    protectedBy: protectedSurface ? 'noCaptchaWrapper' : 'click_filter',
  });
})()
"""


def _safe_progress_step_script() -> str:
    return r"""
(() => {
  const result = {
    clicked: false,
    reason: '',
    label: '',
    url: location.href,
    accountish: false,
    applicationish: false,
    finalish: false,
    candidates: []
  };
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const norm = (s) => clean(s).toLowerCase();
  const hasAny = (text, words) => words.some((word) => text.includes(word));
  const visible = (el) => {
    if (!el || el.disabled || el.getAttribute?.('aria-disabled') === 'true') return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && Number(style.opacity || 1) > 0
      && rect.width > 3
      && rect.height > 3;
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
  const labelOf = (el) => clean([
    el?.innerText,
    el?.textContent,
    el?.value,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('title'),
    el?.getAttribute?.('name'),
    el?.getAttribute?.('id'),
    el?.getAttribute?.('data-automation-id'),
    el?.getAttribute?.('data-testid'),
    el?.getAttribute?.('href'),
  ].filter(Boolean).join(' '));
  const page = norm(document.body?.innerText || '');
  const url = norm(location.href);
  result.accountish = hasAny(page + ' ' + url, [
    'create account', 'sign up', 'signup', 'register', 'registration',
    'sign in', 'signin', 'log in', 'login', 'password', 'confirm password',
    'candidate home account', 'already have an account'
  ]);
  result.applicationish = /\/apply\b|application|candidateexperience|ashbyhq|greenhouse|lever|workday|oraclecloud|smartrecruiters|jobvite|icims|successfactors/i.test(location.href)
    || hasAny(page, ['upload resume', 'resume/cv', 'cover letter', 'voluntary self-identification', 'work authorization', 'sponsorship']);
  // This guard exists to stop us clicking "Apply" on a generic job-board
  // *listing* page before the right job has even been confirmed. It should
  // not also block the correct first Apply click on a Workday job-detail
  // page — this function is only ever called from inside a flow already
  // targeting one specific job (never a generic search crawl), and a
  // Workday job page showing this exact boilerplate (Job Requisition ID,
  // Time Type, sometimes an embedded recruiting video and a long marketing
  // description) before the real form loads is a normal, common shape, not
  // a listing page. Real observed failure: a Workday posting with a video
  // player and heavy marketing copy above the Apply button left the agent
  // waiting/scrolling for 9 steps because this exact check treated it as
  // "still just a posting" and refused the click that would have unstuck it.
  const isWorkdayHost = /myworkdayjobs\.com|myworkdaysite\.com/i.test(url);
  const jobPostingish = !isWorkdayHost
    && hasAny(page, ['job requisition id', 'posted on', 'job details', 'time type'])
    && !/\/apply\b|candidateexperience/i.test(location.href);

  const isFinalApplicationLabel = (label) => {
    const text = norm(label);
    if (!text) return false;
    if (/\bsubmit application\b/.test(text)) return true;
    if (/\bsubmit my application\b/.test(text)) return true;
    if (/\bsend application\b/.test(text)) return true;
    if (/\bsend my application\b/.test(text)) return true;
    if (/\bfinish application\b/.test(text)) return true;
    if (/\bcomplete application\b/.test(text)) return true;
    if (/\bfinal submit\b|\bsubmit final\b/.test(text)) return true;
    if (/^\s*submit\s*$/.test(text) && result.applicationish && !result.accountish) return true;
    if (/^\s*apply\s*$/.test(text) && result.applicationish && !jobPostingish) return true;
    if (/^\s*apply now\s*$/.test(text) && result.applicationish && !jobPostingish) return true;
    if (/\bsend message\b/.test(text) && url.includes('ycombinator.com/companies')) return true;
    return false;
  };
  const isBad = (label) => {
    const text = norm(label);
    if (!text) return true;
    if (isFinalApplicationLabel(text)) return true;
    return hasAny(text, [
      'forgot password', 'reset password', 'resend email', 'resend code',
      'withdraw', 'delete', 'remove', 'cancel', 'close', 'back', 'previous',
      'save job', 'saved', 'share', 'follow', 'report', 'reject cookies',
      'decline cookies', 'deny cookies', 'use my last application',
      'autofill with resume', 'resume autofill', 'mygreenhouse',
      'quick apply with'
    ]);
  };
  const candidates = allElements('button, input[type="button"], input[type="submit"], a, [role="button"], [role="link"]')
    .filter(visible)
    .map((el) => ({ el, label: labelOf(el), lower: norm(labelOf(el)) }))
    .filter((item) => item.lower && !isBad(item.label));
  result.candidates = candidates.slice(0, 18).map((item) => item.label.slice(0, 120));

  const fieldLabel = (el) => norm([
    el?.labels?.[0]?.innerText,
    el?.closest?.('label')?.innerText,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('placeholder'),
    el?.getAttribute?.('name'),
    el?.getAttribute?.('id'),
    el?.getAttribute?.('autocomplete'),
    el?.getAttribute?.('class'),
  ].filter(Boolean).join(' '));
  const accountFieldEmpty = () => {
    const fields = allElements('input, textarea, select')
      .filter(visible)
      .filter((el) => {
        const type = norm(el.getAttribute?.('type') || el.tagName);
        if (['hidden', 'button', 'submit', 'reset', 'image', 'file'].includes(type)) return false;
        const text = fieldLabel(el);
        const important = type === 'password'
          || type === 'email'
          || hasAny(text, [
            'email', 'e mail', 'username', 'user name', 'userid', 'user id',
            'password', 'passwd', 'pwd', 'confirm', 'retype', 'repeat',
            'first name', 'firstname', 'givenname', 'given name',
            'last name', 'lastname', 'familyname', 'family name', 'surname'
          ]);
        if (!important) return false;
        if (el.tagName === 'SELECT') return !el.value;
        return !clean(el.value);
      });
    const missing = fields.map((el) => fieldLabel(el).slice(0, 120));
    const choiceMissing = allElements('input[type="checkbox"], [role="checkbox"], [aria-checked], span[checkbox-state], div[checkbox-state], button[aria-checked]')
      .filter(visible)
      .filter((el) => {
        const label = norm([
          el?.labels?.[0]?.innerText,
          el?.closest?.('label')?.innerText,
          el?.innerText,
          el?.textContent,
          el?.getAttribute?.('aria-label'),
          el?.getAttribute?.('title'),
          el?.getAttribute?.('name'),
          el?.getAttribute?.('id'),
        ].filter(Boolean).join(' '));
        if (!hasAny(label, ['terms of use', 'privacy agreement', 'privacy policy', 'applicant privacy', 'candidate privacy', 'data privacy', 'terms and conditions', 'accept terms'])) return false;
        const aria = norm(el.getAttribute?.('aria-checked'));
        const state = norm(el.getAttribute?.('checkbox-state') || el.getAttribute?.('data-state') || el.getAttribute?.('data-checked'));
        const checked = !!el.checked || aria === 'true' || state === 'checked' || state === 'true' || state === 'selected';
        return !checked;
      })
      .map((el) => fieldLabel(el).slice(0, 120) || 'terms/privacy acceptance');
    return [...missing, ...choiceMissing].slice(0, 8);
  };

  const enabledValue = (el) => {
    if (el.dataset?.hybridFinalSubmitBlocked === 'true') delete el.dataset.hybridFinalSubmitBlocked;
    if (el.getAttribute?.('aria-disabled') === 'true') el.removeAttribute('aria-disabled');
    if ('disabled' in el) el.disabled = false;
    if (el.style) el.style.pointerEvents = '';
    if (el.tagName === 'A' && el.dataset?.originalHref && !el.href) el.href = el.dataset.originalHref;
  };
  const click = (item, reason) => {
    result.clicked = true;
    result.reason = reason;
    result.label = item.label.slice(0, 180);
    // Workday React controls ignore or race synthetic HTMLElement.click().
    // The Python wrapper follows these account/form reasons with one trusted
    // CDP mouse click, so do not fire a conflicting synthetic click first.
    if (['account_create_or_signup', 'account_or_form_continue', 'form_continue'].includes(reason)) {
      result.trusted_click_required = true;
      return result;
    }
    if (window.__NO_FINAL_SUBMIT_GUARD) window.__NO_FINAL_SUBMIT_GUARD.allowUntil = Date.now() + 3000;
    enabledValue(item.el);
    item.el.scrollIntoView({ block: 'center', inline: 'center' });
    item.el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
    item.el.click();
    return result;
  };
  const find = (patterns) => {
    for (const pattern of patterns) {
      const match = candidates.find((item) => pattern.test(item.label));
      if (match) return match;
    }
    return null;
  };

  const privacyAcceptButton = candidates.find((item) => {
    const lower = norm(item.label);
    const tag = norm(item.el.tagName);
    const name = norm(item.el.getAttribute?.('name') || item.el.getAttribute?.('id') || '');
    const buttonish = ['button', 'input'].includes(tag);
    if (!buttonish) return false;
    if (!lower.includes('accept') && !name.includes('accept')) return false;
    if (hasAny(lower, ['decline', 'reject', 'print', 'cookie'])) return false;
    const dialogText = norm(item.el.closest?.('[role="dialog"], .fd-dialog, .dialogBoxWrapper, [class*="dialog"], [id*="dialog"]')?.innerText || document.body?.innerText || '');
    return hasAny(dialogText, ['data privacy consent', 'job applicant privacy', 'privacy notice', 'terms of use']);
  });
  if (privacyAcceptButton) return click(privacyAcceptButton, 'privacy_or_terms_accept');

  const privacyOpenLink = candidates.find((item) => {
    const lower = norm(item.label);
    // Ordinary policy hyperlinks (for example "Privacy Policy and Terms of
    // Use") do not advance a Workday form.  Only open controls whose wording
    // explicitly asks the candidate to review/accept a privacy statement.
    if (!hasAny(lower, ['read and accept', 'review and accept', 'data privacy statement'])) return false;
    if (hasAny(lower, ['decline', 'reject', 'print', 'cookie'])) return false;
    return !['button', 'input'].includes(norm(item.el.tagName));
  });
  if (privacyOpenLink) return click(privacyOpenLink, 'privacy_or_terms_open');

  const pageTitle = norm(document.title);
  const manualApply = candidates.find((item) => /^apply manually\b/i.test(item.label));
  if (manualApply) return click(manualApply, 'start_or_guest_apply');

  const listingApply = jobPostingish
    && !hasAny(pageTitle, ['sign in', 'create account', 'register'])
    && candidates.find((item) => {
      const automation = norm(item.el.getAttribute?.('data-automation-id') || '');
      return (/^apply\b/.test(item.lower) && automation === 'adventurebutton')
        || /^apply(?:\s+apply)?\s*$/.test(item.lower);
    });
  if (listingApply) return click(listingApply, 'start_job_application');

  const createAccount = find([
    /\bcreate account\b/i,
    /\bcreate an account\b/i,
    /\bregister\b/i,
    /\bsign up\b/i,
    /\bsignup\b/i,
    /\bnew candidate\b/i,
    /\bcreate profile\b/i,
    /\bmake account\b/i
  ]);
  if (createAccount) {
    const createLabel = norm(createAccount.label);
    const isSubmitLikeCreate = /createaccountbutton|^create account$|create account create account/i.test(createAccount.label)
      || ['button', 'input'].includes(norm(createAccount.el.tagName));
    const empty = isSubmitLikeCreate && result.accountish ? accountFieldEmpty() : [];
    if (empty.length) {
      result.reason = 'account_fields_empty_before_create';
      result.empty_fields = empty;
      return result;
    }
    return click(createAccount, 'account_create_or_signup');
  }

  const startApply = find([
    /^\s*apply\s*$/i,
    /^\s*apply now\s*$/i,
    /\bapply manually\b/i,
    /\bmanual application\b/i,
    /\bapply as guest\b/i,
    /\bcontinue as guest\b/i,
    /\bcontinue without\b/i,
    /\bstart application\b/i,
    /\bbegin application\b/i,
    /\bcontinue to apply\b/i,
    /\bi'?m interested\b/i,
    /\bapply to role\b/i,
    /\bapply for this (job|position|role)\b/i
  ]);
  if (startApply) return click(startApply, 'start_or_guest_apply');

  const emailOption = find([
    /\bcontinue with email\b/i,
    /\bsign in with email\b/i,
    /\blog in with email\b/i,
    /\bemail me\b/i,
    /^email$/i
  ]);
  if (emailOption && result.accountish) return click(emailOption, 'email_auth_option');

  const forward = find([
    /\bsave and continue\b/i,
    /\bcontinue\b/i,
    /\bnext\b/i,
    /\bproceed\b/i,
    /\bstart\b/i,
    /\bbegin\b/i,
    /\blog in\b/i,
    /\bsign in\b/i
  ]);
  if (forward) {
    const empty = result.accountish ? accountFieldEmpty() : [];
    if (empty.length) {
      result.reason = 'account_fields_empty_before_continue';
      result.empty_fields = empty;
      return result;
    }
    return click(forward, result.accountish ? 'account_or_form_continue' : 'form_continue');
  }

  result.finalish = candidates.some((item) => isFinalApplicationLabel(item.label));
  result.reason = result.finalish ? 'final_submit_only' : 'no_safe_progress_control';
  return result;
})();
"""


async def _trusted_retype_static_inputs(cdp_session: Any, facts: dict[str, str]) -> tuple[int, int]:
    """Retype JS-filled text controls through CDP so React observes trusted input.

    Workday renders controlled React inputs whose DOM value can look correct
    even when React's internal form state is still empty. CDP's Input domain
    produces the browser-level input event that those controls expect.
    """
    queue_script = r"""
(() => {
  let sequence = Number(window.__STATIC_TRUSTED_RETYPE_SEQUENCE || 0);
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 2 && rect.height > 2 && !el.disabled && !el.readOnly;
  };
  const inferWorkdayField = (el) => {
    const identity = String(`${el.id || ''} ${el.name || ''}`).toLowerCase();
    if (identity.includes('schoolname')) return 'school';
    if (identity.includes('fieldofstudy')) return 'major';
    if (identity.includes('gradeaverage')) return 'gpa';
    if (identity.includes('firstyearattended')) return 'education_start_year';
    if (identity.includes('lastyearattended')) return 'graduation_year';
    if (identity.includes('linkedinaccount')) return 'linkedin_url';
    if (identity.includes('source--source')) return 'heard_about';
    return '';
  };
  const controls = Array.from(document.querySelectorAll('input, textarea')).filter((el) => {
    const type = String(el.type || 'text').toLowerCase();
    const field = el.dataset.staticAutofilled || inferWorkdayField(el);
    if (!field) return false;
    const selectedText = String(el.closest('[data-automation-id], [class]')?.innerText || el.parentElement?.innerText || '');
    const unresolvedChoice = ['heard_about', 'major'].includes(field)
      && !/\b[1-9]\d*\s+items?\s+selected\b/i.test(selectedText);
    return visible(el)
      && !['hidden', 'file', 'checkbox', 'radio', 'button', 'submit', 'reset', 'date'].includes(type)
      && (el.dataset.staticTrustedTyped !== 'true' || unresolvedChoice);
  });
  const items = controls.map((el) => {
    sequence += 1;
    const marker = `trusted-${sequence}`;
    el.dataset.staticTrustedRetypeId = marker;
    return { marker, field: el.dataset.staticAutofilled || inferWorkdayField(el) };
  });
  window.__STATIC_TRUSTED_RETYPE_SEQUENCE = sequence;
  return items;
})()
"""
    raw = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={"expression": queue_script, "returnByValue": True},
        session_id=cdp_session.session_id,
    )
    items = (raw or {}).get("result", {}).get("value") or []
    typed = 0
    selected = 0

    async def click_visible_option(needles: list[str]) -> bool:
        coordinate_script = f"""
(() => {{
  const visible = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 1 && rect.height > 1 && style.display !== 'none' && style.visibility !== 'hidden';
  }};
  const norm = (value) => String(value || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const needles = {json.dumps([needle.lower() for needle in needles])};
  const options = Array.from(document.querySelectorAll('[role="option"], [data-automation-id="menuItem"]'))
    .filter((option) => visible(option) && option.getAttribute('data-automation-id') !== 'selectedItem');
  const option = options.find((candidate) => {{
    const text = norm(candidate.innerText || candidate.textContent || candidate.getAttribute('aria-label'));
    return needles.some((needle) => text.includes(needle));
  }});
  if (!option) return null;
  option.scrollIntoView({{ block: 'nearest', inline: 'nearest' }});
  const rect = option.getBoundingClientRect();
  return {{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }};
}})()
"""
        coordinates: dict[str, Any] | None = None
        for _ in range(6):
            raw_coordinates = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={"expression": coordinate_script, "returnByValue": True},
                session_id=cdp_session.session_id,
            )
            candidate_coordinates = (raw_coordinates or {}).get("result", {}).get("value")
            if isinstance(candidate_coordinates, dict):
                coordinates = candidate_coordinates
                break
            scroll_raw = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": r"""
(() => {
  const lists = Array.from(document.querySelectorAll('[role="listbox"], [data-automation-id="activeListContainer"]'));
  const list = lists.find((candidate) => {
    const rect = candidate.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1 && candidate.scrollHeight > candidate.clientHeight + 2;
  });
  if (!list || list.scrollTop >= list.scrollHeight - list.clientHeight - 2) return false;
  list.scrollTop = Math.min(list.scrollHeight, list.scrollTop + Math.max(120, list.clientHeight * 0.75));
  list.dispatchEvent(new Event('scroll', { bubbles: true }));
  return true;
})()
""",
                    "returnByValue": True,
                },
                session_id=cdp_session.session_id,
            )
            if not (scroll_raw or {}).get("result", {}).get("value"):
                break
            await asyncio.sleep(0.2)
        if coordinates is None:
            return False
        x = float(coordinates.get("x") or 0)
        y = float(coordinates.get("y") or 0)
        if x <= 0 or y <= 0:
            return False
        await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
            params={"type": "mouseMoved", "x": x, "y": y},
            session_id=cdp_session.session_id,
        )
        await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
            params={"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            session_id=cdp_session.session_id,
        )
        await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
            params={"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
            session_id=cdp_session.session_id,
        )
        return True

    async def type_with_key_events(marker: str, value: str) -> bool:
        marker_value = json.dumps(marker)
        coordinate_script = f"""
(() => {{
  const el = document.querySelector(`[data-static-trusted-retype-id=${{CSS.escape({marker_value})}}]`);
  if (!el) return null;
  el.scrollIntoView({{ block: 'center', inline: 'nearest' }});
  const rect = el.getBoundingClientRect();
  return {{ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }};
}})()
"""
        raw_coordinates = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": coordinate_script, "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        coordinates = (raw_coordinates or {}).get("result", {}).get("value")
        if not isinstance(coordinates, dict):
            return False
        x = float(coordinates.get("x") or 0)
        y = float(coordinates.get("y") or 0)
        if x <= 0 or y <= 0:
            return False
        for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            params: dict[str, Any] = {"type": event_type, "x": x, "y": y}
            if event_type != "mouseMoved":
                params.update({"button": "left", "clickCount": 1})
            await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                params=params,
                session_id=cdp_session.session_id,
            )
        select_script = f"""
(() => {{
  const el = document.querySelector(`[data-static-trusted-retype-id=${{CSS.escape({marker_value})}}]`);
  if (!el) return false;
  el.focus();
  if (typeof el.select === 'function') el.select();
  return true;
}})()
"""
        await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": select_script, "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        for event_type in ("keyDown", "keyUp"):
            await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                params={"type": event_type, "key": "Backspace", "code": "Backspace"},
                session_id=cdp_session.session_id,
            )
        for character in value:
            await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                params={"type": "keyDown", "key": character, "text": character},
                session_id=cdp_session.session_id,
            )
            await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                params={"type": "keyUp", "key": character},
                session_id=cdp_session.session_id,
            )
        return True

    for item in items[:64]:
        if not isinstance(item, dict):
            continue
        marker = str(item.get("marker") or "")
        field = str(item.get("field") or "")
        value = facts.get(field)
        if field == "education_start_year":
            source = str(facts.get("education_start_date") or facts.get("school_start_date") or "")
            year_match = re.search(r"\b(?:19|20)\d{2}\b", source)
            value = year_match.group(0) if year_match else None
        if not marker or value is None or isinstance(value, (dict, list, tuple)):
            continue
        text_value = str(value)
        if not text_value:
            continue
        marker_json = json.dumps(marker)
        focus_script = f"""
(() => {{
  const el = document.querySelector(`[data-static-trusted-retype-id=${{CSS.escape({marker_json})}}]`);
  if (!el) return false;
  el.focus();
  if (typeof el.select === 'function') el.select();
  else if (typeof el.setSelectionRange === 'function') el.setSelectionRange(0, String(el.value || '').length);
  return true;
}})()
"""
        focused_raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": focus_script, "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        if not (focused_raw or {}).get("result", {}).get("value"):
            continue
        source_selected = False
        option_selected = False
        if field == "heard_about":
            await type_with_key_events(marker, text_value)
            await asyncio.sleep(0.65)
            source_selected = await click_visible_option([text_value, "linkedin"])
            if not source_selected and await click_visible_option(["external career site sources", "external sources"]):
                await asyncio.sleep(0.3)
                await type_with_key_events(marker, text_value)
                await asyncio.sleep(0.65)
                source_selected = await click_visible_option([text_value, "linkedin"])
            if source_selected:
                selected += 1
        elif field in {"major", "school"}:
            await type_with_key_events(marker, text_value)
            await asyncio.sleep(0.65)
            aliases = [text_value]
            if field == "major":
                aliases.extend(["computer science", "computing"])
            option_selected = await click_visible_option(aliases)
            if option_selected:
                selected += 1
        elif field in {"account_email", "account_password", "confirm_password"}:
            # Workday account forms are React-controlled. Trusted key events keep
            # the component state in sync; merely assigning/inserting text can
            # leave the UI looking filled while the submit handler sees blanks.
            await type_with_key_events(marker, text_value)
        else:
            await cdp_session.cdp_client.send.Input.insertText(
                params={"text": text_value},
                session_id=cdp_session.session_id,
            )
        mark_script = f"""
(() => {{
  const el = document.querySelector(`[data-static-trusted-retype-id=${{CSS.escape({marker_json})}}]`);
  if (!el) return false;
  el.dataset.staticTrustedTyped = 'true';
  el.dataset.staticReactSynced = 'true';
  if ({str(source_selected or option_selected).lower()}) el.dataset.staticAutocompleteSelected = {json.dumps(field)};
  return true;
}})()
"""
        await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": mark_script, "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        typed += 1
    if typed:
        await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": "document.activeElement?.blur(); true", "returnByValue": True},
            session_id=cdp_session.session_id,
        )
    return typed, selected


async def _trusted_sync_static_choices(cdp_session: Any) -> int:
    """Commit static privacy/terms choices through browser-level mouse input."""
    prepare_script = r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 2 && rect.height > 2;
  };
  const inputs = Array.from(document.querySelectorAll(
    'input[type="checkbox"][data-static-autofilled="terms_acceptance"]'
  )).filter((el) => !el.dataset.staticReactChoiceSynced);
  const targets = [];
  for (const input of inputs) {
    const label = input.labels?.[0]
      || input.closest('label')
      || (input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null);
    const target = visible(input) ? input : (visible(label) ? label : null);
    if (!target) continue;
    target.scrollIntoView({ block: 'center', inline: 'nearest' });
    const rect = target.getBoundingClientRect();
    targets.push({
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      id: input.id || '',
      checked: !!input.checked
    });
  }
  return targets;
})()
"""
    raw = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={"expression": prepare_script, "returnByValue": True},
        session_id=cdp_session.session_id,
    )
    targets = (raw or {}).get("result", {}).get("value")
    if not isinstance(targets, list):
        return 0
    synced = 0
    for target in targets[:8]:
        if not isinstance(target, dict):
            continue
        x = float(target.get("x") or 0)
        y = float(target.get("y") or 0)
        if x <= 0 or y <= 0:
            continue

        async def trusted_click() -> None:
            for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
                params: dict[str, Any] = {"type": event_type, "x": x, "y": y}
                if event_type != "mouseMoved":
                    params.update({"button": "left", "clickCount": 1})
                await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                    params=params,
                    session_id=cdp_session.session_id,
                )

        # The static pass may have made the DOM look checked without updating
        # Workday's controlled React value.  A trusted off/on transition makes
        # React observe both changes and leaves the required choice enabled.
        if bool(target.get("checked")):
            await trusted_click()
            await asyncio.sleep(0.12)
        await trusted_click()
        await asyncio.sleep(0.12)
        input_id = json.dumps(str(target.get("id") or ""))
        state_raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={
                "expression": f"""
(() => {{
  const id = {input_id};
  const el = id ? document.getElementById(id) : null;
  if (!el) return false;
  if (el.checked) el.dataset.staticReactChoiceSynced = 'true';
  return {{ checked: !!el.checked, ariaChecked: el.getAttribute('aria-checked') }};
}})()
""",
                "returnByValue": True,
            },
            session_id=cdp_session.session_id,
        )
        state = (state_raw or {}).get("result", {}).get("value")
        if isinstance(state, dict) and state.get("checked"):
            synced += 1
    return synced


async def probe_workday_human_checkpoint(browser: Any) -> dict[str, Any]:
    """Inspect the active page and show/remove the Workday click banner."""
    try:
        cdp_session = await browser.get_or_create_cdp_session()
        raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={
                "expression": _workday_human_checkpoint_script(),
                "returnByValue": True,
            },
            session_id=cdp_session.session_id,
        )
        value = (raw or {}).get("result", {}).get("value")
        if isinstance(value, dict):
            return value
        return {"required": False, "reason": "checkpoint_no_return_value"}
    except Exception as exc:
        return {
            "required": False,
            "reason": "checkpoint_probe_error",
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


async def _notify_workday_human_checkpoint(action: str) -> bool:
    """Focus the handoff window, ring once, and send a native notification."""
    if os.name != "posix" or not hasattr(os, "uname") or os.uname().sysname != "Darwin":
        return False
    safe_action = "Sign In" if action == "Sign In" else "Create Account"
    title = "LangHire needs one click"
    message = f"Click {safe_action} in Workday. LangHire will resume automatically."
    try:
        open_process = await asyncio.create_subprocess_exec(
            "open",
            "-b",
            "com.google.chrome.for.testing",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(open_process.communicate(), timeout=5.0)
        await asyncio.sleep(1.25)
    except Exception:
        pass
    apple_script = f"""
tell application "System Events"
  if exists process "Google Chrome for Testing" then
    set frontmost of process "Google Chrome for Testing" to true
  else if exists process "Chromium" then
    set frontmost of process "Chromium" to true
  end if
end tell
display notification {json.dumps(message)} with title {json.dumps(title)}
"""
    try:
        notification_process = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            apple_script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        sound_process = None
        sound_path = Path("/System/Library/Sounds/Glass.aiff")
        if sound_path.exists():
            sound_process = await asyncio.create_subprocess_exec(
                "afplay",
                str(sound_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        await asyncio.wait_for(notification_process.communicate(), timeout=5.0)
        if sound_process is not None:
            await asyncio.wait_for(sound_process.communicate(), timeout=5.0)
        return notification_process.returncode == 0
    except Exception:
        return False


async def wait_for_workday_human_checkpoint(
    browser: Any,
    *,
    initial: dict[str, Any] | None = None,
    timeout_seconds: float = 300.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Pause until the candidate performs the protected Workday account click.

    Two consecutive clear probes are required so a transient navigation/CDP
    state does not falsely resume the agent. A visible validation error counts
    as completion because the AI should read and resolve that error next.
    """
    timeout_seconds = max(0.1, float(timeout_seconds))
    poll_interval = max(0.05, float(poll_interval))
    checkpoint = initial if isinstance(initial, dict) else None
    if not checkpoint or not checkpoint.get("required"):
        checkpoint = await probe_workday_human_checkpoint(browser)
    else:
        # Re-probe immediately in case the user clicked between the static pass
        # and the runner entering its pause.
        current = await probe_workday_human_checkpoint(browser)
        if not current.get("required"):
            return {
                **checkpoint,
                "completed": True,
                "timed_out": False,
                "transition": current.get("reason") or "cleared_before_wait",
                "elapsed": 0.0,
            }
        checkpoint = current
    if not checkpoint.get("required"):
        return {**checkpoint, "completed": False, "timed_out": False, "elapsed": 0.0}

    action = str(checkpoint.get("action") or "Create Account")
    initial_key = str(checkpoint.get("key") or "")
    notified = await _notify_workday_human_checkpoint(action)
    loop = asyncio.get_running_loop()
    started = loop.time()
    consecutive_clear = 0
    last: dict[str, Any] = checkpoint

    while True:
        elapsed = loop.time() - started
        if elapsed >= timeout_seconds:
            return {
                **checkpoint,
                "completed": False,
                "timed_out": True,
                "notification_sent": notified,
                "elapsed": round(elapsed, 2),
                "last_reason": last.get("reason"),
            }
        await asyncio.sleep(min(poll_interval, max(0.01, timeout_seconds - elapsed)))
        current = await probe_workday_human_checkpoint(browser)
        last = current
        if current.get("required"):
            consecutive_clear = 0
            current_key = str(current.get("key") or "")
            if initial_key and current_key and current_key != initial_key:
                return {
                    **checkpoint,
                    "completed": True,
                    "timed_out": False,
                    "notification_sent": notified,
                    "transition": "account_action_changed",
                    "next_checkpoint": current,
                    "elapsed": round(loop.time() - started, 2),
                }
            continue

        if current.get("reason") == "visible_error":
            return {
                **checkpoint,
                "completed": True,
                "timed_out": False,
                "notification_sent": notified,
                "transition": "visible_error",
                "visibleErrors": current.get("visibleErrors") or [],
                "elapsed": round(loop.time() - started, 2),
            }
        consecutive_clear += 1
        if consecutive_clear >= 2:
            return {
                **checkpoint,
                "completed": True,
                "timed_out": False,
                "notification_sent": notified,
                "transition": current.get("reason") or "account_action_cleared",
                "elapsed": round(loop.time() - started, 2),
            }


async def run_static_autofill(
    browser: Any,
    facts: dict[str, str],
    resume_path: str = "",
    guard_final_submit: bool = True,
) -> dict[str, Any]:
    """Apply static facts to the currently focused page.

    This is intentionally best-effort. It should speed up and stabilize apply
    flows, but it should never crash the browser agent if a page blocks scripts.
    """
    if not facts:
        return {"filled": 0, "error": "no facts"}

    cdp_session = await browser.get_or_create_cdp_session()
    script = _autofill_script(facts)
    result: dict[str, Any] = {"filled": 0}
    try:
        cookie_raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": _cookie_helper_script(), "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        cookie_value = (cookie_raw or {}).get("result", {}).get("value")
        if isinstance(cookie_value, dict):
            result["cookie_helper"] = cookie_value
    except Exception as exc:
        result["cookie_helper"] = {"error": str(exc)[:300]}

    if guard_final_submit:
        try:
            guard_raw = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={"expression": _submit_guard_script(), "returnByValue": True},
                session_id=cdp_session.session_id,
            )
            guard_value = (guard_raw or {}).get("result", {}).get("value")
            if isinstance(guard_value, dict):
                result["submit_guard"] = guard_value
        except Exception as exc:
            result["submit_guard"] = {"error": str(exc)[:300]}

    try:
        raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": script, "returnByValue": True, "awaitPromise": True},
            session_id=cdp_session.session_id,
        )
        value = (raw or {}).get("result", {}).get("value")
        if isinstance(value, dict):
            result.update(value)
    except Exception as exc:
        result["error"] = f"js_autofill_failed: {exc}"

    try:
        trusted_retyped, trusted_selected = await _trusted_retype_static_inputs(cdp_session, facts)
        if trusted_retyped:
            result["trusted_retyped"] = trusted_retyped
        if trusted_selected:
            result["trusted_selected"] = trusted_selected
    except Exception as exc:
        result["trusted_retype_error"] = type(exc).__name__

    try:
        trusted_choices = await _trusted_sync_static_choices(cdp_session)
        if trusted_choices:
            result["trusted_choices"] = trusted_choices
    except Exception as exc:
        result["trusted_choice_error"] = type(exc).__name__

    upload = await upload_resume_to_file_inputs(browser, resume_path or facts.get("resume_path", ""))
    if upload:
        result["resume_upload"] = upload
    result["human_checkpoint"] = await probe_workday_human_checkpoint(browser)
    return result


async def release_review_handoff(browser: Any) -> dict[str, Any]:
    """Remove automation-only locks before leaving a tab for manual review.

    Static locks and the final-submit guard are useful while browser-use is
    acting, but they must not survive the ownership handoff to the candidate.
    """
    result: dict[str, Any] = {"controls": 0, "finalSubmits": 0, "observer": False}
    try:
        cdp_session = await browser.get_or_create_cdp_session()
        raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": _release_review_handoff_script(), "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        value = (raw or {}).get("result", {}).get("value")
        if isinstance(value, dict):
            result.update(value)
    except Exception as exc:
        result["error"] = f"review_handoff_release_failed: {type(exc).__name__}: {str(exc)[:200]}"
    return result


async def set_ai_pause_control(browser: Any, paused: bool | None = None) -> dict[str, Any]:
    """Install/probe the browser overlay, optionally forcing its pause state."""
    try:
        cdp_session = await browser.get_or_create_cdp_session()
        raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={
                "expression": _pause_control_overlay_script(paused),
                "returnByValue": True,
            },
            session_id=cdp_session.session_id,
        )
        value = (raw or {}).get("result", {}).get("value")
        return value if isinstance(value, dict) else {"installed": False, "paused": False}
    except Exception as exc:
        return {
            "installed": False,
            "paused": False,
            "error": f"pause_control_failed: {type(exc).__name__}: {str(exc)[:200]}",
        }


async def set_ai_pause_controls_for_tabs(
    browser: Any,
    target_ids: set[str] | list[str],
    paused: bool | None = None,
) -> list[dict[str, Any]]:
    """Install/probe the pause control on each tab owned by the current job."""
    results: list[dict[str, Any]] = []
    installed_targets = getattr(browser, "_langhire_pause_init_targets", None)
    if not isinstance(installed_targets, set):
        installed_targets = set()
        setattr(browser, "_langhire_pause_init_targets", installed_targets)

    for target_id in list(dict.fromkeys(str(item) for item in target_ids if item)):
        try:
            cdp_session = await browser.get_or_create_cdp_session(target_id=target_id, focus=False)
            if target_id not in installed_targets:
                await cdp_session.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
                    params={"source": _pause_control_overlay_script()},
                    session_id=cdp_session.session_id,
                )
                installed_targets.add(target_id)
            raw = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": _pause_control_overlay_script(paused),
                    "returnByValue": True,
                },
                session_id=cdp_session.session_id,
            )
            value = (raw or {}).get("result", {}).get("value")
            result = value if isinstance(value, dict) else {"installed": False, "paused": False}
            result["target_id"] = target_id
            results.append(result)
        except Exception as exc:
            results.append(
                {
                    "target_id": target_id,
                    "installed": False,
                    "paused": False,
                    "error": f"pause_control_failed: {type(exc).__name__}: {str(exc)[:200]}",
                }
            )
    return results


async def _focus_pause_target(browser: Any, target_id: str) -> None:
    """Make the tab where Resume was clicked the agent's actual target."""
    cdp_session = await browser.get_or_create_cdp_session(target_id=target_id, focus=True)
    await cdp_session.cdp_client.send.Target.activateTarget(
        params={"targetId": target_id},
    )


async def wait_while_ai_paused(
    browser: Any,
    cancel_flag: dict | None = None,
    worker_id: int = 0,
    target_ids: set[str] | None = None,
    ignored_target_ids: set[str] | None = None,
) -> bool:
    """Wait until the user resumes from the in-browser control.

    Returns True when an actual pause/resume cycle occurred. The pause state is
    mirrored into the API status dictionary so the desktop UI and logs can
    accurately report that the worker is intentionally idle.
    """
    if target_ids is None:
        controls = [await set_ai_pause_control(browser)]
    else:
        ignored = ignored_target_ids or set()
        try:
            for tab in await browser.get_tabs():
                target_id = str(getattr(tab, "target_id", "") or "")
                if target_id and target_id not in ignored:
                    target_ids.add(target_id)
        except Exception:
            pass
        controls = await set_ai_pause_controls_for_tabs(browser, target_ids)
    if not any(control.get("paused") for control in controls):
        return False

    if target_ids is not None:
        await set_ai_pause_controls_for_tabs(browser, target_ids, True)

    if cancel_flag is not None:
        cancel_flag["paused"] = True
    prefix = f"[W{worker_id}] " if worker_id else ""
    print(f"  ⏸️  {prefix}AI paused by user; browser controls released")
    while True:
        if cancel_flag and cancel_flag.get("cancel_requested"):
            return True
        await asyncio.sleep(0.25)
        if target_ids is None:
            controls = [await set_ai_pause_control(browser)]
        else:
            ignored = ignored_target_ids or set()
            try:
                for tab in await browser.get_tabs():
                    target_id = str(getattr(tab, "target_id", "") or "")
                    if target_id and target_id not in ignored:
                        target_ids.add(target_id)
            except Exception:
                pass
            controls = await set_ai_pause_controls_for_tabs(browser, target_ids)

        resumed_controls = [
            item for item in controls
            if item.get("installed") and item.get("userChanged") and not item.get("paused")
        ]
        if not resumed_controls:
            # A new document defaults to unpaused. Only an explicit click on a
            # visible Resume control is allowed to release a backend pause.
            if target_ids is None:
                await set_ai_pause_control(browser, True)
            else:
                await set_ai_pause_controls_for_tabs(browser, target_ids, True)
            continue

        resumed_control = max(resumed_controls, key=lambda item: int(item.get("updatedAt") or 0))
        resumed_target_id = str(resumed_control.get("target_id") or "")
        if resumed_target_id:
            try:
                await _focus_pause_target(browser, resumed_target_id)
            except Exception:
                pass
        if target_ids is not None:
            await set_ai_pause_controls_for_tabs(browser, target_ids, False)
        break

    if cancel_flag is not None:
        cancel_flag["paused"] = False
    print(f"  ▶️  {prefix}AI resumed by user")
    return True


async def try_safe_progress_step(browser: Any) -> dict[str, Any]:
    """Click exactly one safe non-final progress/navigation button.

    This is for manual-review prep, not autonomous final submission. It may
    open an ATS account form or advance to the next form page. Protected Workday
    account submits return a human checkpoint instead of being automated, and
    the final-submit guard remains responsible for real application submission.
    """
    checkpoint = await probe_workday_human_checkpoint(browser)
    if checkpoint.get("required"):
        return {
            "clicked": False,
            "reason": "human_click_required",
            "human_checkpoint": checkpoint,
        }

    cdp_session = await browser.get_or_create_cdp_session()
    try:
        raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": _safe_progress_step_script(), "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        value = (raw or {}).get("result", {}).get("value")
        if isinstance(value, dict):
            if value.get("clicked") and value.get("reason") in {
                "account_create_or_signup",
                "account_or_form_continue",
                "form_continue",
            }:
                trusted_script = f"""
(() => {{
  const reason = {json.dumps(str(value.get('reason') or ''))};
  const visible = (el) => {{
    if (!el || el.disabled) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 1 && rect.height > 1 && style.display !== 'none' && style.visibility !== 'hidden';
  }};
  const norm = (text) => String(text || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const label = (el) => norm([
    el?.innerText, el?.textContent, el?.value, el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('data-automation-id')
  ].filter(Boolean).join(' '));
  const candidates = Array.from(document.querySelectorAll(
    'button, input[type="submit"], input[type="button"], a, [role="button"], [role="link"], [data-automation-id="click_filter"]'
  )).filter(visible);
  const body = norm(document.body?.innerText || '');
  const pageTitle = norm(document.title);
  const automation = (el) => norm(el?.getAttribute?.('data-automation-id'));
  let target = null;
  let action = reason;
  if (reason === 'account_create_or_signup') {{
    // "Already have an account?" is permanent helper copy on Workday's
    // create-account page, not an account-exists error.  Only switch to Sign
    // In when the page reports a concrete duplicate-account condition.
    const accountExists = /account (?:already )?(?:exists|registered)|already registered|previously registered|email(?: address)? (?:is )?(?:already registered|already in use|already associated)|existing account (?:was )?found/.test(body);
    if (accountExists) {{
      // Prefer the form-local link over Workday's global header Sign In button.
      target = candidates.find((el) => automation(el) === 'signinlink')
        || candidates.find((el) => automation(el) === 'navigationitem-sign in')
        || candidates.find((el) => norm(el.getAttribute('aria-label')) === 'sign in'
          && automation(el) === 'click_filter')
        || candidates.find((el) => /^sign in\\b/.test(label(el)));
      action = 'account_existing_sign_in';
    }}
    if (!target) {{
      // On the account form, prefer the actual submit control over Workday's
      // identically labelled global navigation item.
      target = candidates.find((el) => automation(el) === 'createaccountsubmitbutton')
        || candidates.find((el) => norm(el.getAttribute('aria-label')) === 'create account'
          && ['button', 'input'].includes(norm(el.tagName)));
      action = 'account_create_submit';
    }}
    if (!target && pageTitle.includes('sign in')) {{
      target = candidates.find((el) => automation(el) === 'createaccountlink')
        || candidates.find((el) => automation(el) === 'navigationitem-create an account')
        || candidates.find((el) => /^create (?:an )?account\\b/.test(label(el)));
      action = 'account_create_link';
    }}
    if (!target) {{
      // Workday can briefly render only its account-navigation shell.
      target = candidates.find((el) => automation(el) === 'createaccountlink')
        || candidates.find((el) => automation(el) === 'navigationitem-create an account')
        || candidates.find((el) => /^create (?:an )?account\\b/.test(label(el)));
      action = 'account_create_link';
    }}
  }} else if (reason === 'form_continue') {{
    target = candidates.find((el) => automation(el) === 'pagefooternextbutton')
      || candidates.find((el) => /^(save and continue|continue|next)(?: .*pagefooternextbutton)?$/.test(label(el)));
  }} else {{
    target = candidates.find((el) => automation(el) === 'signinsubmitbutton')
      || candidates.find((el) => norm(el.getAttribute('aria-label')) === 'sign in'
        && automation(el) === 'click_filter')
      || candidates.find((el) => /^(sign in|log in)(?: .*click_filter)?$/.test(label(el)))
      || candidates.find((el) => automation(el) === 'pagefooternextbutton')
      || candidates.find((el) => /^(continue|next)(?: .*pagefooternextbutton)?$/.test(label(el)));
  }}
  if (!target) return null;
  const targetLabel = label(target);
  if (/submit application|finish application|complete application|final submit/.test(targetLabel)) return null;
  target.scrollIntoView({{ block: 'center', inline: 'center' }});
  const rect = target.getBoundingClientRect();
  return {{
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    label: targetLabel,
    action,
    automation: automation(target),
  }};
}})()
"""
                trusted_raw = await cdp_session.cdp_client.send.Runtime.evaluate(
                    params={"expression": trusted_script, "returnByValue": True},
                    session_id=cdp_session.session_id,
                )
                target = (trusted_raw or {}).get("result", {}).get("value")
                if isinstance(target, dict):
                    x = float(target.get("x") or 0)
                    y = float(target.get("y") or 0)
                    if x > 0 and y > 0:
                        automation_id = str(target.get("automation") or "")
                        if automation_id in {
                            "createaccountsubmitbutton",
                            "signinsubmitbutton",
                        }:
                            checkpoint = await probe_workday_human_checkpoint(browser)
                            return {
                                "clicked": False,
                                "reason": "human_click_required",
                                "human_checkpoint": checkpoint,
                                "trusted_action": target.get("action"),
                                "trusted_target": target.get("label"),
                            }
                        await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                            params={"type": "mouseMoved", "x": x, "y": y},
                            session_id=cdp_session.session_id,
                        )
                        await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                            params={"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                            session_id=cdp_session.session_id,
                        )
                        await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                            params={"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                            session_id=cdp_session.session_id,
                        )
                        value["trusted_click"] = True
                        value["trusted_action"] = target.get("action")
                        value["trusted_target"] = target.get("label")
            return value
        return {"clicked": False, "reason": "no_return_value"}
    except Exception as exc:
        return {"clicked": False, "reason": f"safe_progress_failed: {exc}"}


async def try_controlled_final_submit(
    browser: Any,
    facts: dict[str, str],
    resume_path: str = "",
) -> dict[str, Any]:
    """Validate the current page and release exactly one guarded final submit.

    The browser agent is not allowed to decide when to submit. This helper first
    runs one final static fill/validation pass with the guard installed, then
    clicks the final submit control only when there are no obvious required
    blanks, validation errors, credential errors, or verification-code blockers.
    """
    review = await run_static_autofill(
        browser,
        facts,
        resume_path=resume_path,
        guard_final_submit=True,
    )
    blockers: list[str] = []
    for key in ("requiredEmpty", "invalidFields"):
        if int(review.get(key) or 0):
            blockers.append(f"{key}={review.get(key)}")
    if review.get("visibleErrors"):
        blockers.append(f"visibleErrors={review.get('visibleErrors')}")
    if review.get("requiredEmptyLabels"):
        blockers.append(f"requiredEmptyLabels={review.get('requiredEmptyLabels')}")
    if review.get("credentialError"):
        blockers.append("credentialError=true")
    if review.get("verificationCodeRequired"):
        blockers.append("verificationCodeRequired=true")
    if blockers:
        return {
            "submitted": False,
            "reason": "static_validation_blocked",
            "blockers": blockers[:8],
            "review": review,
        }

    cdp_session = await browser.get_or_create_cdp_session()
    try:
        raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": _controlled_final_submit_script(), "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        value = (raw or {}).get("result", {}).get("value")
        if isinstance(value, dict):
            value["review"] = review
            return value
        return {"submitted": False, "reason": "no_return_value", "review": review}
    except Exception as exc:
        return {"submitted": False, "reason": f"controlled_submit_failed: {exc}", "review": review}


async def upload_resume_to_file_inputs(browser: Any, resume_path: str) -> dict[str, Any] | None:
    """Upload the resume to obvious resume/CV file inputs via CDP."""
    if not resume_path or not Path(resume_path).exists():
        return None

    cdp_session = await browser.get_or_create_cdp_session()
    try:
        marker_raw = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={
                "expression": r"""
(() => Array.from(document.querySelectorAll('input[type="file"]')).map((el, index) => {
  const marker = `resume-upload-${index + 1}`;
  el.dataset.staticResumeUploadId = marker;
  const attrs = Array.from(el.attributes || []).map((attr) => `${attr.name} ${attr.value}`).join(' ');
  const context = `${attrs} ${el.closest('section, fieldset, [data-automation-id], div')?.innerText || ''}`.toLowerCase();
  const looksCover = /cover letter|portfolio|photo|image/.test(context);
  const looksResume = /resume|curriculum vitae|\bcv\b|upload|file|pdf/.test(context);
  return { marker, looksResume: looksResume && !looksCover };
}))()
""",
                "returnByValue": True,
            },
            session_id=cdp_session.session_id,
        )
        marker_items = (marker_raw or {}).get("result", {}).get("value") or []
        document = await cdp_session.cdp_client.send.DOM.getDocument(
            params={"depth": 1, "pierce": True},
            session_id=cdp_session.session_id,
        )
        root_id = document["root"]["nodeId"]
        uploaded = 0
        considered = len(marker_items)
        for item in marker_items[:4]:
            if not isinstance(item, dict) or not item.get("looksResume"):
                continue
            marker = str(item.get("marker") or "")
            if not marker:
                continue
            query = await cdp_session.cdp_client.send.DOM.querySelector(
                params={
                    "nodeId": root_id,
                    "selector": f'[data-static-resume-upload-id="{marker}"]',
                },
                session_id=cdp_session.session_id,
            )
            node_id = int(query.get("nodeId") or 0)
            if not node_id:
                continue
            await cdp_session.cdp_client.send.DOM.setFileInputFiles(
                params={"nodeId": node_id, "files": [resume_path]},
                session_id=cdp_session.session_id,
            )
            uploaded += 1
        return {"uploaded": uploaded, "considered": considered} if considered else None
    except Exception as exc:
        return {"uploaded": 0, "error": str(exc)[:300]}
