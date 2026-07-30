import pytest

from kalshi_bots.skills import risk_management as rm


@pytest.fixture(autouse=True)
def _clear_risk_overrides():
    """Live overrides are module-global; never let one leak across tests."""
    yield
    rm.clear_all_overrides()
    rm.override_log.clear()
