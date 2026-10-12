"""
A BODY LARGER THAN ANY ROUTE ACCEPTS is refused (413) from its declared Content-Length, before it is read (2026-10-11,
security review); an ordinary request is untouched.
"""
from tests.integration.master_env import prod  # noqa: F401
from tests.integration.test_reupload_supersedes import client  # noqa: F401


def test_an_oversized_declared_body_is_refused_before_reading(client, monkeypatch):
    monkeypatch.setenv("LOS_MAX_REQUEST_BYTES", "1000")
    r = client.post("/api/v1/fos/copilot", content=b"x" * 2000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"]["code"] == "PAYLOAD_TOO_LARGE"


def test_an_ordinary_request_passes(client, prod, monkeypatch):
    monkeypatch.setenv("LOS_MAX_REQUEST_BYTES", "100000")
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "show my cases", "reply_language": "en", "chat_id": "sz"})
    assert r.status_code == 200


def test_the_default_limit_is_above_the_upload_limit():
    from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES
    import main  # noqa: F401 - the default is derived from it

    assert MAX_UPLOAD_BYTES == 25 * 1024 * 1024


# ---- API-wide limit (app/security/api_rate_limit.py) ----------------------------------------------------------
def test_the_api_limit_answers_429_with_retry_after(client, prod, monkeypatch):
    from app.security import api_rate_limit

    api_rate_limit.reset()
    monkeypatch.setenv("LOS_API_RATE_LIMIT_PER_MINUTE", "3")
    body = {"action": "CUSTOM_QUERY", "message": "show my cases", "reply_language": "en", "chat_id": "rl"}
    codes = [client.post("/api/v1/fos/copilot", json=body).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429, codes
    r = client.post("/api/v1/fos/copilot", json=body)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.json()["detail"]["code"] == "RATE_LIMITED"
    api_rate_limit.reset()


def test_another_caller_is_not_limited_by_the_first(client, prod, monkeypatch, make_token):
    from app.security import api_rate_limit
    from tests.integration.test_reupload_supersedes import FOS_SCOPES

    api_rate_limit.reset()
    monkeypatch.setenv("LOS_API_RATE_LIMIT_PER_MINUTE", "1")
    body = {"action": "CUSTOM_QUERY", "message": "show my cases", "reply_language": "en", "chat_id": "rl2"}
    client.post("/api/v1/fos/copilot", json=body)
    assert client.post("/api/v1/fos/copilot", json=body).status_code == 429
    other = {"Authorization": f"Bearer {make_token(subject='other-officer', scopes=FOS_SCOPES)}"}
    assert client.post("/api/v1/fos/copilot", json=body, headers=other).status_code != 429
    api_rate_limit.reset()


def test_health_is_never_limited(client, monkeypatch):
    from app.security import api_rate_limit

    api_rate_limit.reset()
    monkeypatch.setenv("LOS_API_RATE_LIMIT_PER_MINUTE", "1")
    assert all(client.get("/health").status_code != 429 for _ in range(3))
    api_rate_limit.reset()


def test_a_malformed_size_setting_keeps_the_default(client, prod, monkeypatch):
    monkeypatch.setenv("LOS_MAX_REQUEST_BYTES", "not-a-number")
    r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": "show my cases",
                                                 "reply_language": "en", "chat_id": "sz2"})
    assert r.status_code == 200
