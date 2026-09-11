"""Shared config, utilities, and credential management."""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from filelock import FileLock
from browser_use.llm import ChatAWSBedrock

try:
    from core.config import get_data_dir, load_llm_settings
    from core.llm_factory import create_llm
    from memory import MemoryStore
except ImportError:
    from backend.core.config import get_data_dir, load_llm_settings
    from backend.core.llm_factory import create_llm
    from backend.memory import MemoryStore

_SOURCE_DIR = Path(__file__).resolve().parent.parent.parent  # project root (or temp dir if frozen)

DATA_DIR = get_data_dir()

# When frozen (PyInstaller), BASE_DIR would be a temp dir — use DATA_DIR instead
BASE_DIR = DATA_DIR if getattr(sys, 'frozen', False) else _SOURCE_DIR

# Use OS data dir for settings/profile (written by UI), project root for jobs/logs
JOBS_FILE = DATA_DIR / "jobs.json"
JOBS_LOCK = DATA_DIR / "jobs.json.lock"
QA_FILE = DATA_DIR / "qa_repository.json"
CANDIDATE_PROFILE = DATA_DIR / "candidate_profile.json"
LOGS_DIR = BASE_DIR / "logs"
RESUMES_DIR = BASE_DIR / "resumes"

# Browser profile ALWAYS in OS data dir (must match backend/main.py login endpoint)
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"


def clear_stale_browser_session_state() -> int:
    """Remove restorable tab state without touching cookies or login data.

    The profile belongs only to LangHire.  Clearing its saved tabs prevents a
    crashed run from reopening dozens of job pages on the next collection.
    Callers must ensure no LangHire browser is currently using the profile.
    """
    candidates = [
        BROWSER_PROFILE_DIR / "Default" / "Current Session",
        BROWSER_PROFILE_DIR / "Default" / "Current Tabs",
        BROWSER_PROFILE_DIR / "Default" / "Last Session",
        BROWSER_PROFILE_DIR / "Default" / "Last Tabs",
    ]
    sessions_dir = BROWSER_PROFILE_DIR / "Default" / "Sessions"
    if sessions_dir.is_dir():
        candidates.extend(path for path in sessions_dir.iterdir() if path.is_file())
    removed = 0
    for path in candidates:
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def find_brave_browser() -> str | None:
    """Return the installed Brave Browser executable, if present.

    Automation still uses LangHire's own isolated BROWSER_PROFILE_DIR, not the
    user's real Brave profile — this only swaps which Chromium binary renders
    the window so it looks/feels like the user's actual browser.
    """
    candidates: list[Path]
    if sys.platform == "darwin":
        candidates = [Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")]
    elif sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates = [
            Path(local_app) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            Path(program_files) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            Path(program_files_x86) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        ]
    else:
        candidates = [
            Path("/usr/bin/brave-browser"),
            Path("/usr/bin/brave-browser-stable"),
            Path("/opt/brave.com/brave/brave"),
            Path("/snap/bin/brave"),
        ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def find_playwright_chromium() -> str | None:
    """Return the newest usable Playwright Chromium executable, if installed.

    Browser-use does not currently recognize Playwright's newer Apple-silicon
    ``Google Chrome for Testing`` cache layout, so LangHire resolves it before
    constructing a BrowserSession.
    """
    cache_dirs: list[Path] = []
    if sys.platform == "darwin":
        cache_dirs.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    elif sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        cache_dirs.append(Path(local_app) / "ms-playwright")
    cache_dirs.append(Path.home() / ".cache" / "ms-playwright")

    try:
        import playwright

        bundled = Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
        if bundled.exists():
            cache_dirs.insert(0, bundled)
    except (ImportError, AttributeError, TypeError):
        pass

    platform_patterns = {
        "darwin": (
            "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
            "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        ),
        "win32": ("chrome-win64/chrome.exe", "chrome-win/chrome.exe"),
        "linux": ("chrome-linux64/chrome", "chrome-linux/chrome"),
    }
    patterns = platform_patterns.get(sys.platform, platform_patterns["linux"])
    for cache_dir in cache_dirs:
        if not cache_dir.exists():
            continue
        for chromium_dir in sorted(cache_dir.glob("chromium-*"), reverse=True):
            for pattern in patterns:
                executable = chromium_dir / pattern
                if executable.is_file():
                    return str(executable)
    return None


def resolve_browser_executable() -> str | None:
    """Pick the browser binary used for automation and for manual logins.

    ``LANGHIRE_BROWSER_PATH`` overrides everything, then an installed Brave,
    then a Playwright-managed Chromium. Both paths must agree: the automation
    profile can only be opened by the browser that created it, so a login saved
    by one binary is invisible to the other.
    """
    override = (os.environ.get("LANGHIRE_BROWSER_PATH") or "").strip()
    if override and Path(override).is_file():
        return override
    return find_brave_browser() or find_playwright_chromium()


def browser_session_kwargs() -> dict:
    """Shared local BrowserSession launch options."""
    # browser-use normally suppresses window focus so unattended agents do not
    # steal the desktop. LangHire intentionally hands one protected Workday
    # click to the user, so its automation window must remain focusable.
    try:
        from browser_use.browser.profile import BrowserProfile

        default_ignored_args = list(
            BrowserProfile.model_fields["ignore_default_args"].default_factory()
        )
    except Exception:
        default_ignored_args = ["--enable-automation", "--disable-extensions", "--hide-scrollbars"]
    kwargs = {
        "user_data_dir": str(BROWSER_PROFILE_DIR),
        "headless": False,
        "chromium_sandbox": sys.platform != "linux",
        "ignore_default_args": [
            *default_ignored_args,
            "--disable-focus-on-load",
            "--disable-window-activation",
        ],
        # The bundled anti-popup/cookie extensions add enough startup latency
        # on macOS to exceed browser-use's fixed 30-second CDP timeout. They are
        # not required for LangHire's DOM-based form filling.
        "enable_default_extensions": False,
    }
    executable = resolve_browser_executable()
    if executable and "brave" in Path(executable).name.lower():
        # Brave's startup overhead (Shields, Wallet, Rewards init) exceeds
        # browser-use's fixed 30s launch timeouts; both are overridable via
        # env var (browser_use/browser/events.py: _get_timeout).
        os.environ.setdefault("TIMEOUT_BrowserStartEvent", "90")
        os.environ.setdefault("TIMEOUT_BrowserLaunchEvent", "90")
    if executable:
        kwargs["executable_path"] = executable
    return kwargs

AWS_PROFILE = "default"
AWS_REGION = "us-west-2"
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
ADA_CMD: tuple[str, ...] = ()  # Only needed for Amazon internal credential refresh

# Load settings from OS data dir (written by desktop app UI) if available
_settings_file = DATA_DIR / "settings.json"
_ui_settings = json.loads(_settings_file.read_text()) if _settings_file.exists() else {}
SENSITIVE_DATA = _ui_settings.get("sensitive_data", {"email": "", "password": ""})

# Fall back to profile email if sensitive_data email is blank
if not SENSITIVE_DATA.get("email", "").strip():
    _profile_file = DATA_DIR / "candidate_profile.json"
    if _profile_file.exists():
        _profile_data = json.loads(_profile_file.read_text())
        _profile_email = _profile_data.get("email", "").strip()
        if _profile_email:
            SENSITIVE_DATA["email"] = _profile_email

RESUME_PATH = _ui_settings.get("resume_path", "")
BLOCKED_DOMAINS = _ui_settings.get("blocked_domains", ["meeboss.com"])

_PRIVATE_IP_PREFIXES = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                        "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                        "172.29.", "172.30.", "172.31.", "0.", "169.254.")

def validate_job_url(url: str) -> bool:
    """Reject URLs pointing to private/internal networks (SSRF prevention)."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host or not parsed.scheme.startswith("http"):
            return False
        if host in ("localhost", "0.0.0.0", "[::]", "[::1]"):
            return False
        if any(host.startswith(p) for p in _PRIVATE_IP_PREFIXES):
            return False
        return True
    except Exception:
        return False

# ── Singleton memory store ────────────────────────────────────────────────────
_memory_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """Get or create the singleton memory store instance."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store


def load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default if default is not None else []


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_jobs() -> dict:
    """Read jobs.json with cross-process lock."""
    with FileLock(JOBS_LOCK):
        return load_json(JOBS_FILE, {})


def write_jobs(jobs: dict):
    """Write jobs.json with cross-process lock."""
    with FileLock(JOBS_LOCK):
        save_json(JOBS_FILE, jobs)


def update_job(url: str, **fields):
    """Atomically update a single job entry."""
    with FileLock(JOBS_LOCK):
        jobs = load_json(JOBS_FILE, {})
        if url in jobs:
            jobs[url].update(fields)
            save_json(JOBS_FILE, jobs)


def upsert_job(url: str, fields: dict, *, preserve_status: bool = True) -> tuple[dict, bool]:
    """Atomically insert or merge one job and return ``(job, created)``.

    Collector code used to lock the read and write separately, which allowed a
    concurrent apply worker to be overwritten between those two operations.
    Keeping the complete read/merge/write transaction under one file lock also
    avoids downgrading an already-applied job during metadata refreshes.
    """
    with FileLock(JOBS_LOCK):
        jobs = load_json(JOBS_FILE, {})
        existing = jobs.get(url, {})
        created = url not in jobs
        merged = {**existing, **fields, "url": url}
        if existing.get("collected_at"):
            merged["collected_at"] = existing["collected_at"]
        if preserve_status and existing.get("status") in {"applied", "in_progress"}:
            merged["status"] = existing["status"]
        jobs[url] = merged
        save_json(JOBS_FILE, jobs)
        return dict(merged), created


def update_jobs_bulk(updates: dict[str, dict]) -> int:
    """Atomically apply field updates to multiple existing jobs."""
    if not updates:
        return 0
    with FileLock(JOBS_LOCK):
        jobs = load_json(JOBS_FILE, {})
        changed = 0
        for url, fields in updates.items():
            if url in jobs:
                jobs[url].update(fields)
                changed += 1
        if changed:
            save_json(JOBS_FILE, jobs)
        return changed


_claim_lock = threading.Lock()


def claim_job(url: str) -> bool:
    """Atomically claim a pending job. Returns True if claimed, False if already taken.
    Uses both FileLock (cross-process) and threading lock (in-process workers)."""
    with _claim_lock:
        with FileLock(JOBS_LOCK):
            jobs = load_json(JOBS_FILE, {})
            if url in jobs and jobs[url].get("status") == "pending":
                jobs[url]["status"] = "in_progress"
                save_json(JOBS_FILE, jobs)
                return True
            return False


def refresh_credentials():
    """Run ada credentials update and return True on success."""
    if not ADA_CMD:
        # No credential refresh command configured — skip silently
        # Users should configure AWS credentials via the Settings UI or aws cli
        return True
    print("🔑 Refreshing AWS credentials...")
    try:
        subprocess.run(ADA_CMD, check=True, timeout=30)
        print("✅ Credentials refreshed")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"❌ Credential refresh failed: {e}", file=sys.stderr)
        return False


async def credential_refresh_loop(interval_minutes: int = 14):
    """Background task that refreshes credentials on a timer. Cancels with parent."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        refresh_credentials()


def get_llm():
    """Create a fresh LLM client from the app's current LLM settings."""
    settings = load_llm_settings()
    provider = (settings.get("provider") or "").strip().lower()
    if provider:
        return create_llm(settings)

    # Legacy fallback for older installs that never saved LLM settings.
    import boto3
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return ChatAWSBedrock(model=MODEL_ID, session=session)


def normalize_question(q: str) -> str:
    return re.sub(r"[^\w\s]", "", q.lower()).strip()


def build_memory_context(
    profile: dict,
    qa: dict,
    applied_labels: list[str] | None = None,
    job_url: str | None = None,
) -> str:
    """Build the system message context with candidate profile, Q&A bank, and per-website learnings."""
    parts = []

    # Salary formatting (country-aware)
    sal = profile.get('salary_expectation', {})
    sal_currency = sal.get('currency', 'USD') or 'USD'
    sal_min = sal.get('min', 0) or 0
    sal_max = sal.get('max', 0) or 0
    sal_period = sal.get('period', 'annual')
    salary_str = f"{sal_currency} {sal_min:,}-{sal_max:,} ({sal_period})" if sal_min else "Not specified"

    # Country-aware date format
    date_format = profile.get('date_format', '') or 'MM/DD/YYYY'

    profile_lines = [
        "CANDIDATE PROFILE:",
        f"Name: {profile['name']}",
        f"Email: {profile['email']}, Phone: {profile.get('phone_country_code', '')}{profile['phone']}",
        f"Location: {profile['address']['city']}, {profile['address']['state']} {profile['address']['zip']} {profile['address'].get('country', '')}".strip(),
        f"Work Authorization: {profile['work_authorization']}, Visa Sponsorship Needed: {profile['visa_sponsorship_needed']}",
        f"Willing to Relocate: {profile['willing_to_relocate']}, Preferred Work Mode: {profile['preferred_work_mode']}",
        f"Years of Experience: {profile['years_of_experience']}",
        f"Education: {profile['education']['degree']} from {profile['education']['school']} ({profile['education']['graduation']})",
        f"Current Role: {profile['current_role']}",
        f"Target Locations: {', '.join(profile['target_locations'])}",
        f"Languages: {', '.join(profile['languages'])}",
        f"Skills: {', '.join(profile['skills'])}",
        f"Salary: {salary_str}",
    ]
    if profile.get('notice_period'):
        profile_lines.append(f"Notice Period: {profile['notice_period']}")
    if profile.get('nationality'):
        profile_lines.append(f"Nationality: {profile['nationality']}")
    if profile.get('notes'):
        profile_lines.append(f"Notes: {profile['notes']}")

    parts.append("\n".join(profile_lines))

    # Country-specific instructions for the agent
    country_instructions = []
    country_instructions.append(f"DATE FORMAT: When filling date fields, use {date_format} format.")
    if profile.get('notice_period'):
        country_instructions.append(f"NOTICE PERIOD: If asked about notice period or availability, answer: {profile['notice_period']}")
    if profile.get('nationality'):
        country_instructions.append(f"NATIONALITY: If asked about nationality, answer: {profile['nationality']}")
    if profile.get('cover_letter'):
        country_instructions.append(f"COVER LETTER: If a cover letter is requested, use:\n{profile['cover_letter']}")
    if country_instructions:
        parts.append("COUNTRY-SPECIFIC INSTRUCTIONS:\n" + "\n".join(country_instructions))

    if applied_labels:
        parts.append("Already applied — SKIP:\n" + "\n".join(f"- {j}" for j in applied_labels))

    # Try SQLite Q&A first, fall back to passed-in dict
    qa_for_prompt = qa
    try:
        store = get_memory_store()
        if store:
            db_qa = store.qa_get_all_for_prompt()
            if db_qa:
                qa_for_prompt = db_qa
    except Exception:
        pass
    if qa_for_prompt:
        qa_list = "\n".join(f'Q: {q}\nA: {a}' for q, a in qa_for_prompt.items() if a)
        if qa_list:
            parts.append(f"Pre-filled answers for application questions:\n{qa_list}")

    # ── Per-website memory injection ──────────────────────────────────────
    if job_url:
        store = get_memory_store()
        memories = store.get_domain_memories(job_url, limit=20)
        if memories:
            domain = store.extract_domain(job_url)
            mem_count = len(memories)
            print(f"    🧠 Injecting {mem_count} memories for {domain}")
            parts.append(store.format_for_prompt(memories))

    parts.append(
        "TRACKING INSTRUCTIONS:\n"
        "After each successful application: @@JOB_APPLIED: {\"title\": \"...\", \"company\": \"...\", \"location\": \"...\"}\n"
        "For each form question encountered: @@QUESTION: {\"question\": \"...\", \"answer\": \"...\", \"type\": \"text|dropdown|radio|checkbox\"}\n\n"
        "SELF-LEARNING — report observations about THIS WEBSITE's UI/flow as you navigate:\n"
        "@@LEARNING: {\"domain\": \"<website domain>\", \"category\": \"navigation|form_strategy|element_interaction|failure_recovery|site_structure|qa_pattern\", \"insight\": \"<specific actionable observation>\"}\n"
        "Examples of good learnings:\n"
        "- Navigation: 'Easy Apply opens a modal overlay, don't navigate away from the page'\n"
        "- Element interaction: 'The checkbox is inside a scrollable div, must scroll to find it'\n"
        "- Form strategy: 'This ATS splits the form into 4 steps: Personal → Resume → Questions → Review'\n"
        "Report at least 2-3 learnings per application run."
    )
    return "\n\n".join(parts)


def extract_from_history(result):
    """Extract applied jobs and questions from agent history."""
    jobs, questions, seen = [], {}, set()
    for item in result.history:
        if not item.model_output:
            continue
        memory = item.model_output.memory or ""
        for m in re.finditer(r"@@JOB_APPLIED:\s*(\{[^}]{1,2000}\})", memory):
            try:
                j = json.loads(m.group(1))
                jobs.append(f"{j.get('title','')} at {j.get('company','')} - {j.get('location','')}")
            except json.JSONDecodeError:
                pass
        for m in re.finditer(r"@@QUESTION:\s*(\{[^}]{1,2000}\})", memory):
            try:
                q = json.loads(m.group(1))
                qtext, ans = q.get("question", "").strip(), q.get("answer", "").strip()
                norm = normalize_question(qtext)
                if qtext and norm not in seen:
                    seen.add(norm)
                    questions[qtext] = ans
            except json.JSONDecodeError:
                pass
        # Fallback
        if not jobs and any(kw in memory.lower() for kw in ["application submitted", "successfully applied"]):
            for pat in [r"applied to (.+?) via", r"Application submitted for (.+?) via"]:
                match = re.search(pat, memory, re.IGNORECASE)
                if match:
                    jobs.append(match.group(1).strip())
                    break
    return list(dict.fromkeys(jobs)), questions
