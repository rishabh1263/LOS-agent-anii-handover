"""LOS_STORE_DSN accepts the URL form and the .NET form a .NET team writes (2026-10-06)."""

import pytest

from app.store.postgres_repo import libpq_dsn
from app.store.repository import RepositoryError


def test_the_url_form_passes_unchanged():
    assert libpq_dsn("postgresql://u:p@db:5432/los") == "postgresql://u:p@db:5432/los"


def test_the_dotnet_form_becomes_libpq_keywords():
    out = libpq_dsn("Host=localhost;Port=5432;Database=agenticAI;Username=postgres;Password=s3cr'et")
    assert out == "host='localhost' port='5432' dbname='agenticAI' user='postgres' password='s3cr\\'et'"


def test_an_empty_password_is_omitted_not_sent_empty():
    assert "password" not in libpq_dsn("Host=localhost;Port=5432;Database=los;Username=postgres;Password=")


def test_an_unknown_key_is_refused_by_name():
    with pytest.raises(RepositoryError, match="Pooling"):
        libpq_dsn("Host=localhost;Pooling=true")
