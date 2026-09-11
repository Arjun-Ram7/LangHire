from backend.collection_limits import next_title_collection_limit, remaining_collection_limit


def test_limited_run_shares_one_budget_across_titles():
    assert remaining_collection_limit(100, 0) == 100
    assert remaining_collection_limit(100, 37) == 63
    assert remaining_collection_limit(100, 100) == 0
    assert remaining_collection_limit(100, 105) == 0


def test_unlimited_run_preserves_unlimited_sentinel():
    assert remaining_collection_limit(0, 0) == 0
    assert remaining_collection_limit(0, 250) == 0


def test_budget_is_shared_across_remaining_titles():
    assert next_title_collection_limit(100, 0, 3) == 34
    assert next_title_collection_limit(100, 30, 2) == 35
    assert next_title_collection_limit(100, 65, 1) == 35
    assert next_title_collection_limit(100, 100, 1) == 0


def test_unlimited_run_stays_unlimited_per_title():
    assert next_title_collection_limit(0, 0, 3) == 0
