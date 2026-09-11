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
