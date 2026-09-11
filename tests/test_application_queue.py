from backend.application_queue import preparable_statuses, select_preparable_jobs


def test_review_mode_accepts_jobs_waiting_for_manual_review():
    jobs = {
        "pending": {"status": "pending"},
        "review": {"status": "manual_review"},
        "blocked": {"status": "blocked"},
    }

    selected = select_preparable_jobs(
        jobs,
        ["review", "blocked", "pending", "review"],
        "review",
    )

    assert [url for url, _job in selected] == ["review", "pending"]


def test_non_review_modes_keep_manual_review_jobs_out_of_automatic_runs():
    jobs = {
        "failed": {"status": "failed"},
        "review": {"status": "manual_review"},
    }

    assert preparable_statuses("all") == frozenset({"pending", "failed"})
    assert [url for url, _job in select_preparable_jobs(jobs, jobs, "all")] == ["failed"]


def test_fapply_mode_can_retry_review_jobs_without_touching_applied_jobs():
    jobs = {
        "pending": {"status": "pending"},
        "review": {"status": "manual_review"},
        "applied": {"status": "applied"},
    }

    assert preparable_statuses("fapply") == frozenset({"pending", "failed", "manual_review"})
    selected = select_preparable_jobs(jobs, jobs, "fapply")
    assert [url for url, _job in selected] == ["pending", "review"]
