"""Fixtures for Sporthive tests."""

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Enable loading custom integrations."""
    yield


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    """Skip the polite delay between API requests."""
    monkeypatch.setattr("custom_components.sporthive.coordinator.REQUEST_DELAY_SECONDS", 0)
