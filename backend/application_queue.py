"""Rules for selecting jobs for an application run."""

from collections.abc import Iterable, Mapping


def preparable_statuses(mode: str) -> frozenset[str]:
    """Return the job statuses accepted by a targeted application run.

    Review mode never submits an application, so a job awaiting manual review
    can safely be opened and prepared. Other modes retain the stricter legacy
    behavior and only accept pending jobs or explicit retries.
    """
    statuses = {"pending", "failed"}
    if mode in {"review", "fapply"}:
        statuses.add("manual_review")
    return frozenset(statuses)


def select_preparable_jobs(
    jobs: Mapping[str, dict],
    requested_urls: Iterable[str],
    mode: str,
) -> list[tuple[str, dict]]:
    """Select requested jobs in request order, skipping duplicates/ineligible jobs."""
    allowed = preparable_statuses(mode)
    selected: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for url in requested_urls:
        if url in seen:
            continue
        seen.add(url)
        job = jobs.get(url)
        if job and job.get("status") in allowed:
            selected.append((url, job))
    return selected
