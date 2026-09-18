"""Deterministic Workday filling for widgets the static autofill cannot drive: the
My Experience rows (work history, education) and date spin-buttons.

Workday builds this page from repeatable rows (Add / Add Another) whose date
widgets are React spin-buttons, so it needs trusted key events and a row-aware
plan rather than the one-field-at-a-time static autofill. Work history comes
from the resume; education comes from the same facts the rest of the engine uses.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

_MONTHS = {
    name: f"{index:02d}"
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_EXPERIENCE_HEADINGS = {
    "experience", "work experience", "professional experience", "employment", "work history",
}
_DATE = r"(?:[A-Za-z]{3,9}\.?\s+\d{4}|\d{1,2}/\d{4})"
_RANGE_RE = re.compile(rf"^(?P<start>{_DATE})\s*[–—-]\s*(?P<end>{_DATE}|Present|Current|Now)\s*$", re.I)
_HEADING_RE = re.compile(r"^[A-Z][A-Z &/]{2,40}$")
_BULLETS = "•●▪◦*-"
_DESCRIPTION_LIMIT = 1800

_DEGREE_LEVELS = (
    ("bachelor", ("bachelor",)),
    ("master", ("master",)),
    ("doctorate", ("doctor", "phd")),
    ("associate", ("associate",)),
    ("high school", ("high school", "secondary")),
)


def _month_year(text: str) -> tuple[str, str]:
    text = text.strip()
    numeric = re.match(r"^(\d{1,2})/(\d{4})$", text)
    if numeric and 1 <= int(numeric.group(1)) <= 12:
        return f"{int(numeric.group(1)):02d}", numeric.group(2)
    named = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$", text)
    if named and named.group(1)[:3].lower() in _MONTHS:
        return _MONTHS[named.group(1)[:3].lower()], named.group(2)
    return "", ""


def _valid_range(line: str) -> re.Match | None:
    match = _RANGE_RE.match(line.strip())
    if not match or not all(_month_year(match.group("start"))):
        return None
    end = match.group("end")
    if end.lower() not in {"present", "current", "now"} and not all(_month_year(end)):
        return None
    return match


def _split_header(header: str) -> tuple[str, str]:
    # "Title — Company  ·  Advisor: Dr. X": the advisor tail is not part of the employer.
    head = re.split(r"\s+[·|]\s+", header)[0]
    parts = re.split(r"\s+[—–|]\s+|\s+-\s+", head, maxsplit=1)
    title = parts[0].strip()
    company = parts[1].strip() if len(parts) > 1 else ""
    return title, company


def _description(lines: list[str]) -> str:
    items: list[str] = []
    for line in lines:
        if line[0] in _BULLETS:
            items.append(line.lstrip(_BULLETS + " ").strip())
        elif items:
            items[-1] = f"{items[-1]} {line}".strip()
        else:
            items.append(line)
    kept: list[str] = []
    for item in items:
        if len("\n".join([*kept, item])) > _DESCRIPTION_LIMIT:
            break
        kept.append(item)
    return "\n".join(kept)


def parse_experience_text(text: str) -> list[dict[str, Any]]:
    """Read work-history entries out of resume text.

    An entry is a header line ("Title — Company") directly above a date range;
    the bullets under it, with wrapped lines rejoined, become the description.
    """
    lines = [line.strip() for line in text.splitlines()]
    start = next(
        (i for i, line in enumerate(lines)
         if line and line == line.upper() and line.lower() in _EXPERIENCE_HEADINGS),
        None,
    )
    if start is None:
        return []
    body: list[str] = []
    for line in lines[start + 1:]:
        if line and _HEADING_RE.match(line) and line.lower() not in _EXPERIENCE_HEADINGS:
            break
        if line:
            body.append(line)

    headers = [
        i for i in range(len(body) - 1)
        if body[i][0] not in _BULLETS and _valid_range(body[i + 1])
    ]
    entries: list[dict[str, Any]] = []
    for position, index in enumerate(headers):
        span = _valid_range(body[index + 1])
        title, company = _split_header(body[index])
        end_index = headers[position + 1] if position + 1 < len(headers) else len(body)
        start_month, start_year = _month_year(span.group("start"))
        current = span.group("end").lower() in {"present", "current", "now"}
        end_month, end_year = ("", "") if current else _month_year(span.group("end"))
        entries.append({
            "title": title,
            "company": company,
            "start_month": start_month,
            "start_year": start_year,
            "end_month": end_month,
            "end_year": end_year,
            "current": current,
            "description": _description(body[index + 2:end_index]),
        })
    return entries


def load_work_experience(resume_path: str) -> list[dict[str, Any]]:
    if not resume_path or not Path(resume_path).exists():
        return []
    try:
        import fitz  # pymupdf, already bundled for resume tailoring

        with fitz.open(resume_path) as document:
            text = "\n".join(page.get_text() for page in document)
    except Exception:
        return []
    return parse_experience_text(text)


def with_locations(entries: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Add a Location to each entry. The resume does not state one, so it comes from the
    profile's `work_locations` (employer name -> place) or, for a role at the candidate's
    own university, their home city. Anything else stays blank; a place is never guessed."""
    known = {
        _norm(name): str(place).strip()
        for name, place in (profile.get("work_locations") or {}).items()
        if str(name).strip() and str(place).strip()
    }
    address = profile.get("address") or {}
    home = ", ".join(part for part in (str(address.get("city") or "").strip(), str(address.get("state") or "").strip()) if part)
    school = _norm((profile.get("education") or {}).get("school", ""))
    located = []
    for entry in entries:
        company = _norm(entry["company"])
        place = next((where for name, where in known.items() if name in company), "")
        if not place and school and company == school:
            place = home
        located.append({**entry, "location": place})
    return located


def _year(value: object) -> str:
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return match.group(0) if match else ""


def education_plan(facts: dict[str, Any]) -> dict[str, str]:
    gpa = re.search(r"\d+(?:\.\d+)?", str(facts.get("gpa") or ""))
    return {
        "school": str(facts.get("school") or ""),
        "degree": str(facts.get("degree") or ""),
        "major": str(facts.get("major") or ""),
        "gpa": gpa.group(0) if gpa else "",
        "first_year": _year(facts.get("education_start_date") or facts.get("school_start_date")),
        "last_year": _year(
            facts.get("education_end_date") or facts.get("graduation") or facts.get("graduation_year")
        ),
    }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower().replace("'", ""))).strip()


def degree_option_rank(degree: str, option: str) -> int:
    """Score a dropdown option against the candidate's degree; 0 means unusable."""
    wanted, candidate = _norm(degree), _norm(re.sub(r"\(.*?\)", "", str(option or "")))
    if not candidate:
        return 0
    level = next((name for name, words in _DEGREE_LEVELS if any(w in wanted for w in words)), "")
    words = dict(_DEGREE_LEVELS).get(level, ())
    if not level or not any(w in candidate for w in words):
        return 0
    # "Bachelor of Science" beats a generic "Bachelor's Degree" for a B.S.
    return 20 if candidate in wanted else 10


# --- live page ---------------------------------------------------------------

_SECTIONS = {"Work Experience": "workExperience", "Education": "education"}

_STATE_JS = r"""(() => {
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  const val = (row, css) => { const e = row.querySelector(css); return e ? String(e.value || '') : ''; };
  const date = (row, field, part) => {
    const box = row.querySelector(`[data-automation-id="formField-${field}"]`);
    if (!box) return '';
    const shown = clean(box?.querySelector(`[data-automation-id="dateSection${part}-display"]`)?.innerText);
    return val(box, `input[data-automation-id="dateSection${part}-input"]`) || (/^(MM|YYYY)$/.test(shown) ? '' : shown);
  };
  const out = {};
  for (const [name, prefix] of Object.entries(SECTIONS)) {
    const heading = Array.from(document.querySelectorAll('h4')).find(h => clean(h.innerText).toLowerCase() === name.toLowerCase());
    if (!heading) { out[name] = null; continue; }
    const section = heading.parentElement;
    const rows = Array.from(section.querySelectorAll(`[data-fkit-id^="${prefix}-"][data-fkit-id$="--null"]`)).map(row => {
      const selected = clean(row.querySelector('[data-automation-id="promptAriaInstruction"]')?.innerText).match(/(\d+)\s+items?\s+selected/i);
      const degree = clean(row.querySelector('[data-automation-id="formField-degree"] button')?.innerText);
      return {
        id: row.getAttribute('data-fkit-id'),
        job: row.getAttribute('data-langhire-job') || '',
        title: val(row, 'input[name="jobTitle"]'),
        company: val(row, 'input[name="companyName"]'),
        location: val(row, 'input[name="location"]'),
        current: !!row.querySelector('input[name="currentlyWorkHere"]')?.checked,
        startMonth: date(row, 'startDate', 'Month'), startYear: date(row, 'startDate', 'Year'),
        endMonth: date(row, 'endDate', 'Month'), endYear: date(row, 'endDate', 'Year'),
        description: val(row, 'textarea'),
        school: val(row, 'input[name="schoolName"]'),
        degree: /^select/i.test(degree) ? '' : degree,
        fieldSelected: selected ? Number(selected[1]) : 0,
        gpa: val(row, 'input[name="gradeAverage"]'),
        firstYear: date(row, 'firstYearAttended', 'Year'), lastYear: date(row, 'lastYearAttended', 'Year'),
      };
    });
    const add = section.querySelector('[data-automation-id="add-button"]');
    out[name] = { rows, add: add ? clean(add.innerText) : '' };
  }
  return out;
})()""".replace("SECTIONS", json.dumps(_SECTIONS))


async def _session(browser):
    return await browser.get_or_create_cdp_session()


async def _eval(browser, expression: str) -> Any:
    session = await _session(browser)
    raw = await session.cdp_client.send.Runtime.evaluate(
        params={"expression": expression, "returnByValue": True},
        session_id=session.session_id,
    )
    return (raw or {}).get("result", {}).get("value")


async def _state(browser) -> dict[str, Any]:
    return await _eval(browser, _STATE_JS) or {}


def _field(row_id: str, field: str, inner: str) -> str:
    """JS expression for an element inside one Workday row."""
    css = f'[data-fkit-id="{row_id}"] [data-automation-id="formField-{field}"] {inner}'
    return f"document.querySelector({json.dumps(css)})"


async def _click(browser, element_expr: str) -> bool:
    # Scroll first and let the layout settle: options in a scrolling list move under the pointer.
    scrolled = await _eval(browser, f"""(() => {{
      const el = {element_expr};
      if (!el) return false;
      el.scrollIntoView({{ block: 'center', inline: 'nearest' }});
      return true;
    }})()""")
    if not scrolled:
        return False
    await asyncio.sleep(0.15)
    center = await _eval(browser, f"""(() => {{
      const r = ({element_expr}).getBoundingClientRect();
      return r.width > 1 && r.height > 1 ? {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }} : null;
    }})()""")
    if not isinstance(center, dict):
        return False
    session = await _session(browser)
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        params: dict[str, Any] = {"type": kind, "x": center["x"], "y": center["y"]}
        if kind != "mouseMoved":
            params.update({"button": "left", "clickCount": 1})
        await session.cdp_client.send.Input.dispatchMouseEvent(params=params, session_id=session.session_id)
    return True


async def _press(browser, key: str) -> None:
    session = await _session(browser)
    for kind in ("keyDown", "keyUp"):
        await session.cdp_client.send.Input.dispatchKeyEvent(
            params={"type": kind, "key": key, "code": key}, session_id=session.session_id
        )


async def _key_events(browser, text: str) -> None:
    session = await _session(browser)
    for character in text:
        await session.cdp_client.send.Input.dispatchKeyEvent(
            params={"type": "keyDown", "key": character, "text": character}, session_id=session.session_id
        )
        await session.cdp_client.send.Input.dispatchKeyEvent(
            params={"type": "keyUp", "key": character}, session_id=session.session_id
        )


async def _type(browser, element_expr: str, text: str, *, bulk: bool = False) -> bool:
    """Focus an element and type into it with trusted events (React-controlled inputs)."""
    if not await _click(browser, element_expr):
        return False
    await _eval(browser, f"(() => {{ const el = {element_expr}; if (el && el.select) el.select(); }})()")
    await _press(browser, "Backspace")
    if bulk:
        session = await _session(browser)
        await session.cdp_client.send.Input.insertText(params={"text": text}, session_id=session.session_id)
        return True
    await _key_events(browser, text)
    return True


async def _type_date(browser, row_id: str, field: str, month: str, year: str) -> None:
    # The spin-button inputs are sub-pixel; the visible display div forwards focus to them.
    for part, digits in (("Month", month), ("Year", year)):
        if digits and await _click(browser, _field(row_id, field, f'[data-automation-id="dateSection{part}-display"]')):
            await _key_events(browser, digits)


async def _visible_options(browser) -> list[dict[str, Any]]:
    return await _eval(browser, r"""(() => Array.from(document.querySelectorAll(
      '[role="option"], [data-automation-id="menuItem"], [data-automation-id="promptOption"]'
    )).filter(e => { const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; })
      .map((e, index) => ({ index, text: String(e.innerText || '').replace(/\s+/g, ' ').trim() })))()""") or []


async def _click_option(browser, index: int) -> bool:
    return await _click(browser, f"""Array.from(document.querySelectorAll(
      '[role="option"], [data-automation-id="menuItem"], [data-automation-id="promptOption"]'
    )).filter(e => {{ const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; }})[{index}]""")


async def _wait_rows(browser, section: str, count: int, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    state: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        state = (await _state(browser)).get(section) or {}
        if len(state.get("rows") or []) >= count:
            break
        await asyncio.sleep(0.3)
    return state


async def _fill_work_row(browser, row: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Fill only what the row is missing. Returns whether anything was typed."""
    row_id, touched = row["id"], False
    if not row["title"]:
        touched |= await _type(browser, _field(row_id, "jobTitle", "input"), entry["title"])
    if not row["company"]:
        touched |= await _type(browser, _field(row_id, "companyName", "input"), entry["company"])
    if entry.get("location") and not row["location"]:
        touched |= await _type(browser, _field(row_id, "location", "input"), entry["location"])
    if entry["current"] and not row["current"]:
        touched |= await _click(browser, _field(row_id, "currentlyWorkHere", 'input[type="checkbox"]'))
    if not (row["startMonth"] and row["startYear"]):
        await _type_date(browser, row_id, "startDate", entry["start_month"], entry["start_year"])
        touched = True
    if not entry["current"] and not (row["endMonth"] and row["endYear"]):
        await _type_date(browser, row_id, "endDate", entry["end_month"], entry["end_year"])
        touched = True
    if entry["description"] and not row["description"]:
        touched |= await _type(browser, _field(row_id, "roleDescription", "textarea"), entry["description"], bulk=True)
    return touched


async def _mark_row(browser, row_id: str, index: int) -> None:
    selector = json.dumps(f'[data-fkit-id="{row_id}"]')
    await _eval(browser, f"document.querySelector({selector})?.setAttribute('data-langhire-job', '{index}')")


def choose_work_row(
    rows: list[dict[str, Any]], entry: dict[str, Any], index: int, entry_count: int
) -> tuple[str, dict[str, Any] | None]:
    """Pick the row for resume entry `index`: ("row", row) to fill or complete it,
    ("add", None) to click Add, or ("full", None) when a new row would exceed the entry count.

    Rows carry a marker once filled, so a row whose text was later changed is still
    recognised as ours; adding is capped so a failed match can never grow the list forever.
    """
    for row in rows:
        if row.get("job") == str(index) or _same_job(row, entry):
            return "row", row
    blank = next((row for row in rows if not row["title"] and not row["company"] and not row.get("job")), None)
    if blank is not None:
        return "row", blank
    if len(rows) >= entry_count:
        return "full", None
    return "add", None


def _same_job(row: dict[str, Any], entry: dict[str, Any]) -> bool:
    return _norm(row.get("title", "")) == _norm(entry["title"]) and _norm(row.get("company", "")) == _norm(entry["company"])


async def fill_work_history(browser, entries: list[dict[str, Any]], worker_id: int = 0) -> dict[str, Any]:
    """Add one Workday row per resume entry. Never edits or deletes a row that has content."""
    report: dict[str, Any] = {"added": 0, "already_present": 0, "failed": []}
    section = (await _state(browser)).get("Work Experience")
    if section is None:
        return {**report, "skipped": "no work experience section"}
    for index, entry in enumerate(entries):
        action, target = choose_work_row(section.get("rows") or [], entry, index, len(entries))
        if action == "full":
            report["failed"].append(f"{entry['title']}: rows already fill the list")
            continue
        if action == "add":
            before = len(section.get("rows") or [])
            if not section.get("add") or not await _click(
                browser, "document.evaluate('//h4[normalize-space()=\"Work Experience\"]', document, null, 9, null)"
                         ".singleNodeValue.parentElement.querySelector('[data-automation-id=\"add-button\"]')"
            ):
                report["failed"].append(f"{entry['title']}: no Add button")
                continue
            section = await _wait_rows(browser, "Work Experience", before + 1)
            _, target = choose_work_row(section.get("rows") or [], entry, index, len(entries) + 1)
            if target is None or target.get("job") not in ("", str(index)):
                report["failed"].append(f"{entry['title']}: new row did not appear")
                continue
        await _mark_row(browser, target["id"], index)
        if await _fill_work_row(browser, target, entry):
            report["added"] += 1
            print(f"    🧾 [W{worker_id}] Work experience: {entry['title']} — {entry['company']}")
        else:
            report["already_present"] += 1
        section = (await _state(browser)).get("Work Experience") or section
    return report


async def _pick_option(browser, wanted: list[str], rank) -> bool:
    options = await _visible_options(browser)
    best = max(options, key=lambda option: rank(option["text"]), default=None)
    if best is None or rank(best["text"]) <= 0:
        return False
    return await _click_option(browser, best["index"])


async def fill_education(browser, plan: dict[str, str], worker_id: int = 0) -> dict[str, Any]:
    """Fill the first Education row's blank fields; never overwrites a value or deletes a row."""
    section = (await _state(browser)).get("Education")
    if section is None:
        return {"skipped": "no education section"}
    rows = section.get("rows") or []
    if not rows:
        if not section.get("add") or not await _click(
            browser, "document.evaluate('//h4[normalize-space()=\"Education\"]', document, null, 9, null)"
                     ".singleNodeValue.parentElement.querySelector('[data-automation-id=\"add-button\"]')"
        ):
            return {"failed": ["no Education row and no Add button"]}
        rows = (await _wait_rows(browser, "Education", 1)).get("rows") or []
        if not rows:
            return {"failed": ["Education row did not appear"]}
    row, row_id, filled = rows[0], rows[0]["id"], []

    if plan["school"] and not row["school"]:
        await _type(browser, _field(row_id, "schoolName", "input"), plan["school"])
        await asyncio.sleep(0.8)
        # Some tenants turn the school box into a search list; take the best match if one opened.
        wanted = _norm(plan["school"])
        await _pick_option(browser, [plan["school"]], lambda text: 2 if _norm(text) == wanted else int(wanted in _norm(text)))
        filled.append("school")
    if plan["degree"] and not row["degree"]:
        if await _click(browser, _field(row_id, "degree", "button")):
            await asyncio.sleep(0.6)
            if await _pick_option(browser, [plan["degree"]], lambda text: degree_option_rank(plan["degree"], text)):
                filled.append("degree")
            else:
                await _press(browser, "Escape")
    if plan["major"] and not row["fieldSelected"]:
        if await _type(browser, _field(row_id, "fieldOfStudy", "input"), plan["major"]):
            await _press(browser, "Enter")  # the search box only filters on Enter
            await asyncio.sleep(1.5)
            major = _norm(plan["major"])
            if await _pick_option(browser, [plan["major"]], lambda text: 2 if _norm(text) == major else int(major in _norm(text))):
                filled.append("major")
                await asyncio.sleep(0.4)
            else:
                await _press(browser, "Escape")
    if plan["gpa"] and not row["gpa"]:
        if await _type(browser, _field(row_id, "gradeAverage", "input"), plan["gpa"]):
            filled.append("gpa")
    for field, key, state_key in (("firstYearAttended", "first_year", "firstYear"), ("lastYearAttended", "last_year", "lastYear")):
        if plan[key] and not row[state_key]:
            if await _click(browser, _field(row_id, field, '[data-automation-id="dateSectionYear-display"]')):
                await _key_events(browser, plan[key])
                filled.append(key)
    if filled:
        print(f"    🎓 [W{worker_id}] Education: {', '.join(filled)}")
    return {"filled": filled}


def signature_date_parts(today: date) -> tuple[str, str, str]:
    return f"{today.month:02d}", f"{today.day:02d}", str(today.year)


_EMPTY_DATES_JS = r"""(() => {
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('[data-automation-id^="formField-"]'))
    .filter(field => {
      const label = clean(field.querySelector('label')?.innerText).replace(/\*$/, '').trim();
      const part = name => field.querySelector(`input[data-automation-id="dateSection${name}-input"]`);
      return /^date( signed)?$/i.test(label) && part('Month') && part('Day') && part('Year')
        && !part('Month').value && !part('Day').value && !part('Year').value;
    })
    .map(field => field.getAttribute('data-automation-id'));
})()"""


async def fill_signature_dates(browser, worker_id: int = 0, today: date | None = None) -> int:
    """Type today's date into any empty full-date "Date" box (the signature on
    Voluntary Disclosures / Self Identify). No fact exists for it, so nothing else fills it."""
    month, day, year = signature_date_parts(today or date.today())
    filled = 0
    for field in await _eval(browser, _EMPTY_DATES_JS) or []:
        done = True
        for part, digits in (("Month", month), ("Day", day), ("Year", year)):
            selector = json.dumps(f'[data-automation-id="{field}"] [data-automation-id="dateSection{part}-display"]')
            display = f"document.querySelector({selector})"
            if await _click(browser, display):
                await _key_events(browser, digits)
            else:
                done = False
        filled += done
    if filled:
        print(f"    📅 [W{worker_id}] Signature date: {month}/{day}/{year}")
    return filled
