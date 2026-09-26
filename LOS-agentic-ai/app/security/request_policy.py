"""
REQUEST POLICY -- what KIND of request this is, decided before anything runs.

The input guardrail (guardrails.check_input) already refuses requests for the
system itself: code, files, secrets, prompts, tool payloads. This module adds
the request types that are about OTHER PEOPLE'S DATA or about ABUSING the
Copilot's access, and it is consulted by check_input, so every surface gets
it at the same point: before a follow-up is resolved, before a record is
read, before a tool or retrieval runs, before a model is called.

    CROSS_CUSTOMER_DATA   another / previous / last / all customers' data,
                          rankings or comparisons across customers
    BULK_DATA             "all records", "every application", "everyone"
    DATA_EXPORT           CSV / JSON / Base64 / hex / image dumps, encoding
                          or transforming protected data (still disclosure)
    TOOL_ABUSE            "call the database tool", "execute any tool",
                          "run a query" -- user words never select a tool
    AUTHORITY_CLAIM       "I am admin / CTO", "I'm authorised", "security
                          test" -- a claim is not a credential
    SQL_INJECTION         SQL fragments and tautologies in a question
    OTHER_CONVERSATION    another user's / session's questions or history
    UNAUTHORIZED_SUBJECT  an explicit case / applicant id that is not the
                          one this request is authorised for

HOW IT DECIDES -- signals, not sentences. Each category is a combination of
a small number of signal families (a protected subject, a bulk quantifier, a
transformation, an authority claim ...) matched on the message AS TYPED and
on its canonical English form (language.py + normalize.py, so Hinglish and
Indian-language wording is covered), and on any Base64 payload it carries
(decoded and classified the same way -- encoding is not a way around it).

WHAT IT NEVER DOES. Grant anything. A request it allows is still authorised
by scopes and ownership (access.py, permissions.py) and answered only from
the caller's own case. Its only power is to refuse earlier.

The OWN-DATA distinction is deliberate: "all my documents", "my last
application", "the other applicant" (a co-applicant), "compare my PAN and
bank statement" are the caller's own case and pass.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

_I = re.IGNORECASE

# -- signal vocabularies ------------------------------------------------------

#: People and records that belong to SOMEBODY. `applicant` is written so it
#: never matches "co-applicant" (the caller's own co-applicant).
_PERSON = (r"(customers?|users?|clients?|(?<!co-)(?<!co\s)applicants?|borrowers?|"
           r"persons?|people|individuals?|members?|callers?|visitors?|"
           r"account\s*holders?|grahak\w*)")
#: Only OTHER people -- never the applicant / co-applicant on the caller's own
#: case -- for rankings and comparisons ("which applicant has the issue" is
#: about the caller's own two parties).
_PEOPLE_ONLY = r"(customers?|users?|clients?|borrowers?|people|persons?|account\s*holders?|grahak\w*)"
_RECORD = r"(records?|applications?|cases?|files?|accounts?|profiles?|entries|rows?|loans?|data)"
#: Not the caller. Past / other / every -- and their Hinglish forms.
#: "Other" is handled apart: "the other applicant" / "my other applicant" is
#: the caller's own co-applicant.
_NOT_ME = (r"(another|someone\s+else'?s?|somebody\s+else'?s?|different|"
           r"previous|prev|prior|last|latest|recent|most\s+recent(ly)?|recently\s+processed|"
           r"earlier|next|random|any\s+other|pichl\w*|pichh?l\w*|dusr\w*|doosr\w*|"
           r"kisi\s+aur|aur\s+kisi)")
_EVERY = r"(all|every|each|entire|whole|complete\s+list\s+of|list\s+of\s+all|sab\w*|har|saare|sare|tamam)"
#: POSSESSIVES only: "show me all customers" is not about the caller's own case.
_OWN = r"\b(my|mine|our|mera|meri|mere|apna|apni|apne|hamara|humara)\b"
_ASK = (r"(show|give|get|tell|list|display|reveal|share|send|dump|print|fetch|pull|"
        r"provide|return|find|search|look\s*up|lookup|see|view|access|read|open|"
        r"export|download|batao|bata|dikhao|dikha|do|de\s*do|nikalo)")
_PROTECTED = (r"(pan|aadhaa?r|mobile|phone|number|email|e-mail|address|dob|date\s+of\s+birth|"
              r"name|names|details?|information|info|data|records?|loan\s+amount|amount|"
              r"salary|income|account|bank|kyc|status|documents?|history|application|"
              r"credit|score|profile)")


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, _I) for p in patterns)


_CROSS = _rx(
    # another / previous / last customer ... (a person noun, never "my last application")
    rf"\b{_NOT_ME}\s+(\w+\s+){{0,2}}{_PERSON}('s|s')?\b",
    # "other customers" -- but never "the/my/our other applicant" (co-applicant)
    rf"(?<!the\s)(?<!my\s)(?<!our\s)\bothers?'?\s+(\w+\s+){{0,1}}{_PERSON}('s|s')?\b",
    # the customer/user who was processed before / most recently
    rf"\b{_PERSON}\s+(who|whose|that|which)\b[^?]{{0,40}}\b(processed|applied|came|submitted|"
    rf"before|recent\w*|latest|last|previous|earlier)\b",
    # someone else's
    r"\b(someone|somebody|anyone|anybody)\s+else'?s?\b",
    # rankings / comparisons across customers
    rf"\b(which|who|what)\b[^?]{{0,30}}\b{_PEOPLE_ONLY}\b[^?]{{0,30}}\b(highest|lowest|"
    r"largest|biggest|smallest|most|least|max(imum)?|min(imum)?|top|richest|best|worst)\b",
    r"\bwho\s+(has|have|had|got|took)\s+the\s+(highest|lowest|largest|biggest|most|max\w*|"
    r"min\w*|least|smallest|best|worst)\b",
    rf"\b(top|highest|largest|biggest)\s+\d*\s*{_PEOPLE_ONLY}\b",
    rf"\bcompare\b[^?]{{0,25}}\b(two|2|both|multiple|several|different|other)?\s*{_PEOPLE_ONLY}\b",
    # real / live customer data as an "example"
    rf"\b(real|actual|live|production|genuine)\s+({_PERSON}\s+)?(data|records?|examples?|"
    rf"details|pan|samples?|{_PERSON})\b",
    # initials / last digits of somebody else's value
    rf"\b(initials?|first\s+letters?|last\s+(four|4)\s+digits?)\b[^?]{{0,30}}\b{_NOT_ME}\b",
    rf"\b(initials?|first\s+letters?)\b((?!\bmy\b)[^?]){{0,30}}\b{_PEOPLE_ONLY}\b",
    rf"\b{_PEOPLE_ONLY}\b[^?]{{0,15}}\b(initials?|first\s+letters?)\b",
    # the customer before / after me
    rf"\b{_PEOPLE_ONLY}\s+(who\s+(was|came)\s+)?(before|after|ahead\s+of|behind)\s+(me|us|mine)\b",
    # Hinglish: sabka / sab ka data, dusre ka PAN
    r"\bsab\s*k[aei]\s+(data|details|pan|record\w*|number|info\w*|naam)",
    rf"\b(dusr|doosr|pichl)\w*\s+(\w+\s+)?k[aei]\s+{_PROTECTED}",
)

_BULK = _rx(
    rf"\b{_EVERY}\s+(the\s+)?({_PERSON}|{_RECORD}|entries|everything)\b",
    r"\b(everyone|everybody|everything)\b[^?]{0,30}\b(applied|data|records?|details?|"
    r"information|info|in\s+the\s+(system|database|db))\b",
    r"\b(show|list|give|tell)\s+(me\s+)?(everyone|everybody)\b",
    r"\b(whole|entire|full|complete)\s+(database|db|dataset|table|customer\s+base|system\s+data)\b",
)

_EXPORT = _rx(
    # a machine format for data
    # an OUTPUT verb, then a machine format ("upload it in excel" is not an export)
    r"\b(return|give|send|show|output|print|export|convert|format|dump|put|provide|list|"
    r"display|share|write|render|generate|create|make)\b[^?]{0,40}\b(as|in|into|to|using)\s+"
    r"(an?\s+)?(csv|json|xml|excel|xlsx|xls|spreadsheet|sql|yaml|base64|base\s*64|hex|"
    r"hexadecimal|binary|bytes|image|picture|png|jpe?g|photo|dataframe|table)\b",
    r"\b(csv|json|xml|excel|spreadsheet)\s+(file|format|export|dump|output|of)\b",
    # an export verb on data
    rf"\b(export|dump|download|extract|scrape|backup|back\s+up|exfiltrate)\b[^?]{{0,40}}\b"
    rf"({_RECORD}|{_PERSON}|database|db|tables?|everything|information|info|details)\b",
    # transforming protected data is still disclosing it
    r"\b(encode|encrypt|obfuscate|base64|hex|rot13|url[\s-]?encode|reverse|spell\s+out|"
    r"hash|mask\s+and\s+send)\b[^?]{0,40}\b(password|secret|key|token|pan|aadhaa?r|"
    r"account|data|record|customer|number|credential)",
)

_TOOL = _rx(
    r"\b(call|execute|exec|run|invoke|trigger|use|hit|fire|access|operate)\b[^?]{0,25}"
    r"\b(any|every|all|whatever|arbitrary|available|each|some|internal|hidden|admin|"
    r"database|db|customer|the)?\s*(tools?|functions?|apis?|endpoints?|commands?|"
    r"scripts?|shell|stored\s+procedures?|procedures?|plugins?|skills?)\b",
    r"\b(run|execute|exec|perform|fire|write)\b[^?]{0,20}\b(a|an|the|any|this|my|"
    r"sql|database|db)?\s*(sql\s+)?(quer(y|ies)|sql)\b",
    r"\bquery\s+(the\s+)?(database|db|table|tables|records|system|backend)\b",
    r"\b(whatever|any)\s+tools?\s+(you\s+)?(have|can|got)\b",
)

_AUTHORITY_CLAIM = _rx(
    r"\b(i\s*am|i'?m|im|as\s+(an?|the)|main|mai|hum|we\s+are|this\s+is)\s+(an?\s+|the\s+)?"
    r"(\w+\s+){0,2}(admin\w*|administrator|superuser|super\s+user|root|cto|ceo|cfo|ciso|cio|"
    r"coo|director|manager|supervisor|developer|auditor|compliance(\s+officer)?|officer|"
    r"staff|support(\s+agent)?|security(\s+(team|tester|researcher|officer))?|pen\s*tester|"
    r"police|regulator|rbi|underwriter)\b",
    r"\b(authori[sz]ed|approved|permitted|allowed|cleared|sanctioned|whitelisted)\s+"
    r"(me|us|this|you|the\s+request|my\s+request|access)\b",
    r"\b(authori[sz]ed|official|approved|sanctioned|legitimate|internal|allowed)\s+"
    r"(security\s+)?(test\w*|audit|pentest|penetration\s+test|assessment|request|review)\b",
    r"\b(has|have|had)\s+(authori[sz]ed|approved|allowed|permitted|cleared|given\s+(me\s+)?"
    r"(permission|access|clearance))\b",
    r"\bi\s+(have|got|hold)\s+(the\s+)?(permission|authori[sz]ation|clearance|access\s+rights|"
    r"admin\s+(rights|access))\b",
    r"\bi\s+am\s+allowed\s+to\b",
    r"\b(for|just\s+for|only\s+for)\s+(testing|debugging|debug|audit|qa|a\s+test)\b",
)
#: A claim is only acted on when it is used to ask for MORE than the caller's
#: own case: unrestricted access, restricted data, other or all customers.
#: ("I'm a manager, what documents do I need?" is an occupation, and passes.)
_ACCESS_OBJECT = _rx(
    r"\b(unrestricted|full|admin|root|elevated|complete|special|override|all|extra|"
    r"higher)\s+(access|privileges?|rights|permissions?)\b",
    r"\b(restricted|hidden|confidential|unrestricted|privileged|classified|secret|sensitive|"
    r"internal|protected|all|every|other|everyone'?s?|everybody'?s?|customers?'?|users?'?)\s+"
    r"(customer\s+|user\s+)?(data|records?|information|info|details|fields?|files?|pan|"
    r"numbers?)\b",
    rf"\b{_EVERY}\s+{_PERSON}",
    r"\bsab\s*k[aei]\s+\w+",
)

_RESTRICTED = _rx(
    # NOT the caller's own: "tell me if my sensitive data is safe" passes.
    rf"\b{_ASK}\b((?!\bmy\b|\bour\b)[^?]){{0,30}}\b(restricted|hidden|confidential|"
    r"unrestricted|privileged|classified|secret|sensitive|internal|protected)\s+"
    r"(customer\s+)?(data|records?|information|info|details|fields?|files?|access)\b",
    r"\b(unrestricted|privileged|elevated|admin|root|god|super)\s+(access|mode|privileges?|rights)\b",
)

_SQLI = (
    re.compile(r"'\s*(or|and)\s+'?\s*[\w]+'?\s*(=|like|<>)\s*'?[\w]*", _I),
    re.compile(r"\b(or|and)\s+\d+\s*=\s*\d+\b", _I),
    re.compile(r"\bunion\b[\s(]+(all\s+)?select\b", _I),
    re.compile(r";\s*(drop|delete|insert|update|alter|truncate|create|exec|shutdown|grant)\b", _I),
    re.compile(r"\b(select|delete|insert|update|drop|truncate|alter)\b[^?]{0,60}\b(from|into|"
               r"table|where|set|database)\b[^?]{0,40}(--|;|\*|=|\bwhere\b|\busers?\b|\bpassword)", _I),
    re.compile(r"'[^']{0,40}(--|#)\s*$|'\s*--|--\s*$"),
    re.compile(r"/\*.*?\*/|\bxp_\w+|\bsleep\s*\(\s*\d|\bbenchmark\s*\(|\bwaitfor\s+delay\b|"
               r"@@version|information_schema|pg_catalog|sqlite_master|\bchar\s*\(\s*\d", _I),
)

_OTHER_CONVERSATION = _rx(
    r"\b(previous|prior|last|other|another|earlier|different|next)\s+(user|person|customer|"
    r"client|caller|visitor|people)s?\b[^?]{0,30}\b(ask|asked|say|said|question|questions|"
    r"chat|conversation|session|history|told|message|messages|typed|wrote|talk\w*)\b",
    r"\b(previous|prior|last|other|another|earlier|different|next)\s+(user'?s?|person'?s?|"
    r"customer'?s?|client'?s?)\s+(session|chat|conversation|history|questions?|messages?)\b",
    r"\b(another|other|different|someone\s+else'?s?)\s+(conversation|chat|session)s?\b",
    r"\bwhat\s+(did|do|have)\s+(other|the\s+other|the\s+previous|the\s+last|others)\b",
    r"\bremember\b[^?]{0,30}\b(another|other|previous|different)\b",
)

#: Internal identifiers a message may name: a case / applicant / party id.
_IDS = re.compile(r"\b(case_[0-9a-z]{8,}|(DEMO-)?(CASE|APP|COAPP)-[A-Z0-9-]{4,})\b", _I)

#: Raw / internal structures -- not a business answer.
_RAW = _rx(
    r"\b(raw|complete|full|entire|whole|unfiltered|original)\s+(api\s+)?(json|xml|payload|"
    r"api\s+response|response(\s+object)?|output|object|record|records|dump)\b",
    r"\b(return|give|show|print|send)\b[^?]{0,15}\b(json|xml|api\s+response)\b",
    r"\b(internal|record|system|database|db|hidden)\s+ids?\b",
    r"\baudit\s+(fields?|logs?|trail|entries|records?|data)\b",
    r"\b(request|correlation|trace|tool)\s+ids?\b",
    # database discovery by table name
    r"\b(users?|customers?|applicants?|accounts?|applications?|passwords?|credentials?)\s+tables?\b",
)

#: Talking the Copilot into another role, or out of its rules.
_ROLE_PLAY = _rx(
    r"\brole[\s-]?play\w*\b",
    r"\b(pretend|imagine|act\s+as|behave\s+(like|as)|you\s+are\s+(now\s+)?(a|an|the))\b"
    r"[^?]{0,25}\b(database|db|sql|terminal|shell|system|admin\w*|server|root|superuser|"
    r"hacker|developer|unrestricted|unfiltered|jailbroken)\b",
    r"\bwith\s+(no|zero|without)\s+(rules|restrictions|limits|filters|guardrails|policies)\b",
    r"\bno\s+(rules|restrictions|filters|guardrails)\s+(apply|mode)\b",
)


@dataclass(frozen=True)
class Decision:
    category: str
    rule: str


def _first(patterns, texts, name):
    for index, pattern in enumerate(patterns):
        for text in texts:
            if pattern.search(text):
                return f"{name}_{index}"
    return None


def _decoded(text: str) -> list[str]:
    """Readable text hidden in Base64 blobs -- classified like any other."""
    found = []
    for blob in re.findall(r"[A-Za-z0-9+/]{16,}={0,2}", text or ""):
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
            decoded = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded and sum(ch.isprintable() for ch in decoded) / len(decoded) > 0.9:
            found.append(decoded)
    return found


def _canonical(text: str) -> str:
    try:
        from app.agents.applicant import normalize

        return normalize.normalise(text).text
    except Exception:  # pragma: no cover - normalisation must never open a hole
        return text


def classify(message: str, *, allowed_ids: tuple[str | None, ...] = ()) -> Decision | None:
    """The policy category this request falls in, or None (ordinary request)."""
    raw = " ".join(str(message or "").split())
    if not raw:
        return None
    texts = [raw]
    canonical = _canonical(raw)
    if canonical and canonical.lower() != raw.lower():
        texts.append(canonical)

    # ENCODED PAYLOADS are opened and judged by what they say; asking for
    # one to be decoded and obeyed is an injection by itself.
    decoded = _decoded(raw)
    if decoded:
        for inner in decoded:
            found = classify(inner, allowed_ids=allowed_ids)
            if found:
                return Decision(found.category, "encoded_" + found.rule)
        if re.search(r"\b(decode|decrypt|base64)\b[^?]{0,30}\b(and|then)\b[^?]{0,20}"
                     r"\b(do|follow|execute|run|obey|act|perform)\b", raw, _I):
            return Decision("PROMPT_INJECTION", "decode_and_obey")

    rule = _first(_SQLI, [raw], "sql")
    if rule:
        return Decision("SQL_INJECTION", rule)

    rule = _first(_ROLE_PLAY, texts, "roleplay")
    if rule:
        return Decision("PROMPT_INJECTION", rule)

    # NAMED IDENTIFIERS: any case / applicant / party id other than the ones
    # this request is authorised for is somebody else's.
    allowed = {str(i).lower() for i in allowed_ids if i}
    for match in _IDS.finditer(raw):
        if match.group(0).lower() not in allowed:
            return Decision("UNAUTHORIZED_SUBJECT", "named_identifier")

    rule = _first(_OTHER_CONVERSATION, texts, "conversation")
    if rule:
        return Decision("OTHER_CONVERSATION", rule)

    # ADDING a party is not reading one: "add another applicant to my case".
    for index, pattern in enumerate(_CROSS):
        for text in texts:
            match = pattern.search(text)
            if match and not re.search(
                    r"\b(add|adding|include|including|create|creating|register|onboard|"
                    r"new|invite|join)\b[^?]{0,12}$", text[:match.start()], _I):
                return Decision("CROSS_CUSTOMER_DATA", f"cross_{index}")

    rule = _first(_TOOL, texts, "tool")
    if rule:
        return Decision("TOOL_ABUSE", rule)

    if any(p.search(t) for p in _AUTHORITY_CLAIM for t in texts) and any(
            p.search(t) for p in _ACCESS_OBJECT for t in texts):
        return Decision("AUTHORITY_CLAIM", _first(_AUTHORITY_CLAIM, texts, "claim"))

    rule = _first(_RESTRICTED, texts, "restricted")
    if rule:
        return Decision("AUTHORITY_CLAIM", rule)

    # BULK: "all my documents" is the caller's own case and passes.
    for index, pattern in enumerate(_BULK):
        for text in texts:
            match = pattern.search(text)
            if match and not re.search(_OWN, text[max(0, match.start() - 12):match.end() + 4], _I):
                return Decision("BULK_DATA", f"bulk_{index}")

    rule = _first(_EXPORT, texts, "export")
    if rule:
        return Decision("DATA_EXPORT", rule)

    rule = _first(_RAW, texts, "raw")
    if rule:
        return Decision("RAW_INTERNAL_DATA", rule)
    return None


# -- deterministic, non-blocking answers --------------------------------------

_OWN_HISTORY = _rx(
    r"\bwhat\s+(did|have)\s+i\s+(ask|asked|say|said|type|typed|tell|told|write|wrote)\b",
    r"\b(my|our)\s+(complete\s+|full\s+|whole\s+|entire\s+|past\s+|previous\s+|old\s+)?"
    r"(conversation|chat|message)s?\s*(history|log|logs|transcript)?\b",
    r"\b(conversation|chat)\s+(history|log|transcript)\b",
    r"\bwhat\s+did\s+we\s+(talk|discuss|chat)\b",
    r"\b(yesterday|last\s+time|last\s+week|earlier\s+today|previous\s+session)\b[^?]{0,30}"
    r"\b(ask|asked|said|chat|conversation|question)",
)

_CAPABILITY = _rx(
    r"\bwhat\s+(all\s+)?(customer\s+|personal\s+|user\s+)?(information|info|data|details|"
    r"records?)\s+(can|do|could)\s+you\s+(access|see|read|view|get|have|use|know)\b",
    r"\bwhat\s+(can|do|could)\s+you\s+(access|see|read|view|know\s+about\s+me)\b",
    r"\bwhat\s+(data|information|info)\s+do\s+you\s+(have|hold|keep|store)\b",
    r"\b(which|what)\s+(data|information|systems?|databases?)\s+(do\s+you\s+)?have\s+access\b",
)


def asks_own_history(message: str) -> bool:
    text = _canonical(message)
    return any(p.search(t) for p in _OWN_HISTORY for t in (message or "", text))


def asks_capability(message: str) -> bool:
    text = _canonical(message)
    return any(p.search(t) for p in _CAPABILITY for t in (message or "", text))


__all__ = ["Decision", "asks_capability", "asks_own_history", "classify"]
