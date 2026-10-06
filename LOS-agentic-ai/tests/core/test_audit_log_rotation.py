"""Audit logs are bounded: size rotation with retention, nothing rewritten (2026-10-05)."""

from app.observability import audit_log


def test_a_full_log_rotates_and_keeps_only_the_configured_number(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_MAX_MB", str(200 / (1024 * 1024)))     # 200 bytes
    monkeypatch.setenv("AUDIT_LOG_KEEP", "2")
    path = tmp_path / "audit.jsonl"
    for i in range(40):
        audit_log.append_line(path, '{"n": %d, "pad": "xxxxxxxxxxxxxxxxxxxx"}' % i)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2"]     # retention bound
    assert all(p.stat().st_size <= 260 for p in tmp_path.iterdir())
    last = path.read_text(encoding="utf-8").splitlines()[-1]
    assert '"n": 39' in last                                             # newest line in the live file
    rotated = (tmp_path / "audit.jsonl.1").read_text(encoding="utf-8").splitlines()
    assert all(line.startswith('{"n":') for line in rotated)             # whole lines, never cut
