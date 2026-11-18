"""
LEVIOSA AS THE LMS (owner, 2026-10-23): one loan's live record by PR code, mapped to the LOS-side typed record
(lms.FIELDS) through app/config/leviosa_lms.yaml -- the same discipline as the MIFIN adapter.

NO LEVIOSA CONTRACT EXISTS IN THIS REPOSITORY YET: the field names, status words and record path are config, marked
UNVERIFIED, and change there (never here) once a sample arrives. What this module guarantees regardless:
  * not configured / timeout / 5xx after retries / 4xx  -> lms.LmsUnavailable (the caller stops asking: MIFIN-only)
  * 404 (config not_found_status)                      -> None: the loan is not in Leviosa (NOT_FOUND, not an outage)
  * a reply that is not JSON, or for ANOTHER PR code    -> the loan's record carries a problem and no values
  * a status / yes-no / date it cannot read             -> left out and reported, never guessed and never zero
The token is never logged; neither is a PR code (only its masked form, by the caller).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from app.agents.dedupe import identity as I
from app.agents.dedupe.lms import FIELDS, LmsUnavailable

logger = logging.getLogger(__name__)
_PATH = Path(__file__).resolve().parents[2] / "config" / "leviosa_lms.yaml"
NAME = "LEVIOSA"


@lru_cache(maxsize=1)
def cfg() -> dict[str, Any]:
    return yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}


def _dig(obj: Any, path: list[str]) -> Any:
    for key in path or []:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _date(value: Any) -> str | None:
    text = str(value).strip()
    for fmt in cfg().get("date_formats") or []:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def to_record(prospect_code: str, reply: Any) -> dict[str, Any]:
    """Leviosa's reply -> {our field: value} (+ `_problems`), by the config mapping. Pure."""
    c = cfg()
    problems: list[str] = []
    record = _dig(reply, (c.get("response") or {}).get("record_path") or [])
    if not isinstance(record, dict):
        return {"_problems": ["LEVIOSA_RESPONSE_MALFORMED"]}
    echo_key = (c.get("response") or {}).get("prospect_code_field")
    if echo_key and record.get(echo_key) not in (None, ""):
        if I.normalise("PR_CODE", str(record[echo_key])) != I.normalise("PR_CODE", prospect_code):
            return {"_problems": ["LEVIOSA_REPLY_FOR_ANOTHER_LOAN"]}         # never shown against this loan
    status_map = {str(k).upper(): v for k, v in (c.get("status_map") or {}).items()}
    yes = {str(v).upper() for v in ((c.get("booleans") or {}).get("yes") or [])}
    no = {str(v).upper() for v in ((c.get("booleans") or {}).get("no") or [])}
    out: dict[str, Any] = {}
    for ours, theirs in (c.get("fields") or {}).items():
        if ours not in FIELDS or theirs not in record or record[theirs] in (None, ""):
            continue
        value = record[theirs]
        if ours == "status":
            word = str(value).strip().upper()
            if word in status_map:
                out[ours] = status_map[word]
            else:
                out[ours] = "UNKNOWN"
                problems.append("STATUS_UNRECOGNISED")
        elif ours in ("settled", "written_off"):
            if isinstance(value, bool):
                out[ours] = value
            elif str(value).strip().upper() in yes:
                out[ours] = True
            elif str(value).strip().upper() in no:
                out[ours] = False
            else:
                problems.append(f"{ours.upper()}_INVALID")
        elif ours in ("disbursed_on", "closed_on"):
            iso = _date(value)
            if iso:
                out[ours] = iso
            else:
                problems.append(f"{ours.upper()}_INVALID")
        else:
            out[ours] = value                                  # numbers are typed / checked by lms.parse
    if problems:
        out["_problems"] = problems
    return out


class HttpLeviosaLms:
    """THE LIVE CALL: GET LEVIOSA_LMS_URL (with {prospect_code}), bearer LEVIOSA_LMS_TOKEN."""
    is_demo = False
    name = NAME

    def __init__(self, url: str, token: str | None):
        self.url, self.token = url, token

    def loan(self, prospect_code: str) -> dict[str, Any] | None:
        import httpx

        c = cfg().get("client") or {}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        url = self.url.replace("{prospect_code}", quote(str(prospect_code), safe=""))
        not_found = {int(x) for x in c.get("not_found_status") or [404]}
        last = "LEVIOSA_FAILED"
        for attempt in range(int(c.get("retries", 0)) + 1):
            try:
                r = httpx.get(url, headers=headers, timeout=float(c.get("timeout_seconds", 6)))
            except httpx.TimeoutException:
                last = "LEVIOSA_TIMEOUT"
            except httpx.HTTPError:
                last = "LEVIOSA_UNREACHABLE"
            else:
                if r.status_code in not_found:
                    return None
                if r.status_code >= 500:
                    last = f"LEVIOSA_HTTP_{r.status_code}"
                elif r.status_code >= 400:
                    raise LmsUnavailable(f"LEVIOSA_HTTP_{r.status_code}")           # never retried
                else:
                    try:
                        reply = r.json()
                    except ValueError:
                        return {"_problems": ["LEVIOSA_RESPONSE_MALFORMED"]}
                    return to_record(prospect_code, reply)
            if attempt < int(c.get("retries", 0)):
                time.sleep(float(c.get("backoff_seconds", 0.4)) * (attempt + 1))
        logger.warning("Leviosa LMS: %s", last)                 # the reason only: never the token or the PR code
        raise LmsUnavailable(last)


class DemoLeviosa:
    """DEVELOPMENT ONLY (refused in production by lms.client): synthetic Leviosa replies (mifin_demo.yaml `leviosa:`)
    read by the REAL mapping (to_record), the way DemoMifin feeds the real MIFIN reader."""
    is_demo = True
    name = NAME

    def __init__(self, replies: dict[str, Any]):
        self.replies = {str(k).upper(): v for k, v in (replies or {}).items()}

    def loan(self, prospect_code: str) -> dict[str, Any] | None:
        reply = self.replies.get(str(prospect_code).upper())
        return None if reply is None else to_record(prospect_code, reply)        # absent = Leviosa's 404


def client():
    """LMS_PROVIDER=leviosa: the live client when LEVIOSA_LMS_URL is set, else not configured (UNAVAILABLE)."""
    url = (os.getenv("LEVIOSA_LMS_URL") or "").strip()
    if not url or "{prospect_code}" not in url:
        return None
    return HttpLeviosaLms(url, (os.getenv("LEVIOSA_LMS_TOKEN") or "").strip() or None)


__all__ = ["DemoLeviosa", "HttpLeviosaLms", "NAME", "cfg", "client", "to_record"]
