"""
Who is on each case, as comparable PROFILES -- PAN and mobile, from what the LOS actually stores.

  - PRIMARY APPLICANT: PAN from the party's gate-released EXTRACTION findings (encrypted at rest, opened by the
    repository on read); mobile from `applicants.mobile` (declared on the FOS form).
  - CO-APPLICANT: PAN from `co_applicants.pan` (declared, Fernet-sealed; migration 0004) and from the party's
    EXTRACTION findings. A co-applicant's mobile is not stored anywhere.

SCAN MODE (migration 0010 not applied): the most recent `limits.max_scan_cases` cases are read; more in the store is
reported as partial coverage -- never as a clean history.

Values leave this module only as keyed-HMAC tokens; a plain PAN / mobile exists in memory for one normalisation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.dedupe import identity as I

PRIMARY, CO = "PRIMARY_APPLICANT", "CO_APPLICANT"
_CHUNK = 400
_PAN_KEYS = ("pan_number", "pan")


@dataclass
class Profile:
    case_id: str
    applicant_id: str                      # the case's customer record (APP-...)
    party_id: str
    role: str
    product: str | None = None
    created_at: str | None = None
    pan: set[str] = field(default_factory=set)        # keyed-HMAC tokens, every key
    mobile: set[str] = field(default_factory=set)
    #: where each parameter came from (DECLARED / DOCUMENT_EXTRACTION) -- names only
    sources: dict[str, str] = field(default_factory=dict)
    #: the party's recorded names as word sets (upper case, configured honorifics dropped). Compared in memory only
    #: (one PAN, two different names = an identity conflict); never returned, logged or tokenised out of this module.
    names: list[frozenset[str]] = field(default_factory=list)

    def add_name(self, value: str | None, ignore: frozenset[str] = frozenset()) -> None:
        key = name_key(value, ignore)
        if key and key not in self.names:
            self.names.append(key)

    def add(self, kind: str, value: str | None, source: str) -> None:
        norm = I.normalise(kind, value)
        if not norm:
            return
        (self.pan if kind == "PAN" else self.mobile).update(I.tokens(kind, norm))
        if self.sources.get(kind) != "DOCUMENT_EXTRACTION":     # a document-read value outranks a declared one
            self.sources[kind] = source

    @property
    def has_identity(self) -> bool:
        return bool(self.pan or self.mobile)


def name_key(value: str | None, ignore: frozenset[str] = frozenset()) -> frozenset[str] | None:
    """A name as its set of words: "Mr. Rahul  K Sharma" -> {RAHUL, K, SHARMA} (honorifics in `ignore` dropped)."""
    words = frozenset(re.findall(r"[A-Z]+", str(value or "").upper())) - ignore
    return words or None


def names_differ(a: Profile, b: Profile) -> bool:
    """Both parties have a recorded name and no name of one is contained in a name of the other. A missing middle
    name or initial is the same person ({RAHUL, SHARMA} within {RAHUL, K, SHARMA}); no name on either side compares
    nothing and is never a conflict."""
    if not a.names or not b.names:
        return False
    return not any(x <= y or y <= x for x in a.names for y in b.names)


def name_ignore_words() -> frozenset[str]:
    """dedupe_policy.yaml matching.name_comparison.ignore_words (honorifics), upper case."""
    try:
        from app.agents.dedupe import config as C

        spec = (C.get_policy().get("matching") or {}).get("name_comparison") or {}
    except Exception:  # noqa: BLE001 - an unreadable policy fails elsewhere, loudly; names compare without the list
        spec = {}
    return frozenset(str(w).upper() for w in spec.get("ignore_words") or [])


def _chunks(items: list[str]):
    for i in range(0, len(items), _CHUNK):
        yield items[i:i + _CHUNK]


def _marks(n: int) -> str:
    return ",".join("?" for _ in range(n))


_COLS = "case_id, applicant_id, co_applicant_id, product, loan_amount, created_at"


def case_rows(repository, *, limit: int, case_ids: list[str] | None = None,
              applicant_ids: list[str] | None = None) -> tuple[list[dict], bool]:
    """(applications rows, truncated?): the named cases / customer records, or the newest `limit`."""
    if case_ids or applicant_ids:
        column, wanted = ("case_id", case_ids) if case_ids else ("applicant_id", applicant_ids)
        rows: list[dict] = []
        for part in _chunks(list(dict.fromkeys(wanted))):
            rows += [dict(r) for r in repository._all(
                f"SELECT {_COLS} FROM applications WHERE {column} IN ({_marks(len(part))})", tuple(part))]
        return rows, False
    rows = [dict(r) for r in repository._all(
        f"SELECT {_COLS} FROM applications ORDER BY created_at DESC, case_id LIMIT ?", (limit + 1,))]
    return rows[:limit], len(rows) > limit


def build(repository, cases: list[dict]) -> list[Profile]:
    """Profiles for every party on these cases."""
    if not cases:
        return []
    case_ids = [c["case_id"] for c in cases]
    by_case = {c["case_id"]: c for c in cases}
    profiles: dict[tuple[str, str], Profile] = {}

    def party(case_id: str, party_id: str, role: str) -> Profile:
        key = (case_id, party_id)
        if key not in profiles:
            row = by_case[case_id]
            profiles[key] = Profile(case_id, row["applicant_id"], party_id, role, row.get("product"),
                                    str(row.get("created_at") or "") or None)
        return profiles[key]

    ignore = name_ignore_words()
    applicant_ids = list(dict.fromkeys(c["applicant_id"] for c in cases if c.get("applicant_id")))
    mobiles: dict[str, str | None] = {}
    names: dict[str, str | None] = {}
    for part in _chunks(applicant_ids):
        for r in repository._all(f"SELECT applicant_id, mobile, full_name FROM applicants WHERE applicant_id IN "
                                 f"({_marks(len(part))})", tuple(part)):
            mobiles[r["applicant_id"]] = r["mobile"]
            names[r["applicant_id"]] = r["full_name"]
    for c in cases:
        primary = party(c["case_id"], c["applicant_id"], PRIMARY)
        primary.add("MOBILE", mobiles.get(c["applicant_id"]), "DECLARED")
        primary.add_name(names.get(c["applicant_id"]), ignore)
        if c.get("co_applicant_id"):
            party(c["case_id"], c["co_applicant_id"], CO)

    try:
        has_coapp = repository.table_exists("co_applicants")
    except Exception:  # noqa: BLE001 - an unreadable catalogue: no declared co-applicant PAN, the rest still reads
        has_coapp = False
    if has_coapp:
        from app.store.crypto import open_value

        for part in _chunks(case_ids):
            for r in repository._all("SELECT co_applicant_id, case_id, pan, name FROM co_applicants "
                                     f"WHERE case_id IN ({_marks(len(part))})", tuple(part)):
                co = party(r["case_id"], r["co_applicant_id"], CO)
                co.add("PAN", open_value(r["pan"]), "DECLARED")
                co.add_name(open_value(r["name"]), ignore)

    for part in _chunks(case_ids):
        for case_id, findings in repository.get_current_findings_for_cases(part, kind="EXTRACTION").items():
            row = by_case[case_id]
            for f in findings:
                payload = f.payload or {}
                party_id = f.party_id or row["applicant_id"]
                p = party(case_id, party_id, PRIMARY if party_id == row["applicant_id"] else CO)
                for k in _PAN_KEYS:
                    if payload.get(k):
                        p.add("PAN", str(payload[k]), "DOCUMENT_EXTRACTION")
    return list(profiles.values())


def file_hashes(repository, case_ids: list[str], *, limit: int | None = None) -> tuple[dict[str, list[str]], int, bool]:
    """case_id -> SHA-256 of each stored file (hashed from the document store; migration 0010 would keep it on the
    row). Returns (hashes, files missing from the store, truncated?)."""
    from app.store import documents as store_module
    from app.store.documents import get_document_store

    store = get_document_store()
    out: dict[str, list[str]] = {}
    missing, read, truncated = 0, 0, False
    for case_id, docs in repository.list_documents_for_cases(case_ids).items():
        for doc in docs:
            if limit is not None and read >= limit:
                truncated = True
                break
            read += 1
            try:
                described = store.describe(store_module.storage_key(doc.document_id))
            except Exception:  # noqa: BLE001 - an unreadable file is counted, never fatal
                described = None
            if described is None:
                missing += 1
                continue
            out.setdefault(case_id, []).append(described.content_hash)
    return out, missing, truncated


__all__ = ["CO", "PRIMARY", "Profile", "build", "case_rows", "file_hashes"]
