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
from urllib.parse import quote

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import BrowserSession

try:
    import core.shared_config as config
    from core.shared_config import (
        JOBS_FILE, CANDIDATE_PROFILE, LOGS_DIR, BASE_DIR, BROWSER_PROFILE_DIR,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        read_jobs, write_jobs, update_job,
    )
    from core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
except ImportError:
    import backend.core.shared_config as config
    from backend.core.shared_config import (
        JOBS_FILE, CANDIDATE_PROFILE, LOGS_DIR, BASE_DIR, BROWSER_PROFILE_DIR,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        read_jobs, write_jobs, update_job,
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
    r"\b(no|not|unable to|cannot|can't|will not|won't|does not|do not)\s+(offer\s+)?(provide\s+)?(visa\s+)?sponsor",
    r"\bnot eligible for (visa|employment) sponsorship\b",
    r"\bwithout (current or future )?(visa|employer|employment) sponsorship\b",
    r"\bauthorized(?:\s+\w+){0,8}\s+without\s+.*sponsorship\b",
    r"\bmust be (legally )?authorized to work .* without .*sponsorship\b",
    r"\brequires? unrestricted work authorization\b",
]


def _has_location_hint(text: str, hint: str) -> bool:
    """Match location hints as words/phrases, not arbitrary substrings."""
    return re.search(rf"(^|[^a-z]){re.escape(hint.lower())}([^a-z]|$)", text) is not None


def _build_search_url(title: str, profile: dict, filters: dict | None = None) -> str:
    """Build a LinkedIn search URL with the UI filters applied."""
    locations = profile.get("target_locations") or ["United States"]
    location = ", ".join(locations)
    search_url = f"https://www.linkedin.com/jobs/search/?keywords={quote(title)}&location={quote(location)}"

    if filters:
        linkedin_filter_params = {
            "date_posted": "f_TPR",
            "experience_level": "f_E",
            "work_type": "f_WT",
            "job_type": "f_JT",
        }
        for key, value in filters.items():
            if value and key in linkedin_filter_params:
                search_url += f"&{linkedin_filter_params[key]}={quote(str(value))}"
    else:
        search_url += "&f_TPR=r604800"

    return search_url


def _linkedin_job_id(url: str) -> str:
    match = re.search(r"(?:/jobs/view/|currentJobId=)(\d+)", url or "")
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


def _is_us_location(location: str, profile: dict) -> bool:
    """Reject clearly non-US locations when the profile is US-targeted."""
    target_country = (profile.get("country") or profile.get("address", {}).get("country") or "US").upper()
    if target_country not in {"US", "USA", "UNITED STATES"}:
        return True

    location_l = (location or "").lower()
    if not location_l:
        return True
    explicit_us = any(_has_location_hint(location_l, hint) for hint in ("united states", "usa", "u.s.", "us"))
    if any(_has_location_hint(location_l, hint) for hint in _CLEAR_NON_US_LOCATION_HINTS) and not explicit_us:
        return False
    if any(_has_location_hint(location_l, hint) for hint in _US_STATE_HINTS):
        return True
    if any(_has_location_hint(location_l, abbr) for abbr in _US_STATE_ABBREVIATIONS):
        return True
    return True


def _matches_authorization_constraints(text: str, profile: dict) -> tuple[bool, str]:
    """Skip no-sponsorship roles only when the candidate profile says sponsorship is needed."""
    needs_sponsorship = bool(profile.get("visa_sponsorship_needed"))
    if not needs_sponsorship:
        return True, ""

    text_l = (text or "").lower()
    for pattern in _NO_SPONSORSHIP_PATTERNS:
        if re.search(pattern, text_l):
            return False, "Filtered out: posting says no visa sponsorship"
    return True, ""


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


async def _wait_for_linkedin_login(page, target_url: str, timeout: float = 300.0) -> bool:
    """Navigate to LinkedIn and wait for manual login if needed."""
    await page.goto(target_url)
    deadline = asyncio.get_running_loop().time() + timeout
    prompted = False
    while True:
        state = await _wait_for_ready(page, timeout=8)
        page_url = state.get("url", "")
        text = state.get("text", "").lower()
        logged_out = (
            "/login" in page_url
            or "/uas/login" in page_url
            or ("sign in" in text and "email or phone" in text)
            or ("join linkedin" in text and "sign in" in text)
        )
        if not logged_out:
            return True
        if not prompted:
            print("    🔐 LinkedIn login required. Log in in the browser window; collector will continue automatically.")
            prompted = True
        if asyncio.get_running_loop().time() >= deadline:
            print("    ❌ LinkedIn login was not completed in time")
            return False
        await asyncio.sleep(5)
        try:
            await page.goto(target_url)
        except Exception:
            pass


_EXTRACT_CARDS_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const jobIdFromHref = (href) => {
    const m = String(href || '').match(/\/jobs\/view\/(\d+)/) || String(href || '').match(/[?&]currentJobId=(\d+)/);
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
    const id = el.getAttribute('data-occludable-job-id')
      || el.getAttribute('data-job-id')
      || jobIdFromHref(anchor?.href || '');
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
    const m = String(href || '').match(/\/jobs\/view\/(\d+)/) || String(href || '').match(/[?&]currentJobId=(\d+)/);
    return m ? m[1] : '';
  };
  const all = Array.from(document.querySelectorAll(
    'li[data-occludable-job-id], li.jobs-search-results__list-item, div.job-card-container[data-job-id], div[data-job-id], .job-card-container'
  ));
  const el = all.find((node) => {
    const anchor = node.querySelector('a[href*="/jobs/view/"]');
    return node.getAttribute('data-occludable-job-id') === jobId
      || node.getAttribute('data-job-id') === jobId
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
  const idMatch = pageUrl.match(/\/jobs\/view\/(\d+)/) || pageUrl.match(/[?&]currentJobId=(\d+)/);
  const id = (idMatch && idMatch[1]) || expectedId || '';
  const root = document.querySelector('.jobs-search__job-details--container, .jobs-details__main-content, .job-view-layout, main, .jobs-unified-top-card')
    || document.body;
  const pickText = (selectors) => {
    for (const selector of selectors) {
      const el = root.querySelector(selector) || document.querySelector(selector);
      const text = clean(el?.innerText || el?.textContent || '');
      if (text) return text;
    }
    return '';
  };
  const buttonTexts = Array.from(root.querySelectorAll('button, a'))
    .map((el) => clean(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
    .filter(Boolean);
  const descriptionEl = root.querySelector('.jobs-description__content, .jobs-box__html-content, #job-details, [class*="jobs-description"], [class*="description"]')
    || document.querySelector('.jobs-description__content, .jobs-box__html-content, #job-details, [class*="jobs-description"], [class*="description"]');
  const bodyText = clean(document.body?.innerText || '');
  let description = clean(descriptionEl?.innerText || '');
  if (!description) {
    const aboutIndex = bodyText.search(/About the job/i);
    if (aboutIndex >= 0) description = bodyText.slice(aboutIndex);
  }
  const easy = buttonTexts.some((t) => /^easy apply$/i.test(t) || /easy apply/i.test(t));
  const hasApply = easy || buttonTexts.some((t) => /^(apply|apply now|apply on company site|continue to apply)$/i.test(t));
  return JSON.stringify({
    id,
    url: id ? `https://www.linkedin.com/jobs/view/${id}/` : pageUrl,
    page_url: pageUrl,
    title: pickText(['h1', '.job-details-jobs-unified-top-card__job-title', '.jobs-unified-top-card__job-title', '[class*="job-title"]']),
    company: pickText(['.job-details-jobs-unified-top-card__company-name', '.jobs-unified-top-card__company-name', '[class*="company-name"]']),
    location: pickText(['.job-details-jobs-unified-top-card__primary-description-container .tvm__text', '.jobs-unified-top-card__bullet', '[class*="job-location"]', '[class*="location"]']),
    easy_apply: easy ? true : (hasApply ? false : null),
    description: description.slice(0, 20000),
    button_texts: buttonTexts.slice(0, 30)
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


async def _scroll_results(page) -> dict:
    return _parse_eval_json(await page.evaluate(_SCROLL_RESULTS_JS), {})


async def _go_to_next_results_page(page) -> dict:
    return _parse_eval_json(await page.evaluate(_GO_TO_NEXT_RESULTS_PAGE_JS), {})


def _has_usable_card_metadata(job: dict) -> bool:
    title = (job.get("title") or "").strip().lower()
    company = (job.get("company") or "").strip().lower()
    return bool(title and title != "unknown") or bool(company and company != "unknown")


async def collect_for_title(title: str, existing_jobs: dict, profile: dict, max_jobs: int = 0, filters: dict | None = None) -> list[dict]:
    """Deterministically collect LinkedIn listings for one title.

    This intentionally avoids an LLM browser agent. The old collector could click
    Apply/Easy Apply or wander into company pages before saving the job. This
    version extracts LinkedIn job IDs from the DOM, saves immediately, then
    refines metadata from the details panel when possible.
    """
    refresh_credentials()
    _agent_log_start("collect", title)

    browser = BrowserSession(**config.browser_session_kwargs())
    search_url = _build_search_url(title, profile, filters)
    seen_urls = set(existing_jobs.keys())
    found_urls: set[str] = set()
    found: list[dict] = []

    def upsert_job(raw_job: dict, provisional: bool = False) -> bool:
        job = _clean_job(raw_job, title)
        if not job:
            return False
        url = job["url"]
        text_for_filters = " ".join(
            str(job.get(k, "")) for k in ("title", "company", "location", "description")
        )
        if not _is_us_location(job.get("location", ""), profile):
            if url in read_jobs():
                update_job(url, status="blocked", error=f"Filtered out: non-US location ({job.get('location', '')})")
            print(f"    ⏭️  Skipped non-US location: {job['title']} — {job.get('location', '')}")
            return False
        ok_auth, reason = _matches_authorization_constraints(text_for_filters, profile)
        if not ok_auth:
            if url in read_jobs():
                update_job(url, status="blocked", error=reason)
            print(f"    ⏭️  {reason}: {job['title']} at {job['company']}")
            return False

        jobs = read_jobs()
        is_new = url not in jobs
        existing = jobs.get(url, {})
        now = datetime.now(timezone.utc).isoformat()
        merged = {
            **existing,
            **{k: v for k, v in job.items() if v not in ("", None, [])},
            "url": url,
            "search_title": existing.get("search_title") or title,
            "status": existing.get("status") or "pending",
            "collected_at": existing.get("collected_at") or now,
            "applied_at": existing.get("applied_at"),
            "error": existing.get("error"),
            "collection_method": "deterministic_linkedin_dom",
        }
        if "easy_apply" not in merged:
            merged["easy_apply"] = None
        jobs[url] = merged
        write_jobs(jobs)

        seen_urls.add(url)
        if is_new and url not in found_urls:
            found_urls.add(url)
            found.append(merged)
            print(f"    💾 Saved 1 new job (total this title: {len(found)}) — {merged['title']} at {merged['company']}")
        elif not provisional:
            print(f"    🧾 Refined job details — {merged.get('title', 'Unknown')} at {merged.get('company', 'Unknown')}")
        return is_new

    try:
        await browser.start()
        page = await browser.new_page("about:blank")
        print(f"    🔎 Opening LinkedIn search: {title}")
        if not await _wait_for_linkedin_login(page, search_url):
            return found

        max_scroll_rounds = 60 if max_jobs <= 0 else max(8, min(60, max_jobs * 4))
        max_result_pages = 20 if max_jobs <= 0 else max(2, min(20, (max_jobs // 25) + 4))
        result_page = 1
        stale_rounds = 0
        processed_ids: set[str] = set()

        for round_idx in range(max_scroll_rounds):
            await _wait_for_ready(page, timeout=10)
            cards = await _extract_cards(page)
            new_cards = [c for c in cards if c.get("id") and c["id"] not in processed_ids]
            if new_cards:
                print(f"    📄 Found {len(new_cards)} visible unprocessed cards (scroll {round_idx + 1})")
                stale_rounds = 0
            else:
                stale_rounds += 1

            for card in new_cards:
                job_id = str(card.get("id") or "")
                processed_ids.add(job_id)
                card_url = _normalize_linkedin_job_url(card.get("url", ""), job_id)
                if not card_url or card_url in seen_urls:
                    continue

                # Save before clicking only when the card has useful metadata.
                # Some virtualized LinkedIn rows expose only an ID until selected;
                # saving those immediately creates junk "Unknown at Unknown" rows.
                if _has_usable_card_metadata(card):
                    upsert_job(card, provisional=True)

                try:
                    await page.evaluate(_CLICK_CARD_JS, job_id)
                    await asyncio.sleep(1.2)
                    details = await _extract_details(page, job_id)
                    details_id = str(details.get("id") or "")
                    if details_id and details_id != job_id:
                        upsert_job(details)
                        continue
                    merged = {**card, **{k: v for k, v in details.items() if v not in ("", None, [])}}
                    upsert_job(merged)
                except Exception as e:
                    if _has_usable_card_metadata(card):
                        print(f"    ⚠️  Could not refine card {job_id}; kept card metadata. Reason: {e}")
                        upsert_job(card)
                    else:
                        print(f"    ⚠️  Skipped card {job_id}: no metadata and details failed. Reason: {e}")

                if max_jobs > 0 and len(found) >= max_jobs:
                    print(f"    ✅ Reached {max_jobs} jobs — stopping collection for this title")
                    return found

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
            await browser.close()
        except Exception as close_err:
            print(f"    ⚠️  Browser cleanup error: {close_err}")

    return found


async def fetch_description_for_job(url: str, job: dict) -> str:
    """Visit a LinkedIn job page and extract the description without an LLM."""
    refresh_credentials()
    browser = BrowserSession(**config.browser_session_kwargs())

    title = job.get("title", "Unknown")
    company = job.get("company", "unknown")

    try:
        await browser.start()
        page = await browser.new_page("about:blank")
        print(f"    🔎 Reading LinkedIn description without clicking Apply: {title} at {company}")
        if not await _wait_for_linkedin_login(page, url, timeout=120):
            return ""
        await asyncio.sleep(1.0)
        details = await _extract_details(page, _linkedin_job_id(url))
        description = (details.get("description") or "").strip()
        updates = {}
        for key in ("title", "company", "location", "easy_apply"):
            value = details.get(key)
            if value not in ("", None, []):
                updates[key] = value
        if updates:
            update_job(_normalize_linkedin_job_url(url), **updates)
        return description
    finally:
        try:
            await browser.close()
        except Exception as close_err:
            print(f"    ⚠️  Browser cleanup error: {close_err}")


async def collect_descriptions(jobs: dict):
    """Phase 2: Fetch descriptions for all jobs that don't have one yet."""
    needs_desc = [
        (url, j) for url, j in jobs.items()
        if j.get("status") == "pending" and not j.get("description")
    ]

    if not needs_desc:
        print("All jobs already have descriptions.")
        return

    print(f"\n📋 Fetching descriptions for {len(needs_desc)} jobs...\n")

    for i, (url, job) in enumerate(needs_desc):
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        print(f"  [{i+1}/{len(needs_desc)}] {title} at {company}...")

        for attempt in range(2):
            try:
                description = await asyncio.wait_for(fetch_description_for_job(url, job), timeout=30)
                if description:
                    update_job(url, description=description)
                    print(f"    ✅ Got description ({len(description)} chars)")
                else:
                    print(f"    ⚠️  No description extracted")
                break
            except TimeoutError:
                print("    ⚠️  Description fetch timed out; keeping job without description")
                break
            except Exception as e:
                error_str = str(e).lower()
                if "security token" in error_str or "expired" in error_str:
                    print(f"    🔑 Credentials expired — refreshing...")
                    refresh_credentials()
                    if attempt < 1:
                        continue
                print(f"    ❌ Error: {e}")
                break


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
        await collect_descriptions(jobs)
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
