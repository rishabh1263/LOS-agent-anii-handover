"""
THE LOS -> CRIME VERIFICATION INTAKE: PUT /applications/{id}. In production this is the only way an application and
its people enter (the demo seed is for demo mode only).

The body carries ALL the application's people; the list replaces the one held. One transaction:
    - exactly one applicant; co-applicants numbered 1..N, at most config subjects.max_co_applicants, no number twice
    - every PAN in the configured format, no PAN twice on one application; given details in the right format
      (details may still be incomplete: the desk shows what is missing and blocks initiation until it is complete)
    - the caller may write this branch (and, for an existing application, its current branch)
    - a person with verification history keeps their PAN and cannot be removed (409): the history stays true
Audit APPLICATION_SYNCED (counts only, never a PAN or a name).
"""

from __future__ import annotations

import re
from typing import Any

from psycopg import errors as pg_errors

from app.agents.credit.crime_verification.config import say, section
from app.agents.credit.crime_verification.db import database
from app.agents.credit.crime_verification.domain.models import ApplicationIn
from app.agents.credit.crime_verification.domain.validation import validate_subject
from app.agents.credit.crime_verification.services import queries, views
from app.agents.credit.crime_verification.services.access import Caller, require
from app.agents.credit.crime_verification.services.audit import audit
from app.agents.credit.crime_verification.services.errors import ServiceError


def _check(body: ApplicationIn) -> list[dict[str, str]]:
    rules = section("validation")
    errors: list[dict[str, str]] = []
    primaries = [p for p in body.people if p.role == "PRIMARY"]
    if len(primaries) != 1:
        errors.append({"field": "people", "message": say("errors.BAD_APPLICATION")})
    most = int(section("subjects").get("max_co_applicants", 10))
    co = [p for p in body.people if p.role != "PRIMARY"]
    if len(co) > most:
        errors.append({"field": "people", "message": say("errors.TOO_MANY_SUBJECTS", max=most)})
    keys = [views.party_key(p.role, p.seq) for p in body.people]
    pans = [p.pan.upper() for p in body.people]
    for i, p in enumerate(body.people):
        prefix = f"people[{i}]"
        if keys.count(keys[i]) > 1:
            errors.append({"field": f"{prefix}.seq", "message": say("errors.DUPLICATE_SUBJECT")})
        if p.role != "PRIMARY" and p.seq > most:
            errors.append({"field": f"{prefix}.seq", "message": say("errors.TOO_MANY_SUBJECTS", max=most)})
        if not re.match(str(rules.get("pan_pattern", r"^.{10}$")), pans[i]):
            errors.append({"field": f"{prefix}.pan", "message": say("validation.pan_format")})
        elif pans.count(pans[i]) > 1:
            errors.append({"field": f"{prefix}.pan", "message": say("errors.SAME_PAN_TWICE")})
        # what was given must be right; what is missing is allowed here (the desk asks for it)
        given = {k: v for k, v in p.model_dump(exclude={"role", "seq", "pan"}).items() if v}
        for e in validate_subject(given, prefix):
            if e["field"].split(".", 1)[1] in given:
                errors.append(e)
    return errors


def upsert_application(who: Caller, application_id: str, body: ApplicationIn) -> dict[str, Any]:
    require(who, "manage_applications")
    if not who.sees(body.branch):
        raise ServiceError(403, "BRANCH_NOT_PERMITTED", branch=body.branch)
    errors = _check(body)
    if errors:
        raise ServiceError(422, "VALIDATION_ERROR", details=errors)
    with database().tx() as conn:
        held = conn.execute("SELECT * FROM crime_verification_applications WHERE application_id = %s FOR UPDATE",
                            (application_id,)).fetchone()
        if held and not who.sees(held["branch"]):
            raise ServiceError(403, "BRANCH_NOT_PERMITTED", branch=held["branch"])
        conn.execute(
            "INSERT INTO crime_verification_applications (application_id, title, branch, product, loan_amount, stage, "
            "status) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (application_id) DO UPDATE SET "
            "title = EXCLUDED.title, branch = EXCLUDED.branch, product = EXCLUDED.product, "
            "loan_amount = EXCLUDED.loan_amount, stage = EXCLUDED.stage, status = EXCLUDED.status",
            (application_id, body.title, body.branch, body.product, body.loan_amount, body.stage, body.status))
        before = views.parties(conn, application_id)
        with_history = {views.party_key(r["role"], r["seq"]) for r in conn.execute(
            "SELECT DISTINCT role, seq FROM crime_verification_subjects WHERE application_id = %s", (application_id,))}
        incoming = {views.party_key(p.role, p.seq): p for p in body.people}
        co = sum(1 for k in incoming if k != "PRIMARY")
        held_co = sum(1 for k in before if k != "PRIMARY")             # the person is named as the desk shows them now
        for key, row in before.items():
            label = views.party_label(row["role"], row["seq"], held_co)
            if key not in incoming and key in with_history:
                raise ServiceError(409, "PARTY_HAS_HISTORY", role=label)
            if key in incoming and key in with_history and incoming[key].pan.upper() != row["pan"]:
                raise ServiceError(409, "PARTY_HAS_HISTORY", role=label)
        # gone, or a new PAN (only people without history get here): rewritten, so a PAN moving between two people in
        # one push never meets itself in the per-application PAN guard
        rewrite = {k for k in before if k not in incoming or incoming[k].pan.upper() != before[k]["pan"]}
        for key in rewrite:
            row = before[key]
            conn.execute("DELETE FROM crime_verification_parties WHERE application_id = %s AND role = %s AND seq = %s",
                         (application_id, row["role"], row["seq"]))
        try:
            _write_people(conn, application_id, body)
        except pg_errors.UniqueViolation:
            # a PAN the database already holds for this application under another person (a race with another push)
            raise ServiceError(409, "BAD_APPLICATION") from None
        audit(conn, application_id, None, who.user, "APPLICATION_SYNCED", people=len(body.people),
              co_applicants=co, created=held is None, removed=len(set(before) - set(incoming)))
    return queries.application_view(who, application_id)


def _write_people(conn, application_id: str, body: ApplicationIn) -> None:
    with conn.transaction():
        for p in body.people:
            conn.execute(
                "INSERT INTO crime_verification_parties (application_id, role, seq, full_name, father_name, "
                "date_of_birth, permanent_address, pan) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (application_id, role, seq) DO UPDATE SET full_name = EXCLUDED.full_name, "
                "father_name = EXCLUDED.father_name, date_of_birth = EXCLUDED.date_of_birth, "
                "permanent_address = EXCLUDED.permanent_address, pan = EXCLUDED.pan",
                (application_id, p.role, p.seq, p.full_name, p.father_name, p.date_of_birth or None,
                 p.permanent_address, p.pan.upper()))

