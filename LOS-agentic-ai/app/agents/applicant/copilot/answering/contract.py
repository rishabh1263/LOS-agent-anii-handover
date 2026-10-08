"""
THE REPLY CONTRACT (MASTER SPEC section 8; flag COPILOT_MD_TTS_CONTRACT, config app/config/copilot_reply.yaml).

Every chat reply of BOTH endpoints (and the stream's final event) is exactly

    {"request_id": "...", "markdown": "...", "tts": "..."}

THE SHAPE IS CODE, never configurable (section 14). The words, link labels, limits and spoken forms are config.

MARKDOWN is what the screen shows: the answer as the pipeline already wrote it (fact-checked, masked, in the
locked language), in the professional format (no emojis, one "Next step:" line), with every interactive item as
a link -- [Label](action:<name>?k=v) from ONE registry (`action_links`), or [Question](ask:<text>). Each item once.

TTS is what the voice reads, made FROM that markdown only (so its facts are a subset of the screen's): the direct
answer, the lists summarised by status, the next step; at most `tts.max_sentences`; no markdown, links, tables or
symbols; ids shortened ("case ending 38A9"), amounts and dates spoken.

NOTHING IS ADDED THAT THE PIPELINE DID NOT SAY: this layer only re-renders the reply it is given.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote

import yaml

FLAG = "COPILOT_MD_TTS_CONTRACT"
_ON = {"1", "true", "yes", "on"}
_PATH = Path(__file__).resolve().parents[4] / "config" / "copilot_reply.yaml"
_CACHE: dict[str, Any] = {"mtime": None, "data": {}}
_LOCK = threading.Lock()
_log = logging.getLogger(__name__)

#: a link the frontend turns into a button: [label](action:name?x=y) / [label](ask:text)
LINK = re.compile(r"\[([^\]]+)\]\(((?:action|ask):[^)\s]*)\)")
_ID = re.compile(r"\b(COAPP|CASE|APP)-([0-9A-Z]{4,})\b", re.I)
_AMOUNT = re.compile(r"(?:₹|\bRs\.?|\bINR)\s?(\d[\d,]*(?:\.\d+)?)", re.I)
_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ][\d:.]+(?:Z|[+-]\d\d:?\d\d)?)?\b")
_STATUS = re.compile(r"\*\*([A-Z_]{3,})\*\*")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def cfg() -> dict[str, Any]:
    """copilot_reply.yaml, re-read when the file changes (a config edit is followed without a restart)."""
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _CACHE["mtime"] != mtime:
            _CACHE["data"] = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
            _CACHE["mtime"] = mtime
        return _CACHE["data"]


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(cfg().get("enabled", False))


def _language(reply: dict[str, Any] | None = None) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    locked = language_lock.current()
    if locked:
        return locked
    contract = (reply or {}).get("language_contract") if isinstance(reply, dict) else None
    if isinstance(contract, dict) and contract.get("response_language"):
        return str(contract["response_language"])
    return "en"


def _pick(value: Any, lang: str) -> Any:
    from app.agents.applicant.copilot.answering import language_lock

    if isinstance(value, dict) and not any(k in value for k in ("en", "hi", "hi-Latn", "mr", "default")):
        return value
    return language_lock.pick(value, lang) if isinstance(value, dict) else value


# --------------------------------------------------------------------------
# action links -- one registry
# --------------------------------------------------------------------------

def registry() -> dict[str, dict[str, Any]]:
    return cfg().get("action_links") or {}


def _encode(text: str) -> str:
    return quote(str(text), safe="?=&,:'-_.~!*/@")


def link(name: str, lang: str = "en", label: str | None = None, **params: Any) -> str:
    """[Label](action:name?k=v) for a REGISTERED action; an unknown name is a programming error."""
    spec = registry().get(name)
    if spec is None:
        raise KeyError(f"action link '{name}' is not in copilot_reply.yaml action_links")
    wanted = [p for p in spec.get("params") or [] if params.get(p) not in (None, "")]
    query = "&".join(f"{p}={_encode(params[p])}" for p in wanted)
    if label is None:
        label = str(_pick(spec.get("labels") or name, lang)).format(**{k: v for k, v in params.items()})
    return f"[{label}](action:{name}{'?' + query if query else ''})"


def ask(question: str) -> str:
    """[Question](ask:Question) -- the frontend sends the text as the next message."""
    text = " ".join(str(question).split())
    return f"[{text}](ask:{_encode(text)})"


def parse(href: str) -> tuple[str, str, dict[str, str]]:
    """('action'|'ask', name-or-text, params) of a link target."""
    scheme, _, rest = str(href or "").partition(":")
    if scheme == "ask":
        return "ask", unquote(rest), {}
    name, _, query = rest.partition("?")
    return scheme, name, {k: v for k, v in parse_qsl(query)}


def request_for(href: str) -> dict[str, Any] | None:
    """
    The request body an action link stands for (registry `request`, parameters filled), or None when it is not a
    server action (unknown, client-only). The caller still goes through the ordinary route -- scope is re-checked
    there on every action, never here.
    """
    scheme, name, params = parse(href)
    if scheme == "ask":
        return {"action": "CUSTOM_QUERY", "message": name}
    spec = registry().get(name)
    if scheme != "action" or not spec:
        return None
    body = spec.get("request") or {}
    if body.get("client_only") or body.get("method"):
        return None
    if any(p not in params for p in spec.get("params") or []):
        return None
    return {k: (str(v).format(**params) if isinstance(v, str) else v) for k, v in body.items()}


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    """The professional format: no pictographs, bullets for icon rows, one "Next step:" line, no raw codes."""
    from app.agents.applicant.copilot.answering import professional

    cleaned = _labels(professional.strip_emojis(professional._bulleted(professional._next_step(str(text or "")))))
    cleaned = _no_separators(cleaned)
    # the older widget's inline chip text ("PAN [Upload]") is not a button here: the buttons are the action links
    # (a checkbox "- [ ]" / "[x]" is content, never touched: a chip label has 2+ letters)
    return re.sub(r"[ \t]+\[(?=[^\]\n()]*[A-Za-z\u0900-\u097F]{2})[^\]\n()]{2,30}\](?!\()", "", cleaned)


def _no_separators(text: str) -> str:
    """
    FINAL FIX A5: natural sentences -- " -- " becomes ". " before a capital, else ", "; command-style hints
    ("say ...", "type ...", markdown.command_hints) are removed. Link targets are never touched.
    """
    for pattern in (cfg().get("markdown") or {}).get("command_hints") or []:
        text = re.sub(pattern, "", text)

    def line(text_line: str) -> str:
        # a short label before the separator ("08 Oct 2026, 11:30 -- Case created", "PAN -- missing") becomes
        # "label: ..."; a clause becomes its own sentence before a capital, else a comma
        def joint(m: re.Match) -> str:
            before, after = m.group(1), m.group(2)
            left = text_line[:m.start(2)].rsplit(" -- ", 1)[0]
            label = re.sub(r"^\s*(?:[-*•]|\d+\.)\s*", "", left)
            if ": " not in label and len(label.split()) <= 6 and not label.rstrip().endswith((".", "!", "?")):
                return before + ": " + after
            if after.isupper() and after.isalpha():
                return before + ("" if before in ".!?:" else ".") + " " + after
            return before + ("" if before in ",;:" else ",") + " " + after

        return re.sub(r"(\S)\s+--\s+(\S)", joint, text_line)

    return "\n".join(line(ln) for ln in text.split("\n"))


_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z]{2,})+\b")


def _labels(text: str) -> str:
    """A configured document code ("ADDRESS_PROOF") is shown as its label ("Address Proof") -- never a raw code."""
    from app.agents.applicant import config
    from app.agents.applicant.copilot.answering.answer import _readable

    known = {str(t).upper() for t in config.document_types() or []}
    for product in config.products() or []:
        for item in config.checklist_for(product) or []:
            known |= {str(item.get(k)).upper() for k in ("slot", "document_type", "code") if item.get(k)}
    return _CODE.sub(lambda m: _readable(m.group(0)) if m.group(0) in known else m.group(0), text)


def _rows(reply: dict[str, Any]) -> list[dict[str, Any]]:
    for holder in (reply.get("presentation"), reply.get("workspace_view")):
        if isinstance(holder, dict) and isinstance(holder.get("case_list"), list):
            return [r for r in holder["case_list"] if isinstance(r, dict) and r.get("case_id")]
    return []


def _workspace(reply: dict[str, Any]) -> dict[str, Any] | None:
    for holder in (reply.get("presentation"), reply.get("workspace_view")):
        if isinstance(holder, dict) and isinstance(holder.get("workspace"), dict):
            return holder["workspace"]
    return None


def _link_options(text: str, options: list[str]) -> str:
    """"1. Option" lines of a clarification become "1. [Option](ask:Option)" (each option once)."""
    lines = text.split("\n")
    done: set[str] = set()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*\d+\.\s+)(.+?)\s*$", line)
        if m and m.group(2) in options and m.group(2) not in done:
            lines[i] = m.group(1) + ask(m.group(2))
            done.add(m.group(2))
    rest = [o for o in options if o not in done]
    if rest:
        lines += [f"{n}. {ask(o)}" for n, o in enumerate(rest, len(done) + 1)]
    return "\n".join(lines)


def _action_links(reply: dict[str, Any], lang: str) -> list[str]:
    out: list[str] = []
    for a in reply.get("actions") or []:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("type") or "").upper()
        if kind == "OPEN_URL" and a.get("document_id"):
            out.append(link("view_document", lang, id=a["document_id"], doc_label=a.get("label") or ""))
        elif kind == "RAISE_QUERY":
            ref = (a.get("query") or {}).get("draft_id") or (a.get("query") or {}).get("ref") or "draft"
            out.append(link("raise_query", lang, ref=ref))
        elif kind == "MARK_QUERY_SENT" and a.get("query_id"):
            out.append(link("mark_query_sent", lang, id=a["query_id"]))
        elif kind == "COPY_TEXT":
            out.append(link("copy", lang, ref="draft-1"))
        elif kind == "OPEN_UI_NEW_CASE":
            out.append(link("new_case", lang))
    da = reply.get("document_actions") if isinstance(reply.get("document_actions"), dict) else {}
    party_of = {"PRIMARY_APPLICANT": "applicant", "APPLICANT": "applicant", "CO_APPLICANT": "co_applicant"}
    for row in (da.get("reupload") or []) + (da.get("pending") or []):
        if isinstance(row, dict) and (row.get("document_type") or row.get("slot")):
            doc = row.get("document_type") or row.get("slot")
            out.append(link("upload", lang, doc=doc, party=party_of.get(str(row.get("party") or "").upper(),
                                                                           "applicant"),
                            doc_label=row.get("label") or doc))
    for row in da.get("under_review") or []:
        if isinstance(row, dict) and row.get("document_id"):
            out.append(link("view_document", lang, id=row["document_id"], doc_label=row.get("label") or ""))
    note = reply.get("handoff_note") if isinstance(reply.get("handoff_note"), dict) else None
    if note and reply.get("case_id") and (note.get("ready") or reply.get("intent") == "HANDOFF_NOTE"):
        # the note is attached only for a ready case (handoff_note.attach); its download is the registry link
        out.append(link("handoff_note", lang, case=reply["case_id"]))
    if reply.get("intent") == "CASE_LIST" and (reply.get("presentation") or reply.get("workspace_view") or {}) \
            .get("has_more"):
        out.append(link("list_more", lang))
    return out


def _policy() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.cfg().get("link_policy") or {}


def _case_items(reply: dict[str, Any], lang: str) -> list[str]:
    """
    FINAL FIX A4: downloads + Show in UI (+ the workflow step on a case summary) -- ONLY at the end of a portfolio or
    case-summary answer (link_policy.case_links_on), never on every reply, never on a refusal.
    """
    from app.agents.applicant.copilot.capabilities import product_flow

    intent = str(reply.get("intent") or "")
    if _refused(reply) or reply.get("no_case_links") or intent not in set(_policy().get("case_links_on") or []):
        return []
    view = reply.get("workspace_view") if isinstance(reply.get("workspace_view"), dict) else {}
    if intent == "CASE_LIST":
        return product_flow.case_link_items(None, lang, list_query=view.get("query") or {}) \
            if view.get("case_list") else []
    if not reply.get("case_id"):
        return []
    from app.agents.applicant.copilot.capabilities import stage_flow

    return stage_flow.offers(str(reply["case_id"]), lang) + product_flow.case_link_items(str(reply["case_id"]), lang)


_NOT_ABOUT_THE_CASE = {"GUARDRAIL_BLOCKED", "OUT_OF_SCOPE", "UNKNOWN", "ABUSIVE", "COOLDOWN", "SECURITY_EVENT",
                       "SOCIAL_ENGINEERING", "SELF_HARM_SUPPORT", "RATE_LIMITED", "FAQ_UNKNOWN", "FAQ_ANSWER",
                       "NOT_IN_SCOPE"}


def _refused(reply: dict[str, Any]) -> bool:
    guard = reply.get("guardrail") if isinstance(reply.get("guardrail"), dict) else {}
    return (str(reply.get("intent") or "").upper() in _NOT_ABOUT_THE_CASE
            or str(guard.get("action") or "").upper() == "BLOCKED"
            or str(reply.get("response_source") or "").upper() in ("SAFETY", "GUARDRAIL"))


def _case_table(rows: list[dict[str, Any]], lang: str) -> list[str]:
    head = _pick((cfg().get("markdown") or {}).get("case_table_headers")
                 or {"en": ["#", "Case", "Applicant", "Stage", "Status", ""]}, lang)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        who = " ".join(x for x in (str(r.get("applicant_name") or ""),
                                   f"({r['applicant_id']})" if r.get("applicant_id") else "") if x)
        cells = [str(r.get("number") or ""), str(r["case_id"]), who,
                 str(r.get("stage") or ""), _clean(str(r.get("status_label") or "")).strip(),
                 link("open_case", lang, id=r["case_id"])]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return lines


def markdown(reply: dict[str, Any]) -> str:
    return _render(reply)[0]


def _render(reply: dict[str, Any], previous: set[str] | None = None) -> tuple[str, str]:
    """
    (the whole markdown, the answer body alone -- what the voice reads from). `previous`: the link targets the last
    reply of this chat showed (link_policy.no_repeat).
    """
    lang = _language(reply)
    from app.agents.applicant.copilot.answering import professional

    answer, marked = str(reply.get("answer") or ""), str(reply.get("answer_markdown") or "")
    # the bold version only when it says the same as the answer (a later step may have extended the answer)
    same = marked and " ".join(professional.plain(_clean(marked)).split()) == " ".join(
        professional.plain(_clean(answer)).split())
    text = marked if same else answer or marked
    if reply.get("markdown") and not text:
        text = str(reply["markdown"])
    text = _clean(text)
    clar = reply.get("clarification_required") if isinstance(reply.get("clarification_required"), dict) else None
    # only a clarification the answer still asks (a later step may have replaced the answer)
    options = [str(o) for o in (clar or {}).get("options") or []
               if str(clar.get("question") or "").strip() and str(clar.get("question")).strip() in text]
    rows = _rows(reply)
    if rows and not options and "](action:open_case" not in text:
        # the list's row lines -- and the applicant group lines, whose ids the table shows -- become ONE table
        # with an Open link per row
        ids = {r["case_id"] for r in rows} | {str(r.get("applicant_id")) for r in rows if r.get("applicant_id")}
        kept = [ln for ln in text.split("\n") if not any(i in ln for i in ids)]
        head, tail = kept[:1], [ln for ln in kept[1:] if ln.strip()]
        text = "\n".join(head + [""] + _case_table(rows, lang) + ([""] + tail if tail else []))
    if options:
        text = _link_options(text, options)
    body = dedupe(text)
    blocks = [text.rstrip()]
    faq_block = str(reply.get("faq_block") or "").strip()
    if faq_block:
        blocks.append(_clean(faq_block))         # the FAQ for this state (section 7), after the answer / review
    # FINAL FIX A4 -- less is more: ONE budget of `link_policy.max_items` extra items per reply (the answer's own
    # links first, then downloads / Show in UI on a summary, then suggestions); exit / switch are never buttons
    # (understood from text); a link the previous reply showed is not repeated
    policy = _policy()
    never = {f"(action:{n}" for n in policy.get("never_show") or []}
    shown_before = set(previous or ()) if policy.get("no_repeat", True) else set()

    def fresh(item: str) -> bool:
        targets = re.findall(r"\]\(((?:action|ask):[^)]*)\)", item)
        return bool(targets) and not any(n in item for n in never) and not all(t in shown_before for t in targets)

    budget = int(policy.get("max_items", 3))     # the flow question's own options (faq_block) are its answers
    items = [i for i in dict.fromkeys([ln for ln in _action_links(reply, lang) if ln not in text]
                                      + _case_items(reply, lang)) if fresh(i)][:max(budget, 0)]
    if items:
        label = str(_pick((cfg().get("markdown") or {}).get("actions_label") or "", lang) or "")
        blocks.append(((label + "\n") if label else "") + " · ".join(items))
    budget -= len(items)
    if not options and not faq_block and budget > 0:
        limit = min(int((cfg().get("markdown") or {}).get("max_suggestions", 3)), budget)
        asked = [q for q in reply.get("suggested_questions") or [] if isinstance(q, str) and q.strip()]
        ws = _workspace(reply)
        if ws:
            asked += [b.get("message") for b in ws.get("buttons") or [] if isinstance(b, dict)
                      and b.get("type") == "ask" and b.get("message")]
        said = text.lower()
        asked = [q for q in dict.fromkeys(_clean(q).strip() for q in asked) if q and q.lower() not in said
                 and not re.fullmatch(r"\d+", q) and ask(q).split("](", 1)[1][:-1] not in shown_before][:limit]
        if asked:
            label = str(_pick((cfg().get("markdown") or {}).get("suggestions_label") or "", lang) or "")
            blocks.append(((label + "\n") if label else "") + "\n".join(f"- {ask(q)}" for q in asked))
    md = _tidy_links(dedupe("\n\n".join(b for b in blocks if b.strip())))
    if reply.get("case_id") and str(reply.get("intent") or "") != "CASE_LIST":
        md = _consistent(md, str(reply.get("request_id") or ""))
    return md, body


def _consistent(md: str, request_id: str) -> str:
    """
    FINAL FIX A1 safety net: a case reply never says "all clear" beside a pending / not-run / failed / in-review
    item. The all-clear line is dropped and the event logged (the case brief is consistent by construction).
    """
    rules = (cfg().get("markdown") or {}).get("consistency") or {}
    clear = [re.compile(p, re.I) for p in rules.get("all_clear") or []]
    still_open = [re.compile(p, re.I) for p in rules.get("open") or []]
    lines = md.split("\n")
    clear_lines = [n for n, ln in enumerate(lines) if any(r.search(ln) for r in clear)]
    others = "\n".join(ln for n, ln in enumerate(lines) if n not in clear_lines)
    if clear_lines and any(r.search(others) for r in still_open):
        _log.warning("consistency: dropped an all-clear line beside an open item request_id=%s", request_id)
        return "\n".join(ln for n, ln in enumerate(lines) if n not in clear_lines)
    return md


def _tidy_links(text: str) -> str:
    """
    A link label is a button: no bold inside it, and no glossary expansion a rewrite step added that the link's
    own text does not have ("[KYC (Know Your Customer) issues](ask:KYC issues)" -> "[KYC issues](...)").
    """
    def fix(m: re.Match) -> str:
        label, href = m.group(1).replace("**", "").replace("__", ""), m.group(2)
        if "(" in label and href.startswith("ask:") and "(" not in unquote(href[4:]):
            label = re.sub(r"\s*\([^)]*\)", "", label).strip() or label
        return f"[{label}]({href})"

    return LINK.sub(fix, str(text or ""))


def dedupe(text: str) -> str:
    """No sentence said twice: a repeated non-empty line (case-, space- and marker-insensitive) is dropped."""
    seen: set[str] = set()
    out: list[str] = []
    for line in str(text or "").split("\n"):
        key = re.sub(r"[\s*_>#-]+", " ", line).strip().lower()
        if key and not key.startswith("|") and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# --------------------------------------------------------------------------
# tts
# --------------------------------------------------------------------------

def _tts_cfg() -> dict[str, Any]:
    return cfg().get("tts") or {}


def _spell(n: int, lang: str) -> str:
    words = ((_tts_cfg().get("number_words") or {}).get(lang) or {})
    ones, tens = words.get("ones") or [], words.get("tens") or []
    if not ones or n < 0 or n >= 100:
        return str(n)
    if n < len(ones):
        return str(ones[n])
    t, o = divmod(n, 10)
    return str(tens[t]) + (f"-{ones[o]}" if o else "")


def _say_amount(raw: str, lang: str) -> str:
    rule = (_tts_cfg().get("amounts") or {}).get(lang) or (_tts_cfg().get("amounts") or {}).get("en") or {}
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return raw
    spell = bool(rule.get("spell"))
    parts: list[str] = []
    rest = int(round(value))
    for size, word in rule.get("scales") or []:
        count, rest = divmod(rest, int(size))
        if count:
            parts.append(f"{_spell(count, lang) if spell else count} {word}")
    if rest or not parts:
        parts.append(_spell(rest, lang) if spell else str(rest))
    return " ".join(parts) + f" {rule.get('rupees', '')}".rstrip()


def _say_id(m: re.Match, lang: str) -> str:
    words = (_tts_cfg().get("id_words") or {}).get(m.group(1).upper()) or m.group(1)
    tail = int(_tts_cfg().get("id_tail", 4))
    return f"{_pick(words, lang)} {m.group(2)[-tail:].upper()}"


def _say_date(m: re.Match, lang: str) -> str:
    months = (_tts_cfg().get("months") or {}).get(lang) or (_tts_cfg().get("months") or {}).get("en") or []
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12) or len(months) < 12:
        return m.group(0)
    return str(_tts_cfg().get("date_format", "{day} {month} {year}")).format(day=day, month=months[month - 1],
                                                                           year=year)


def speakable(text: str, lang: str) -> str:
    """One line as plain spoken words: no markdown, links, ids by character, raw amounts / dates / symbols."""
    s = LINK.sub("", str(text or ""))
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\*\*|__", "", s)                      # bold markers vanish (never a gap: "(**Pending**)")
    s = _DATE.sub(lambda m: _say_date(m, lang), s)
    s = _AMOUNT.sub(lambda m: _say_amount(m.group(1), lang), s)
    s = _ID.sub(lambda m: _say_id(m, lang), s)
    for symbol, word in (((_tts_cfg().get("symbols") or {}).get(lang)) or {}).items():
        s = s.replace(symbol, word)
    s = re.sub(r"\s+--\s+", ", ", s)
    s = re.sub(r"[*_`#>|~^\\{}\[\]<>=+@$]", " ", s)
    s = re.sub(r"(?<=\w)-(?=\w)", "-", s)
    s = re.sub(r"\s-\s|^\s*-\s*", " ", s)
    s = re.sub(r"\s+([,.;:!?।])", r"\1", " ".join(s.split()))
    return s.strip(" ,;")


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?।" else text + "."


def _join(items: list[str], lang: str) -> str:
    word = _pick(_tts_cfg().get("and_word") or "and", lang)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


def tts(md: str, lang: str | None = None) -> str:
    """The spoken version of the markdown: answer line, list summaries by status, next step (max sentences)."""
    lang = lang or _language()
    cfg_t = _tts_cfg()
    limit = int(cfg_t.get("max_sentences", 3))
    first, next_step, heading = None, None, ""
    groups: dict[tuple[str, str], list[str]] = {}
    loose: list[str] = []                              # bullets with no status word: spoken as they are
    known = {str(k).upper() for k in (cfg_t.get("status_words") or {})}
    from app.agents.applicant.copilot.answering import professional

    step_label = str((professional._cfg() or {}).get("next_step_label") or "Next step:").rstrip(":").lower()
    for raw in str(md or "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("|") or not LINK.sub("", line).strip(" -·:"):
            continue
        plain = re.sub(r"\*\*|__", "", line)
        if plain.lower().startswith(step_label):
            next_step = next_step or line
            continue
        if _ID.fullmatch(plain.strip()):
            continue                                   # the active-case line: the screen shows it, never read
        if line.startswith(("-", "•")) or re.match(r"^\d+\.\s", line):
            status = next((m.upper() for m in re.findall(r"\*\*([^*]+)\*\*", line) if m.upper() in known), None)
            parts = re.split(r":|\s--\s", re.sub(r"^(?:[-•]|\d+\.)\s*", "", plain), maxsplit=1)
            name = parts[0].strip().rstrip(".")
            # a "name: STATUS" row only (a sentence that merely contains a status word is spoken as it is)
            status = status if len(parts) == 2 and status and status.lower() not in name.lower() else None
            if status and name:
                groups.setdefault((heading, status), []).append(speakable(name, lang))
            elif not status:
                loose.append(re.sub(r"^(?:[-•]|\d+\.)\s*", "", line))
            continue
        if line.startswith(">"):
            continue                                   # a quoted draft: on screen to copy, not read aloud
        if first is not None and (re.fullmatch(r"\*\*[^*]+\*\*.*", line) or plain.endswith(":")):
            heading = speakable(re.sub(r"\(.*?\)", "", plain), lang).rstrip(":")    # a section: groups apart
            continue
        if first is None:
            first = line
    sentences = [_sentence(speakable(first, lang).rstrip(":"))] if first else []
    short = [re.split(r"\s--\s|:", re.sub(r"^(?:[-•]|\d+\.)\s*", "", x), maxsplit=1)[0] for x in loose]
    if not groups and first and re.sub(r"\*\*|__", "", str(first)).rstrip().endswith(":") and loose \
            and all(len(s.split()) <= 6 for s in short):
        # "Still pending:" + short items -> ONE sentence naming them all ("Still pending: PAN, Address Proof and
        # Bank Statement.") -- never only the first item
        items = list(dict.fromkeys(speakable(s, lang).strip(" .") for s in short if s.strip()))
        sentences = [_sentence(speakable(first, lang).rstrip(":") + ": " + _join(items[:8], lang))]
        loose = []
    if not groups:
        # an answer made of plain points ("- PAN is verified."): the points are the answer
        sentences += [_sentence(speakable(x, lang).rstrip(":")) for x in loose[:limit]]
    sentences = [re.sub(r"([.!?।])\1+$", r"\1", s) for s in sentences]
    words = cfg_t.get("status_words") or {}
    template = _pick(cfg_t.get("list_summary") or "{status}: {items}.", lang)
    for n, ((section, status), items) in enumerate(groups.items()):
        label = _pick(words.get(status), lang) if words.get(status) else status.title()
        if n and section:
            label = f"{section}, {label.lower()}"          # a later section names itself
        sentences.append(str(template).format(status=label, items=_join(list(dict.fromkeys(items)), lang)))
    if next_step:
        spoken = _sentence(speakable(next_step, lang))
        sentences = sentences[:max(limit - 1, 1)] + [spoken]
    spoken = " ".join(s for s in sentences[:limit] if s.strip(" .")).strip()
    # the cap holds on the FINAL text too: an item may carry its own full stops ("08 Oct, 11:30. Case created.")
    parts = re.split(r"(?<=[.!?।])\s+", spoken)
    return " ".join(parts[:limit]).strip()


# --------------------------------------------------------------------------
# the published reply
# --------------------------------------------------------------------------

_LAST_LINKS: dict[tuple[str, str], set[str]] = {}


def publish(reply: Any, request_id: str | None = None, chat_key: tuple[str, str | None] | None = None) -> Any:
    """
    {request_id, markdown, tts} when the contract is on, else the reply unchanged. `chat_key` (subject, chat_id):
    the links shown are remembered so the next reply does not repeat them (link_policy.no_repeat).
    """
    if not enabled():
        return reply
    if hasattr(reply, "model_dump"):
        reply = reply.model_dump()
    if not isinstance(reply, dict):
        return reply
    if reply.get("atomic") and reply.get("tts_override"):
        # a blocked message (abuse_guard, section 16): the warning exactly as written; the voice says the warning
        # only, from its own template -- never the word, never spelled
        return {"request_id": str(reply.get("request_id") or request_id or ""),
                "markdown": str(reply.get("answer") or ""), "tts": str(reply["tts_override"])}
    followed = reply.get("followed_up")
    if isinstance(followed, dict) and followed.get("interpreted_as"):
        # MASTER SPEC section 4: the follow-up as typed and as rewritten (the reply no longer carries it), masked
        from app.agents.applicant.copilot.capabilities import abuse_guard
        from app.security import sensitivity

        _log.info("follow-up request_id=%s original=%r rewritten=%r reason=%s", reply.get("request_id"),
                  abuse_guard.mask_text(sensitivity.mask_identifiers(str(followed.get("original_message") or ""))),
                  abuse_guard.mask_text(sensitivity.mask_identifiers(str(followed.get("interpreted_as") or ""))),
                  followed.get("reason"))
    from app.agents.applicant.copilot.capabilities import abuse_guard

    key = (str(chat_key[0]), str(chat_key[1] or "default")) if chat_key else None
    md, body = _render(reply, _LAST_LINKS.get(key) if key else None)
    if key:
        with _LOCK:
            if len(_LAST_LINKS) > 5000:
                _LAST_LINKS.clear()
            _LAST_LINKS[key] = set(re.findall(r"\]\(((?:action|ask):[^)]*)\)", md))
    if reply.get("tts_text"):
        body = str(reply["tts_text"])          # a reply that names what the voice reads (a subset of the screen)
    # generated text (drafts, summaries, notes, quoted messages) never carries a flagged word, on screen or spoken
    md, body = abuse_guard.mask_text(md), abuse_guard.mask_text(body, spoken=True)
    return {"request_id": str(reply.get("request_id") or request_id or ""), "markdown": md,
            "tts": tts(body, _language(reply))}


# --------------------------------------------------------------------------
# chat memory: the context the reply no longer carries
# --------------------------------------------------------------------------

_MEMORY: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _chat_key(subject: str, chat_id: str | None) -> tuple[str, str]:
    return str(subject or "anonymous"), str(chat_id or "default")


def remember(subject: str, chat_id: str | None, context: Any) -> None:
    if not isinstance(context, dict) or not context:
        return
    mem = cfg().get("memory") or {}
    with _LOCK:
        if len(_MEMORY) >= int(mem.get("max_chats", 5000)):
            for key, _ in sorted(_MEMORY.items(), key=lambda kv: kv[1][0])[: max(1, len(_MEMORY) // 10)]:
                _MEMORY.pop(key, None)
        _MEMORY[_chat_key(subject, chat_id)] = (time.time() + float(mem.get("ttl_seconds", 1800)), dict(context))


def recall(subject: str, chat_id: str | None) -> dict[str, Any] | None:
    with _LOCK:
        found = _MEMORY.get(_chat_key(subject, chat_id))
        if not found:
            return None
        if found[0] < time.time():
            _MEMORY.pop(_chat_key(subject, chat_id), None)
            return None
        return dict(found[1])


def forget(subject: str, chat_id: str | None) -> None:
    with _LOCK:
        _MEMORY.pop(_chat_key(subject, chat_id), None)


__all__ = ["FLAG", "LINK", "ask", "cfg", "dedupe", "enabled", "forget", "link", "markdown", "parse", "publish",
           "recall", "registry", "remember", "request_for", "speakable", "tts"]
