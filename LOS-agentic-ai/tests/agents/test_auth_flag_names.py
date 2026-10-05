"""
ONE AUTHENTICATION SWITCH: AUTH_ENABLED (2026-10-04).

A deployment .env carried API_AUTH_ENABLED=false, which no code reads -- it looked like
authentication was off while it was on. The dead name is reported at startup and NEVER
honoured: honouring "false" would switch authentication off under a second name.
"""

from __future__ import annotations

import logging

import pytest

from app.security import auth


def test_the_dead_name_is_reported_and_ignored(monkeypatch, caplog):
    monkeypatch.setenv("API_AUTH_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.security.auth"):
        auth.validate_auth_mode()
    assert auth.auth_enabled() is True
    assert any("API_AUTH_ENABLED is set but IGNORED" in r.getMessage() for r in caplog.records)


def test_auth_enabled_false_outside_development_refuses_to_start(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        auth.validate_auth_mode()


def test_no_warning_without_the_dead_name(monkeypatch, caplog):
    monkeypatch.delenv("API_AUTH_ENABLED", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.security.auth"):
        auth.validate_auth_mode()
    assert not auth.dead_auth_flags() and not caplog.records
