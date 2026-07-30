from credforge.config import Settings


def test_defaults_without_any_env(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.brave_api_key is None
    assert settings.anthropic_api_key is None
    assert settings.resolve_confidence_threshold == 0.7


def test_unprefixed_vendor_keys_are_read(monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")
    settings = Settings(_env_file=None)
    assert settings.brave_api_key.get_secret_value() == "brave-test-key"
    assert settings.anthropic_api_key.get_secret_value() == "anthropic-test-key"


def test_credforge_prefixed_vars_are_read(monkeypatch) -> None:
    monkeypatch.setenv("CREDFORGE_RESOLVE_CONFIDENCE_THRESHOLD", "0.9")
    settings = Settings(_env_file=None)
    assert settings.resolve_confidence_threshold == 0.9
