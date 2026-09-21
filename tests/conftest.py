import pytest


@pytest.fixture(autouse=True)
def _collector_settings_come_from_the_test_not_the_machine(monkeypatch):
    """The collector reads settings.json; a test must not change result with the developer's own file."""
    monkeypatch.setattr("cli.collect_jobs.load_settings", lambda: {})
