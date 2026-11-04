"""Tests of the configuration helper."""

from common.config import get_settings


def test_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERVICE_NAME", "prueba")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "42")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "7")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.service_name == "prueba"
    assert settings.rate_limit_per_minute == 42
    assert settings.circuit_failure_threshold == 7

    get_settings.cache_clear()


def test_invalid_numbers_fall_back_to_the_default(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "no-es-un-numero")
    get_settings.cache_clear()

    assert get_settings().rate_limit_per_minute == 60

    get_settings.cache_clear()
