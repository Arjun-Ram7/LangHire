"""Helpers for enforcing collection limits across multiple job titles."""

import math


def remaining_collection_limit(max_jobs: int, collected_count: int) -> int:
    """Return the per-title allowance remaining in the overall collection run.

    The collectors use ``0`` to mean unlimited, so an unlimited run keeps that
    sentinel. For limited runs, ``0`` means the overall budget is exhausted.
    """
    if max_jobs <= 0:
        return 0
    return max(0, max_jobs - collected_count)


def next_title_collection_limit(
    max_jobs: int, collected_count: int, titles_remaining: int
) -> int:
    """Share the remaining overall budget across the unprocessed titles.

    Recalculating after every title lets later titles use any capacity that an
    earlier search could not fill, while preventing the first title from
    consuming the entire run budget.
    """
    remaining = remaining_collection_limit(max_jobs, collected_count)
    if max_jobs <= 0 or remaining == 0:
        return remaining
    return math.ceil(remaining / max(1, titles_remaining))
