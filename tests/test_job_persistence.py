import json
from pathlib import Path

from backend.core import shared_config


def test_atomic_upsert_preserves_terminal_status_and_original_timestamp(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    lock_file = tmp_path / "jobs.json.lock"
    jobs_file.write_text(json.dumps({
        "https://example.com/job": {
            "status": "applied",
            "title": "Old title",
            "collected_at": "2026-01-01T00:00:00+00:00",
        }
    }))
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", lock_file)

    merged, created = shared_config.upsert_job(
        "https://example.com/job",
        {
            "status": "blocked",
            "title": "Verified title",
            "collected_at": "2026-09-09T00:00:00+00:00",
        },
    )

    assert not created
    assert merged["status"] == "applied"
    assert merged["title"] == "Verified title"
    assert merged["collected_at"] == "2026-01-01T00:00:00+00:00"


def test_clear_stale_sessions_keeps_cookies(tmp_path, monkeypatch):
    profile = tmp_path / "browser_profile"
    sessions = profile / "Default" / "Sessions"
    sessions.mkdir(parents=True)
    (sessions / "Session_1").write_text("stale tab")
    cookies = profile / "Default" / "Cookies"
    cookies.write_text("login data")
    monkeypatch.setattr(shared_config, "BROWSER_PROFILE_DIR", profile)

    assert shared_config.clear_stale_browser_session_state() == 1
    assert not (sessions / "Session_1").exists()
    assert cookies.read_text() == "login data"


def test_bulk_updates_are_written_as_one_transaction(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    lock_file = tmp_path / "jobs.json.lock"
    jobs_file.write_text(json.dumps({"one": {"status": "pending"}, "two": {"status": "pending"}}))
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", lock_file)

    changed = shared_config.update_jobs_bulk({
        "one": {"status": "manual_review"},
        "two": {"status": "blocked"},
        "missing": {"status": "blocked"},
    })

    saved = json.loads(jobs_file.read_text())
    assert changed == 2
    assert saved["one"]["status"] == "manual_review"
    assert saved["two"]["status"] == "blocked"


def _jobs_in(tmp_path, monkeypatch, jobs):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps(jobs))
    monkeypatch.setattr(shared_config, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", tmp_path / "jobs.json.lock")
    return jobs_file


def test_marking_many_jobs_applied_is_one_write_and_stamps_the_date(tmp_path, monkeypatch):
    urls = [f"https://example.com/job/{i}" for i in range(130)]
    jobs_file = _jobs_in(tmp_path, monkeypatch, {
        url: {"status": "pending", "error": "old failure", "applied_at": None} for url in urls
    })

    result = shared_config.mark_jobs_status(urls, "applied", now="2026-09-20T12:00:00+00:00")

    saved = json.loads(jobs_file.read_text())
    assert result == {"updated": 130, "missing": []}
    assert {job["status"] for job in saved.values()} == {"applied"}
    assert {job["applied_at"] for job in saved.values()} == {"2026-09-20T12:00:00+00:00"}
    assert {job["error"] for job in saved.values()} == {None}


def test_an_earlier_applied_date_is_kept_and_unknown_urls_are_reported(tmp_path, monkeypatch):
    jobs_file = _jobs_in(tmp_path, monkeypatch, {
        "https://example.com/a": {"status": "applied", "applied_at": "2026-09-01T00:00:00+00:00"},
        "https://example.com/b": {"status": "failed", "applied_at": None},
    })

    result = shared_config.mark_jobs_status(
        ["https://example.com/a", "https://example.com/b", "https://example.com/gone"], "applied",
        now="2026-09-20T12:00:00+00:00",
    )

    saved = json.loads(jobs_file.read_text())
    assert result == {"updated": 2, "missing": ["https://example.com/gone"]}
    assert saved["https://example.com/a"]["applied_at"] == "2026-09-01T00:00:00+00:00"
    assert saved["https://example.com/b"]["applied_at"] == "2026-09-20T12:00:00+00:00"


def test_only_known_statuses_can_be_set_in_bulk(tmp_path, monkeypatch):
    jobs_file = _jobs_in(tmp_path, monkeypatch, {"https://example.com/a": {"status": "pending"}})

    try:
        shared_config.mark_jobs_status(["https://example.com/a"], "in_progress")
    except ValueError:
        pass
    else:
        raise AssertionError("in_progress is a worker-owned state and must be refused")

    assert json.loads(jobs_file.read_text())["https://example.com/a"]["status"] == "pending"
