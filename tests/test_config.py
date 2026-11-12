import pytest
from pydantic import ValidationError

from app.config import Settings


def test_production_rejects_default_jwt_secret():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production")


def test_production_rejects_private_targets_override():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret="x" * 40,
            allow_private_targets=True,
        )


def test_production_accepts_strong_secret():
    s = Settings(_env_file=None, environment="production", jwt_secret="x" * 40)
    assert s.allow_private_targets is False


def test_smtp_credentials_come_from_environment(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.net")
    monkeypatch.setenv("SMTP_PASSWORD", "s3cret")
    s = Settings(_env_file=None)
    assert s.smtp_host == "smtp.example.net" and s.smtp_password == "s3cret"
