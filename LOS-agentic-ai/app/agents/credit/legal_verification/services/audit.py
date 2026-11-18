"""The audit trail and ids. Audit metadata is safe by construction: ids, kinds, fields, statuses -- never a document's content."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def audit(conn, application_id: str, case_id: str | None, actor: str, event: str, **metadata: Any) -> None:
    conn.execute("INSERT INTO legal_verification_audit (application_id, case_id, actor, event_type, metadata) "
                 "VALUES (%s, %s, %s, %s, %s)", (application_id, case_id, actor, event, Jsonb(metadata)))
