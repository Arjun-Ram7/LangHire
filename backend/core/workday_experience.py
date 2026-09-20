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
import random
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable

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
        school: val(row, 'input[name="schoolName"]') || clean(row.querySelector('[data-automation-id="formField-school"] [data-automation-id="promptAriaInstruction"]')?.innerText).replace(/^\d+\s+items?\s+selected,?\s*/i, ''),
        schoolSearch: !!row.querySelector('[data-automation-id="formField-school"] [data-automation-id="multiSelectContainer"]'),
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


_OPTION_SELECTOR = '[role="option"], [data-automation-id="menuItem"], [data-automation-id="promptOption"]'


def options_expr(anchor: str | None) -> str:
    """JS expression for the option elements of the popup that is open.

    Other widgets keep hidden lists (Country Phone Code's "United States of America (+1)" rows, the
    State list), so with an `anchor` element the popup nearest to it is used, not every list on the page.
    """
    return f"""(() => {{
  const shown = e => {{ const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; }};
  let popups = Array.from(document.querySelectorAll('[role="listbox"], [data-automation-id="activeListContainer"]')).filter(shown);
  const anchor = {anchor or 'null'};
  if (anchor && popups.length > 1) {{
    const a = anchor.getBoundingClientRect();
    const distance = p => {{ const r = p.getBoundingClientRect(); return Math.abs(r.top - a.bottom) + Math.abs((r.left + r.width / 2) - (a.left + a.width / 2)) / 4; }};
    popups = [popups.sort((x, y) => distance(x) - distance(y))[0]];
  }}
  const roots = popups.length ? popups : [document];
  return roots.flatMap(root => Array.from(root.querySelectorAll('{_OPTION_SELECTOR}'))).filter(shown);
}})()"""


async def _visible_options(browser, anchor: str | None = None) -> list[dict[str, Any]]:
    return await _eval(browser, f"""{options_expr(anchor)}.map((e, index) => ({{ index, text: String(e.innerText || '').replace(/\\s+/g, ' ').trim() }}))""") or []


async def _click_option(browser, index: int, anchor: str | None = None) -> bool:
    return await _click(browser, f"{options_expr(anchor)}[{index}]")


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


# A school's short name and the official one a search list carries.
_SCHOOL_ALIASES = {"virginia tech": ("virginia polytechnic institute",)}


def school_option_rank(school: str, option: str) -> int:
    """Rank a search-list entry for the candidate's school; 0 is never chosen."""
    wanted, text = _norm(school), _norm(option)
    if not wanted or not text:
        return 0
    if text == wanted:
        return 3
    if any(text.startswith(alias) for alias in _SCHOOL_ALIASES.get(wanted, ())):
        return 2
    # "West Virginia University" or "Virginia Union University" merely contain a shared word.
    return 1 if text.startswith(wanted) else 0


def _school_ok(school: str, current: str) -> bool:
    return school_option_rank(school, current) > 0 or _norm(school) == _norm(current)


def education_field_updates(plan: dict[str, str], row: dict[str, Any]) -> list[str]:
    """Fields of the Education row to (re)type, in fill order. Blank fields are filled and a
    value that differs from the candidate's facts is corrected: an earlier run's answer is saved
    in Workday's draft, so leaving non-blank values alone kept a wrong degree or year forever."""
    updates = []
    if plan["school"] and not _school_ok(plan["school"], row["school"]):
        updates.append("school")
    # Any "Bachelor of Science ..." option is right for a B.S.; only a lesser match is corrected.
    if plan["degree"] and degree_option_rank(plan["degree"], row["degree"]) < 20:
        updates.append("degree")
    if plan["gpa"] and row["gpa"] != plan["gpa"]:
        updates.append("gpa")
    if plan["first_year"] and row["firstYear"] != plan["first_year"]:
        updates.append("first_year")
    if plan["last_year"] and row["lastYear"] != plan["last_year"]:
        updates.append("last_year")
    return updates


async def _pick_school_from_search(browser, row_id: str, school: str) -> str | None:
    """Type into a search-list School field and click the entry for the school."""
    element = _field(row_id, "school", "input")
    aliases = [school, *(alias for alias in _SCHOOL_ALIASES.get(_norm(school), ()))]
    for query in aliases:
        if not await _type(browser, element, query):
            return None
        await _press(browser, "Enter")  # the search box only filters on Enter
        await asyncio.sleep(1.5)
        options = await _visible_options(browser, element)
        best = max(options, key=lambda option: school_option_rank(school, option["text"]), default=None)
        if best is not None and school_option_rank(school, best["text"]) > 0 and await _click_option(browser, best["index"], element):
            await asyncio.sleep(0.5)
            return best["text"]
        await _press(browser, "Escape")
    return None


async def fill_education(browser, plan: dict[str, str], worker_id: int = 0) -> dict[str, Any]:
    """Fill or correct the first Education row from the candidate's facts; never deletes a row."""
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
    updates = education_field_updates(plan, row)

    if "school" in updates:
        if row.get("schoolSearch"):
            picked = await _pick_school_from_search(browser, row_id, plan["school"])
            if picked:
                filled.append("school")
        else:
            await _type(browser, _field(row_id, "schoolName", "input"), plan["school"])
            await asyncio.sleep(0.8)
            # Some tenants offer suggestions as you type; take the best match if a list opened.
            wanted = _norm(plan["school"])
            await _pick_option(browser, [plan["school"]], lambda text: 2 if _norm(text) == wanted else int(wanted in _norm(text)))
            filled.append("school")
    if "degree" in updates:
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
    if "gpa" in updates:
        if await _type(browser, _field(row_id, "gradeAverage", "input"), plan["gpa"]):
            filled.append("gpa")
    for field, key in (("firstYearAttended", "first_year"), ("lastYearAttended", "last_year")):
        if key in updates:
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


def phone_type_rank(option: str) -> int:
    """Phone Device Type is answered Mobile; anything else (Home, Work, "Select One") is unusable."""
    text = _norm(option)
    if text == "mobile":
        return 2
    return int(any(word in text for word in ("mobile", "cell")))


_PHONE_TYPE_BUTTON = (
    "Array.from(document.querySelectorAll('button[aria-haspopup=\"listbox\"]'))"
    ".find(b => /phone device type/i.test(b.getAttribute('aria-label') || ''))"
)


async def fill_phone_device_type(browser, worker_id: int = 0) -> bool:
    """Choose Mobile in an unanswered Phone Device Type dropdown (My Information)."""
    unanswered = await _eval(browser, f"""(() => {{
      const button = {_PHONE_TYPE_BUTTON};
      return !!button && /^select/i.test((button.innerText || '').trim());
    }})()""")
    if not unanswered or not await _click(browser, _PHONE_TYPE_BUTTON):
        return False
    await asyncio.sleep(0.6)
    if await _pick_option(browser, ["Mobile"], phone_type_rank):
        print(f"    📱 [W{worker_id}] Phone device type: Mobile")
        return True
    await _press(browser, "Escape")
    return False


# --- application questions ---------------------------------------------------

def _yes_no(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"yes", "true", "y", "1"}:
        return "Yes"
    if text in {"no", "false", "n", "0"}:
        return "No"
    return None


def dropdown_answer(question: str, facts: dict[str, Any]) -> str | None:
    """Answer a Workday Yes/No dropdown question from the candidate's facts, or None if it is
    not one we know (or the fact is missing), leaving it to the AI or the candidate."""
    text = _norm(question)
    if any(phrase in text for phrase in ("18 years", "over 18", "at least 18", "age of 18")):
        return _yes_no(facts.get("age_over_18"))
    if "high school" in text or re.search(r"\bged\b", text):
        return "Yes"
    # Sponsorship questions also say "to work legally in the United States": test them first.
    if "sponsorship" in text:
        return _yes_no(facts.get("visa_sponsorship_needed"))
    if any(phrase in text for phrase in ("legally authorized", "authorized to work", "eligible to work")):
        return _yes_no(facts.get("authorized_to_work_us"))
    if re.search(r"previously (?:been )?(?:worked|employed)", text) or "former employee" in text or "worked for us before" in text:
        return _yes_no(facts.get("previously_worked_for_company"))
    return None


_GENERIC_INTEREST = (
    "I am a computer science student looking for a software engineering internship where I can build "
    "reliable software with real users and learn from an experienced team."
)


def text_answer(question: str, facts: dict[str, Any]) -> str | None:
    text = _norm(question)
    if any(phrase in text for phrase in (
        "salary expectation", "salary range", "desired salary", "desired pay", "expected salary",
        "compensation expectation", "pay expectation",
    )):
        return str(facts.get("desired_pay") or "").strip() or None
    if any(phrase in text for phrase in ("available to start", "start date", "when can you start", "earliest start")):
        return str(facts.get("earliest_start_date") or "").strip() or None
    if "best way to contact" in text or "preferred method of contact" in text or "preferred contact" in text:
        return "Email"
    if any(phrase in text for phrase in (
        "looking for new opportunities", "why are you interested", "why do you want to work", "why this company",
        "why are you looking", "interest in this role",
    )):
        return str(facts.get("interest_statement") or "").strip() or _GENERIC_INTEREST
    return None


def option_rank(wanted: str, option: str) -> int:
    """Exact match of a dropdown option; a prefix ("Not sure" for "No") never counts."""
    return 2 if _norm(option) == _norm(wanted) else 0


_HEAR_UNSAFE = ("select one", "referral", "internal", "employee", "agency", "recruiter", "recruiting", "fair", "event")
_PHONE_CODE_ROW = re.compile(r"\(\+\d+\)")


def hear_pick(options: list[str], rng: random.Random | None = None) -> str | None:
    """Answer "How did you hear about us?": LinkedIn if offered, otherwise any harmless option.

    Referral, internal and agency options ask for a name, "Other" asks for text, and the stray
    "(+1)" rows belong to the Country Phone Code list, so none of those is ever chosen at random.
    """
    rng = rng or random.Random()
    real = [o for o in options if _norm(o) and _norm(o) != "select one" and not _PHONE_CODE_ROW.search(o)]
    for option in real:
        if "linkedin" in _norm(option):
            return option
    pool = [o for o in real if _norm(o) != "other" and not any(word in _norm(o) for word in _HEAR_UNSAFE)]
    if pool:
        return rng.choice(pool)
    return real[0] if real else None


def veteran_option_rank(fact: str, option: str) -> int:
    """Rank a Workday veteran-status option against the candidate's fact; 0 is never chosen.

    "I identify as ..." options claim service, so they are never picked for a non-veteran, and
    "I am not a protected veteran" / "I am not a veteran" are the answer.
    """
    wanted, text = _norm(fact), _norm(option)
    if "not a veteran" in wanted or "not a protected veteran" in wanted:
        if "identify as" in text:
            return 0
        if text.startswith("i am not a protected veteran") or text.startswith("i am not a veteran"):
            return 3
        return 2 if "not a protected veteran" in text or "not a veteran" in text else 0
    if any(word in wanted for word in ("prefer not", "decline", "do not wish", "don t wish")):
        return 3 if any(word in text for word in ("do not wish", "don t wish", "decline", "prefer not")) else 0
    return 0


def is_hear_question(question: str) -> bool:
    return "hear about" in _norm(question)


def dropdown_choice(question: str, facts: dict[str, Any]) -> Callable[[str], int] | None:
    """How to pick an option for a dropdown question: a ranking of option texts, or None if unknown."""
    text = _norm(question)
    if is_hear_question(question):
        return None  # picked with hear_pick, which needs the whole list at once
    if "veteran" in text and "have you" not in text:
        fact = str(facts.get("veteran_status") or "")
        return (lambda option: veteran_option_rank(fact, option)) if fact else None
    answer = dropdown_answer(question, facts)
    if answer:
        return lambda option: option_rank(answer, option)
    return None


_QUESTIONS_JS = r"""(() => {
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const selected = field => { const m = clean(field.querySelector('[data-automation-id="promptAriaInstruction"]')?.innerText).match(/(\d+)\s+items?\s+selected/i); return m ? Number(m[1]) : 0; };
  const out = [];
  for (const field of document.querySelectorAll('[data-automation-id^="formField-"]')) {
    const id = field.getAttribute('data-automation-id');
    const fkit = field.getAttribute('data-fkit-id') || '';
    if (/^(workExperience|education)-/.test(fkit) || field.closest('[data-fkit-id^="workExperience-"], [data-fkit-id^="education-"]')) continue;
    const question = clean((field.querySelector('legend') || field.querySelector('label'))?.innerText).replace(/\*\s*$/, '');
    const button = field.querySelector('button[aria-haspopup="listbox"]');
    if (button) {
      if (visible(button) && /^select/i.test(clean(button.innerText))) out.push({ kind: 'select', selector: '#' + CSS.escape(button.id), question });
      continue;
    }
    if (field.querySelector('[data-automation-id="multiSelectContainer"]')) {
      const input = field.querySelector('input');
      if (!input || !visible(input) || selected(field)) continue;
      out.push({ kind: id === 'formField-countryPhoneCode' ? 'phone_code' : 'hear_multi', selector: '#' + CSS.escape(input.id), question });
      continue;
    }
    const box = field.querySelector('textarea, input[type="text"]');
    if (box && visible(box) && !box.value && !box.readOnly && !box.disabled) {
      out.push({ kind: 'text', selector: '#' + CSS.escape(box.id), question });
    }
  }
  return out;
})()"""


def _phone_code_rank(text: str) -> int:
    # "United States of America (+1)": the +1 must be the country itself, not an island territory.
    norm = _norm(text)
    return 2 if norm.startswith("united states of america") and "1" in norm else 0


async def _open_and_pick(browser, element: str, choose: Callable[[list[str]], str | None]) -> str | None:
    """Open the widget, choose one of its options by text with `choose`, click it. Returns the text."""
    if not await _click(browser, element):
        return None
    await asyncio.sleep(0.7)
    options = await _visible_options(browser, element)
    picked = choose([option["text"] for option in options])
    if picked is not None:
        index = next((option["index"] for option in options if option["text"] == picked), None)
        if index is not None and await _click_option(browser, index, element):
            return picked
    await _press(browser, "Escape")
    return None


async def fill_workday_questions(browser, facts: dict[str, Any], worker_id: int = 0) -> int:
    """Answer blank Workday dropdown / short-answer questions and Country Phone Code from facts.

    The static pass cannot open Workday's listbox buttons (they need trusted clicks), so it
    reported these as blanks and the run fell to the AI even though the answers were known.
    """
    filled = 0
    for item in await _eval(browser, _QUESTIONS_JS) or []:
        element = f"document.querySelector({json.dumps(item['selector'])})"
        kind, question = item["kind"], item["question"]
        if kind == "select":
            if is_hear_question(question):
                picked = await _open_and_pick(browser, element, hear_pick)
            else:
                rank = dropdown_choice(question, facts)
                if rank is None:
                    continue
                picked = await _open_and_pick(
                    browser, element,
                    lambda texts: max(texts, key=rank, default=None) if texts and rank(max(texts, key=rank)) > 0 else None,
                )
            if picked:
                filled += 1
                print(f"    ✅ [W{worker_id}] {question[:60]}: {picked}")
        elif kind == "hear_multi":
            if not is_hear_question(question):
                continue
            picked = await _open_and_pick(browser, element, hear_pick)
            if picked:
                filled += 1
                await asyncio.sleep(0.4)
                print(f"    ✅ [W{worker_id}] {question[:60]}: {picked}")
        elif kind == "text":
            answer = text_answer(question, facts)
            if answer and await _type(browser, element, answer, bulk=True):
                filled += 1
                print(f"    ✅ [W{worker_id}] {question[:60]}: {answer[:40]}")
        elif kind == "phone_code" and str(facts.get("phone_country_code") or "+1").strip() == "+1":
            if await _type(browser, element, "United States of America"):
                await _press(browser, "Enter")  # the search box only filters on Enter
                await asyncio.sleep(1.5)
                chosen = await _eval(browser, f"""(() => {{
                  const t = ({element})?.closest('[data-automation-id="formField-countryPhoneCode"]')?.innerText || '';
                  return /united states of america/i.test(t);
                }})()""")
                if chosen or await _pick_option(browser, ["United States"], _phone_code_rank):
                    filled += 1
                    print(f"    ✅ [W{worker_id}] Country phone code: United States of America (+1)")
                else:
                    await _press(browser, "Escape")
    return filled
