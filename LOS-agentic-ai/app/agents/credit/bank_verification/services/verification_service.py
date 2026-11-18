"""
API 1 -- PROCESS: take the statement (uploaded / the case's seeded one / none), the AA response (AA_SUCCESS), the
CPA's form, read and record everything, run the eight rules, record the run, answer with an explainable result.

    1. the case may be processed (exists, not finally decided)
    2. the statement file is read (statement_service) and recorded with its hash and what was read
    3. the AA response (simulated) is loaded for AA_SUCCESS; nothing for AA_FAILURE
    4. the form: as sent by the CPA, else filled from AA (AA_SUCCESS) or from what was read (AA_FAILURE)
    5. idempotency: the same Idempotency-Key with the same inputs answers the stored run; with different inputs, 409.
       The same inputs again without a key answer the last run too (`replayed`), so a double click is not a new run.
    6. rules R1-R8, recorded one row each; the case's status / risk level follow R8
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from fastapi import HTTPException

from app.agents.credit.bank_verification import rules as rulebook
from app.agents.credit.bank_verification.config import say, section, words
from app.agents.credit.bank_verification.repository import Repository, new_id
from app.agents.credit.bank_verification.schemas import (DocumentFacts, EnteredDetails, FinalStatus, Mode,
                                                         OwnershipStatus, ProcessRequest,
                                                         Provenance, RuleResult, RuleStatus, StatementData, mask)
from app.agents.credit.bank_verification.services import statement_service, transaction_service


class ServiceError(HTTPException):
    """A request the service refuses: `status` is the HTTP status, `code` the stable error code; the message is the
    wording config's errors.<code> (or the reader's own sentence for a file problem). An HTTPException, so the LOS
    server's error envelope (app/api/errors.py) carries its code and message too when the router runs on 8010."""

    def __init__(self, http: int, code: str, message: str | None = None, details: Any = None, **values: Any):
        message = message if message is not None else say(f"errors.{code}", **values)
        super().__init__(status_code=http, detail={"code": code, "message": message, "details": details})
        self.status, self.code, self.message, self.details = http, code, message, details

    def __str__(self) -> str:
        return self.message


def upload_error(exc: "statement_service.UploadError", wrap: str | None = None) -> ServiceError:
    """A refused file as an API error: 413 too large, 415 wrong type, 422 otherwise."""
    status = 413 if exc.code == "FILE_TOO_LARGE" else 415 if "TYPE" in exc.code else 422
    return ServiceError(status, exc.code, say(wrap, problem=exc) if wrap else str(exc))


FINAL = {str(s) for s in words("case_status.final")}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _statement(repo: Repository, request: ProcessRequest, upload: tuple[bytes, str | None] | None,
               actor: str) -> tuple[StatementData | None, DocumentFacts | None]:
    if request.statement_source == "NONE":
        if upload is not None:
            raise ServiceError(422, "UNEXPECTED_FILE", source="NONE")
        return None, None
    if request.statement_source == "UPLOAD":
        if upload is None:
            raise ServiceError(422, "STATEMENT_FILE_REQUIRED")
        content, name = upload
        seeded = False
    else:
        # SEEDED: the case's seeded synthetic statement; ON_FILE: its latest statement, uploaded or seeded
        if upload is not None:
            raise ServiceError(422, "UNEXPECTED_FILE", source=request.statement_source)
        docs = [d for d in repo.documents(request.case_id, "STATEMENT")
                if d["seeded"] or request.statement_source == "ON_FILE"]
        if not docs:
            raise ServiceError(409, "NO_SEEDED_STATEMENT" if request.statement_source == "SEEDED" else "NO_STATEMENT_ON_FILE")
        content, name, seeded = repo.file_bytes(docs[-1]["document_id"]), docs[-1]["file_name"], bool(docs[-1]["seeded"])
        reused = _stored_read(repo, docs[-1])
        if reused is not None:
            return reused
    try:
        data, facts = statement_service.read(content, name, seeded=seeded)
    except statement_service.UploadError as exc:
        repo.audit(request.case_id, actor, "STATEMENT_REJECTED", {"code": exc.code, "reason": str(exc)})
        raise upload_error(exc) from None
    existing = next((d for d in repo.documents(request.case_id, "STATEMENT") if d["sha256"] == facts.sha256), None)
    if existing is not None:
        facts.document_id = existing["document_id"]
        if existing.get("fingerprint") is None and data is not None:     # copied in from the LOS before it was read
            repo.complete_document(existing["document_id"], facts.fingerprint, data.details.account_number,
                                   [f"{t.date.isoformat()}|{t.amount}|{t.direction.value}" for t in data.transactions])
    else:
        keys = [f"{t.date.isoformat()}|{t.amount}|{t.direction.value}" for t in (data.transactions if data else [])]
        facts.document_id = repo.add_document(
            request.case_id, "STATEMENT", content, file_name=name, kind=facts.kind, sha256=facts.sha256,
            fingerprint=facts.fingerprint, account_number=data.details.account_number if data else None,
            transaction_keys=keys, seeded=seeded)
        repo.audit(request.case_id, actor, "STATEMENT_RECEIVED",
                   {"document_id": facts.document_id, "file_name": name, "size": facts.size, "seeded": seeded,
                    "sha256": facts.sha256[:16]})
    repo.add_extraction(request.case_id, facts.document_id, facts.extraction_status.value,
                        data.model_dump(mode="json") if data else None, facts.model_dump(mode="json"))
    repo.audit(request.case_id, actor, "STATEMENT_READ",
               {"status": facts.extraction_status.value, "transactions": len(data.transactions) if data else 0,
                "notes": facts.notes})
    return data, facts


def _stored_read(repo: Repository, doc: dict[str, Any]) -> tuple[StatementData | None, DocumentFacts] | None:
    """The read already made of this stored statement (at upload), when config statement_prefill.reuse_extraction."""
    if not section("statement_prefill").get("reuse_extraction", True):
        return None
    found = repo.latest_extraction(doc["document_id"])
    if found is None:
        return None
    facts = DocumentFacts.model_validate(found["facts"])
    facts.document_id = doc["document_id"]
    return (StatementData.model_validate(found["data"]) if found["data"] else None), facts


def statement_form(repo: Repository, case_id: str, mode: Mode) -> EnteredDetails | None:
    """The fields config statement_prefill names, filled from the latest stored statement's read, tagged EXTRACTED
    (the CPA checks each one). None when off, in another mode, or nothing has been read."""
    spec = section("statement_prefill") or {}
    if not spec.get("enabled") or mode.value not in [str(m) for m in spec.get("modes") or []]:
        return None
    docs = repo.documents(case_id, "STATEMENT")
    found = repo.latest_extraction(docs[-1]["document_id"]) if docs else None
    if not found or not found["data"]:
        return None
    read = transaction_service.prefill(StatementData.model_validate(found["data"]), Provenance.EXTRACTED)
    fields = [str(f) for f in spec.get("fields") or []]
    out = EnteredDetails()
    for key in ("bank_name", "account_number", "account_type"):
        if key in fields and getattr(read, key):
            setattr(out, key, getattr(read, key))
            out.provenance[key] = Provenance.EXTRACTED
    if "transactions" in fields:
        out.transactions = list(read.transactions)
        for i in range(len(read.transactions)):
            out.provenance[f"transaction_{i + 1}"] = Provenance.EXTRACTED
    return out


def latest_uncertain(case: dict[str, Any], data: StatementData | None, facts: DocumentFacts) -> bool:
    """Rule 2's own test, on a read alone: are the statement's latest rows certain?"""
    from app.agents.credit.bank_verification.rules import transaction_matching as r2

    if data is None or not data.transactions:
        return False
    ev = rulebook.Evidence(case_id=case["case_id"], mode=Mode(case["mode"]), applicant_name=case["applicant_name"],
                           entered=EnteredDetails(), statement=data, facts=facts)
    end_required = bool(section("latest_transactions").get("require_end_of_statement", True))
    return r2.recent_row_unread(ev, transaction_service.latest(data.transactions).rows) or \
        (end_required and not r2.read_to_the_end(ev))


def read_statement(repo: Repository, case_id: str, upload: tuple[bytes, str | None], actor: str) -> dict[str, Any]:
    """Read an uploaded statement NOW (before the run) and answer with what it fills on the form. A long or scanned
    one is read in the background: `status: READING`, and the case screen refreshes until the read is in."""
    heal(repo, case_id)
    case = repo.case(case_id)
    if case is None or case.get("historical"):
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=case_id)
    if case["status"] == READING:
        raise ServiceError(409, "STATEMENT_BEING_READ")
    if case["status"] in FINAL:
        raise ServiceError(409, "CASE_FINALIZED", case_id=case_id, status=case["status"])
    try:
        statement_service.sniff(upload[0], upload[1], list(section("uploads").get("statement_types") or ["pdf"]))
    except statement_service.UploadError as exc:
        raise upload_error(exc) from None
    request = ProcessRequest(case_id=case_id, mode=Mode(case["mode"]), statement_source="UPLOAD")
    pages = _heavy_pages(upload[0])
    if pages:
        previous = case["status"]
        _IN_FLIGHT.add(case_id)
        repo.update_case(case_id, status=READING)
        repo.audit(case_id, actor, "STATEMENT_QUEUED", {"pages": pages})

        def job() -> None:
            try:
                _statement(repo, request, upload, actor)
            except Exception as exc:  # noqa: BLE001 - the reason goes to the case history
                repo.audit(case_id, actor, "STATEMENT_READ_FAILED",
                           {"reason": exc.message if isinstance(exc, ServiceError) else type(exc).__name__})
            finally:
                repo.update_case(case_id, status=previous)
                _IN_FLIGHT.discard(case_id)

        _executor().submit(job)
        return {"case_id": case_id, "status": READING, "pages": pages}
    data, facts = _statement(repo, request, upload, actor)
    filled = statement_form(repo, case_id, Mode(case["mode"]))
    return {"case_id": case_id, "status": "READ", "document_id": facts.document_id,
            "extraction_status": facts.extraction_status.value, "scanned": facts.scanned, "notes": facts.notes,
            "transactions_read": len(data.transactions) if data else 0,
            # the latest rows filled may not be THE latest (a newest row unread, or the end of the statement not
            # reached): the screen asks the CPA to type the latest three from the statement -- the same test rule 2 uses
            "latest_uncertain": latest_uncertain(case, data, facts),
            "filled": _form_view(filled) if filled else None}


def _aa(repo: Repository, case_id: str, mode: Mode) -> StatementData | None:
    if mode is not Mode.AA_SUCCESS:
        return None
    response = repo.latest_source(case_id, "AA")
    if not response:
        raise ServiceError(409, "NO_AA_DATA")
    return StatementData.model_validate(response["data"])


def application_form(repo: Repository, case_id: str, mode: Mode) -> EnteredDetails | None:
    """The bank account the customer gave on the application (source APPLICATION), as the form's starting values --
    only in the modes config application_details.modes names."""
    spec = section("application_details") or {}
    if not spec.get("enabled") or mode.value not in [str(m) for m in spec.get("modes") or []]:
        return None
    given = repo.latest_source(case_id, "APPLICATION")
    if not given:
        return None
    fields = [str(f) for f in spec.get("fields") or [] if given.get(str(f))]
    if not fields:
        return None
    return EnteredDetails(**{f: given[f] for f in fields}, provenance={f: Provenance.APPLICATION for f in fields})


def _form(request: ProcessRequest, aa: StatementData | None, statement: StatementData | None,
          application: EnteredDetails | None = None) -> EnteredDetails:
    """The form as sent; a masked account number that equals the prefill's (or the application's) mask means 'keep
    the one on file' -- the screen only ever shows the number masked."""
    prefill_from = aa if aa is not None else statement
    prefill = transaction_service.prefill(prefill_from, Provenance.AA if aa is not None else Provenance.EXTRACTED) \
        if prefill_from is not None else EnteredDetails()
    if request.entered is None:
        return prefill
    form = request.entered.model_copy(deep=True)
    for known in (prefill.account_number, application.account_number if application else None):
        if form.account_number and "X" in form.account_number.upper() and known \
                and form.account_number.upper() == (mask(known) or "").upper():
            form.account_number = known
            break
    # WHAT THE OFFICER LEFT BLANK is taken from the source -- only where config fill_blanks_from_source allows it (AA
    # success: the AA data); AA failure: the CPA types it, a blank stays blank. What was typed is what gets compared.
    if not bool((section("fill_blanks_from_source") or {}).get(request.mode.value, False)):
        return form
    for key in ("bank_name", "account_number", "account_type"):
        if not getattr(form, key) and getattr(prefill, key):
            setattr(form, key, getattr(prefill, key))
            form.provenance[key] = prefill.provenance.get(key, Provenance.EXTRACTED)
    if all(t.blank() for t in form.transactions) and prefill.transactions:
        form.transactions = list(prefill.transactions)
        for i in range(len(prefill.transactions)):
            form.provenance[f"transaction_{i + 1}"] = prefill.provenance.get(f"transaction_{i + 1}", Provenance.EXTRACTED)
    return form


def prefill_view(repo: Repository, case: dict[str, Any]) -> dict[str, Any] | None:
    """The form a case opens with before any run: filled from the AA data (AA success); from the application's bank
    account (AA failure, config application_details); nothing otherwise."""
    if case.get("mode") != Mode.AA_SUCCESS.value:
        given = application_form(repo, case["case_id"], Mode(case["mode"]))
        read = statement_form(repo, case["case_id"], Mode(case["mode"]))
        if given and read:
            # the application's claim wins; the statement fills only what the application does not hold
            claim = {k: getattr(given, k) for k in ("bank_name", "account_number") if getattr(given, k)}
            given = read.model_copy(update={**claim, "provenance": {
                **{k: v for k, v in read.provenance.items() if k not in claim}, **given.provenance}})
        given = given or read
        return _form_view(given) if given else None
    response = repo.latest_source(case["case_id"], "AA")
    if not response:
        return None
    return _form_view(transaction_service.prefill(StatementData.model_validate(response["data"]), Provenance.AA))


def ownership(repo: Repository, case_id: str) -> OwnershipStatus | None:
    """The agency's latest recorded ownership outcome for the case, if any."""
    decisions = repo.decisions(case_id)
    return OwnershipStatus(decisions[-1]["payload"]["ownership"]) if decisions else None


def evaluate(repo: Repository, case: dict[str, Any], mode: Mode, form: EnteredDetails, aa: StatementData | None,
             statement: StatementData | None, facts: DocumentFacts | None,
             owner: OwnershipStatus | None) -> list[RuleResult]:
    prior = [rulebook.PriorDocument(case_id=d["case_id"], sha256=d["sha256"], fingerprint=d["fingerprint"],
                                    account_number=d["account_number"],
                                    transaction_keys=frozenset(d["transaction_keys"]))
             for d in repo.documents(role="STATEMENT") if d["case_id"] != case["case_id"]]
    evidence = rulebook.Evidence(case_id=case["case_id"], mode=mode, applicant_name=case["applicant_name"],
                                 entered=form, aa=aa, statement=statement, facts=facts, ownership=owner,
                                 prior_documents=prior)
    return rulebook.run_all(evidence)


def _view(data: StatementData | None) -> dict[str, Any] | None:
    """A source for the screen: the account number masked."""
    if data is None:
        return None
    out = data.model_dump(mode="json")
    out["details"]["account_number"] = mask(data.details.account_number) if data.details.account_number and \
        "X" not in data.details.account_number.upper() else data.details.account_number
    return out


def _form_view(form: EnteredDetails) -> dict[str, Any]:
    out = form.model_dump(mode="json")
    out["account_number"] = mask(form.account_number) if form.account_number else None
    return out


def respond(case: dict[str, Any], run_id: str, mode: Mode, results: list[RuleResult], form: EnteredDetails,
            aa: StatementData | None, statement: StatementData | None, facts: DocumentFacts | None) -> dict[str, Any]:
    summary = results[-1]
    return {
        "case_id": case["case_id"],
        "run_id": run_id,
        "mode": mode.value,
        "source": "AA" if mode is Mode.AA_SUCCESS else "MANUAL",
        "processing_status": "COMPLETED",
        "final_status": case["status"] if case["status"] in FINAL else None,
        "overall_status": summary.status.value,
        "rules": [r.model_dump(mode="json") for r in results],
        "risk_flags": summary.details.get("risk_flags", []),
        "missing_evidence": summary.missing_evidence,
        "mismatches": summary.details.get("mismatches", []),
        "checks": {k: summary.details.get(k) for k in ("checks_applicable", "checks_passed", "checks_failed",
                                                        "checks_review", "not_applicable", "score_note")},
        "recommended_action": summary.recommended_action,
        "explanation": summary.reason,
        "form": _form_view(form),
        "sources": {"aa": _view(aa), "statement": _view(statement),
                    "statement_file": facts.model_dump(mode="json", exclude={"fingerprint"}) if facts else None},
        "replayed": False,
    }


def _risk_level(results: list[RuleResult]) -> str:
    return results[-1].details.get("highest_severity") or "NONE"


READING = "READING"
_EXECUTOR: Any = None
_EXECUTOR_LOCK = threading.Lock()


def _executor():
    """ONE background reader (config background.workers): long / scanned statements are CPU-heavy."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor

            _EXECUTOR = ThreadPoolExecutor(max_workers=int(section("background").get("workers", 1)),
                                           thread_name_prefix="bank-statement-reader")
        return _EXECUTOR


_IN_FLIGHT: set[str] = set()                # cases whose statement a background job is reading now


def heal(repo: Repository, case_id: str) -> None:
    """A case left READING with no job reading it (the server restarted mid-read) goes back to where its last run
    put it, and its history says the read was interrupted -- never stuck on "reading" forever."""
    case = repo.case(case_id)
    if not case or case["status"] != READING or case_id in _IN_FLIGHT:
        return
    found = latest(repo, case_id)
    status = str(words("case_status.after_run")[found[0]["overall_status"]]) if found else str(words("case_status.seeded"))
    repo.update_case(case_id, status=status)
    repo.audit(case_id, "system", "STATEMENT_READ_FAILED", {"reason": "interrupted"})


def wait_background(timeout: float = 600) -> None:
    """Block until the queued reads are done (tests)."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        running, _EXECUTOR = _EXECUTOR, None
    if running is not None:
        running.shutdown(wait=True)


def _heavy_pages(content: bytes) -> int | None:
    """The page count when this statement is read in the background (long digital / scanned), else None."""
    spec = section("background")
    if not spec.get("enabled", False) or not content.startswith(b"%PDF-"):
        return None
    try:
        import pymupdf

        with pymupdf.open(stream=content, filetype="pdf") as doc:
            pages = doc.page_count
            scanned = not any((doc[i].get_text() or "").strip() for i in range(min(pages, 3)))
    except Exception:  # noqa: BLE001 - a damaged file is reported by the normal read, at once
        return None
    if scanned and statement_service.scans_enabled():
        return pages if pages > int(spec.get("scan_over_pages", 1)) else None
    return pages if pages > int(spec.get("over_pages", 30)) else None


def submit(repo: Repository, payload: dict[str, Any] | ProcessRequest, *, upload: tuple[bytes, str | None] | None,
           actor: str, idempotency_key: str | None) -> dict[str, Any]:
    """API 1's entry: a small statement is checked now (process); a long or scanned one is queued and the answer is
    `processing_status: READING` at once -- the screen refreshes until the run is recorded."""
    request = payload if isinstance(payload, ProcessRequest) else ProcessRequest.model_validate(payload)
    heal(repo, request.case_id)
    case = repo.case(request.case_id)
    if case is None or case.get("historical"):
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=request.case_id)
    if case["status"] == READING:
        raise ServiceError(409, "STATEMENT_BEING_READ")
    if case["status"] in FINAL:
        raise ServiceError(409, "CASE_FINALIZED", case_id=request.case_id, status=case["status"])
    content = upload[0] if upload else None
    if upload is not None:
        try:
            statement_service.sniff(upload[0], upload[1], list(section("uploads").get("statement_types") or ["pdf"]))
        except statement_service.UploadError as exc:
            raise upload_error(exc) from None
    elif request.statement_source in ("ON_FILE", "SEEDED"):
        docs = [d for d in repo.documents(request.case_id, "STATEMENT")
                if d["seeded"] or request.statement_source == "ON_FILE"]
        content = repo.file_bytes(docs[-1]["document_id"]) if docs and _stored_read(repo, docs[-1]) is None else None
    pages = _heavy_pages(content) if content else None
    if not pages:
        return process(repo, request, upload=upload, actor=actor, idempotency_key=idempotency_key)
    previous = case["status"]
    _IN_FLIGHT.add(request.case_id)
    repo.update_case(request.case_id, status=READING)
    repo.audit(request.case_id, actor, "STATEMENT_QUEUED", {"pages": pages})

    def job() -> None:
        try:
            process(repo, request, upload=upload, actor=actor, idempotency_key=idempotency_key, background=True)
        except Exception as exc:  # noqa: BLE001 - the case goes back to where it was, with the reason in its history
            repo.update_case(request.case_id, status=previous)
            repo.audit(request.case_id, actor, "STATEMENT_READ_FAILED",
                       {"reason": exc.message if isinstance(exc, ServiceError) else type(exc).__name__})
        finally:
            _IN_FLIGHT.discard(request.case_id)

    _executor().submit(job)
    return {"case_id": request.case_id, "processing_status": READING, "pages": pages, "final_status": None}


def process(repo: Repository, payload: dict[str, Any] | ProcessRequest, *, upload: tuple[bytes, str | None] | None,
            actor: str, idempotency_key: str | None, background: bool = False) -> dict[str, Any]:
    request = payload if isinstance(payload, ProcessRequest) else ProcessRequest.model_validate(payload)
    case = repo.case(request.case_id)
    if case is None or case.get("historical"):
        raise ServiceError(404, "CASE_NOT_FOUND", case_id=request.case_id)
    if case["status"] == READING and not background:
        raise ServiceError(409, "STATEMENT_BEING_READ")
    if case["status"] in FINAL:
        raise ServiceError(409, "CASE_FINALIZED", case_id=request.case_id, status=case["status"])
    if idempotency_key:
        earlier = repo.run_by_key(request.case_id, idempotency_key)
        if earlier is not None:
            # the same key: the same request (content hash of what was SENT), or a conflict
            sent = _hash({"request": request.model_dump(mode="json"),
                          "file": hashlib.sha256(upload[0]).hexdigest() if upload else None})
            stored = json.loads(earlier["response"])
            if stored.get("_request_hash") != sent:
                raise ServiceError(409, "IDEMPOTENCY_KEY_REUSED")
            return {**{k: v for k, v in stored.items() if not k.startswith("_")}, "replayed": True}
    statement, facts = _statement(repo, request, upload, actor)
    aa = _aa(repo, request.case_id, request.mode)
    form = _form(request, aa, statement, application_form(repo, request.case_id, request.mode))
    owner = ownership(repo, request.case_id)
    if request.agency_id and request.agency_id != case.get("assigned_agency"):
        from app.agents.credit.bank_verification.config import cfg

        if request.agency_id not in {a["id"] for a in cfg().get("agencies") or []}:
            raise ServiceError(422, "UNKNOWN_AGENCY", agency_id=request.agency_id)
        repo.update_case(request.case_id, assigned_agency=request.agency_id)
        repo.audit(request.case_id, actor, "AGENCY_ASSIGNED", {"agency_id": request.agency_id})
        case = repo.case(request.case_id)
    repo.add_source(request.case_id, "FORM", {**form.model_dump(mode="json"), "account_number": mask(form.account_number)})
    inputs = {"mode": request.mode.value, "statement": facts.sha256 if facts else None,
              "aa": aa.model_dump(mode="json") if aa else None, "form": form.model_dump(mode="json"),
              "ownership": owner.value if owner else None,
              "prior": sorted(f"{d['case_id']}:{d['sha256']}" for d in repo.documents(role="STATEMENT")
                              if d["case_id"] != request.case_id)}
    input_hash = _hash(inputs)
    last = repo.latest_run(request.case_id)
    if not idempotency_key and last is not None and last["input_hash"] == input_hash:
        stored = json.loads(last["response"])
        if case["status"] == READING:
            # a background read that found nothing new: the case leaves READING with the replayed run's status
            repo.update_case(request.case_id, status=str(words("case_status.after_run")[stored["overall_status"]]))
        repo.audit(request.case_id, actor, "VERIFICATION_REPLAYED", {"run_id": last["run_id"]})
        return {**{k: v for k, v in stored.items() if not k.startswith("_")}, "replayed": True}
    results = evaluate(repo, case, request.mode, form, aa, statement, facts, owner)
    run_id = new_id("RUN")
    status = str(words("case_status.after_run")[results[-1].status.value])
    repo.update_case(request.case_id, status=status, mode=request.mode.value, risk_level=_risk_level(results))
    case = repo.case(request.case_id)
    response = respond(case, run_id, request.mode, results, form, aa, statement, facts)
    stored = {**response, "_request_hash": _hash({"request": request.model_dump(mode="json"),
                                                  "file": hashlib.sha256(upload[0]).hexdigest() if upload else None}),
              "_evidence": {"form": form.model_dump(mode="json"), "aa": aa.model_dump(mode="json") if aa else None,
                            "statement": statement.model_dump(mode="json") if statement else None,
                            "facts": facts.model_dump(mode="json") if facts else None}}
    repo.add_run(request.case_id, run_id, idempotency_key, input_hash, stored, response["rules"])
    repo.audit(request.case_id, actor, "VERIFICATION_RUN",
               {"run_id": run_id, "overall": results[-1].status.value,
                "rules": {r.rule_id: r.status.value for r in results}, "action": results[-1].recommended_action})
    from app.agents.credit.bank_verification.services import summary_service

    summary_service.precompute(repo, request.case_id)        # the easy-words summary starts now, in the background
    return response


def latest(repo: Repository, case_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(the latest run's public response, its stored evidence), or None."""
    last = repo.latest_run(case_id)
    if last is None:
        return None
    stored = json.loads(last["response"])
    return {k: v for k, v in stored.items() if not k.startswith("_")}, stored.get("_evidence") or {}


def rerun_with(repo: Repository, case_id: str, owner: OwnershipStatus, actor: str) -> dict[str, Any]:
    """The latest run's evidence re-evaluated with the agency's ownership outcome (finalize), recorded as a new run."""
    found = latest(repo, case_id)
    if found is None:
        raise ServiceError(409, "NOT_PROCESSED")
    response, ev = found
    case = repo.case(case_id)
    mode = Mode(response["mode"])
    form = EnteredDetails.model_validate(ev["form"])
    aa = StatementData.model_validate(ev["aa"]) if ev.get("aa") else None
    statement = StatementData.model_validate(ev["statement"]) if ev.get("statement") else None
    facts = DocumentFacts.model_validate(ev["facts"]) if ev.get("facts") else None
    results = evaluate(repo, case, mode, form, aa, statement, facts, owner)
    run_id = new_id("RUN")
    out = respond(case, run_id, mode, results, form, aa, statement, facts)
    repo.add_run(case_id, run_id, None, _hash({"rerun": run_id}), {**out, "_evidence": ev}, out["rules"])
    repo.audit(case_id, actor, "RULES_RERUN_WITH_AGENCY_INPUT",
               {"run_id": run_id, "ownership": owner.value, "rules": {r.rule_id: r.status.value for r in results}})
    return out
