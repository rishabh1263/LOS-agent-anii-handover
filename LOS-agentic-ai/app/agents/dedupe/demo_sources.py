"""
DEMO MIFIN (owner, 2026-10-22: "everything dummy, the logic runs on it") -- answers in the MIFIN RESPONSE SHAPE from
app/config/demo/mifin_demo.yaml, so the real normaliser and every rule after it run exactly as they will on MIFIN.
MIFIN_PROVIDER=demo turns it on; production refuses it (mifin.client()). Every result it feeds is marked is_demo.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).resolve().parents[2] / "config" / "demo" / "mifin_demo.yaml"


@lru_cache(maxsize=2)
def _load(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def data() -> dict[str, Any]:
    return _load(str(Path(os.getenv("MIFIN_DEMO_PATH") or PATH)))


def reload() -> None:
    _load.cache_clear()


class DemoMifin:
    is_demo = True

    def lookup(self, parameter: str, value: str) -> dict[str, Any]:
        from app.agents.dedupe import identity

        kind = str(parameter or "").upper()
        wanted = identity.normalise(kind, value) if kind in identity.NORMALISE else None
        for entry in data().get("customers") or []:
            keys = entry.get("keys") or {}
            if wanted and identity.normalise(kind, str(keys.get(kind) or "")) == wanted:
                return self._answer(entry)
            # a loan account number (PR code) finds the customer who holds that loan, as MIFIN's own search does
            if wanted and kind == "PR_CODE" and any(
                    identity.normalise(kind, str(p.get("PROSPECTCODE") or "")) == wanted for p in entry.get("prospects") or []):
                return self._answer(entry)
        return {"RESPONSE": {"MESSAGE": "NO RECORD FOUND", "STATUS": "S"}}

    @staticmethod
    def _answer(entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("error_status"):
            return {"RESPONSE": {"MESSAGE": "ERROR", "STATUS": entry["error_status"]}}
        if entry.get("ambiguous"):
            details = [{"CUSTID": cid, "STRONG_MATCH": "Y", "POTENTIAL_MATCH": "", "APPLICANTLOANINFO": {
                "PROSPECTCODES": []}} for cid in entry["ambiguous"]]
            return {"RESPONSE": {"MESSAGE": "MULTIPLE APPLICANTS FOUND", "STATUS": "S", "APPLICANTDETAILS": details}}
        applicant = dict(entry.get("applicant") or {})
        applicant["APPLICANTLOANINFO"] = {"PROSPECTCODES": list(entry.get("prospects") or [])}
        return {"RESPONSE": {"MESSAGE": "APPLICANT ID FOUND WITH GIVEN INPUT", "STATUS": "S",
                             "APPLICANTDETAILS": applicant}}


__all__ = ["DemoMifin", "data", "reload"]
