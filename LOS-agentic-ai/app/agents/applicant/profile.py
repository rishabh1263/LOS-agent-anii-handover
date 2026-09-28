"""
The caller's OWN application details -- the basic information FOS captured.

    "What loan amount did I enter?"   -> application.loan_amount
    "Which mobile is registered?"     -> applicant.mobile
    "What is the employment type?"    -> application.employment_type
    "What is my case id?"             -> application.case_id
    "Show my application details"     -> every recorded field, with its value

FROM THE RECORD, AND ONLY THE RECORD. Every value is read through the
applicant.get / application.get tools for the case the caller is authorised
for -- the same persisted applicant and application rows the FOS form wrote.
Never from conversation memory, retrieval or a model; never inferred. A field
the record does not carry is said to be not recorded, never guessed, never
replaced by an identifier.

IDENTIFIERS. The application is referenced by its CASE ID and the applicant
by the APPLICANT ID -- the two public identifiers every request already
carries. There is no separate application number, and no internal row id
is ever published.

FIELDS, NOT SENTENCES. A question names a field by its vocabulary (the
patterns below are field synonyms, not question templates); the capability
registry (capabilities.py) says which fields exist and may be published.

SENSITIVITY. Each value passes app/security/sensitivity.py before it is
said: business fields in full, personal fields per the configured disclosure,
high-sensitivity identifiers masked, credentials never.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.agents.applicant import capabilities

ALL = "ALL"                         # every recorded detail, applicant and application
ALL_APPLICANT = "ALL_APPLICANT"     # the applicant's own details
ALL_APPLICATION = "ALL_APPLICATION" # the application's recorded fields
ALL_FINANCIAL = "ALL_FINANCIAL"     # the recorded financial fields
FIELDS_AVAILABLE = "FIELDS_AVAILABLE"
APPLICATION_REF = "application_ref" # "application number": the case ID, stated as such

#: (field, label, VOCABULARY of the field). The vocabulary is the words a
#: person uses for the field, in any question -- never a whole sentence.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("loan_amount", "loan amount",
     # RECORDED amounts only: "how much CAN I borrow" is an eligibility
     # question, and an EMI / income / balance amount is a different figure.
     r"^(?!.*\b(emi|instal\w*|foir|income|salary|balance|credit|fee|charges?|obligation\w*|"
     r"property)\b)(.*\b(loan\s+)?(amount|amt|principal)\b|.*\bhow\s+much\b[^?]{0,20}"
     r"\b(did|have|had)\s+(i|we|the\s+applicant|the\s+customer)\b|"
     r".*\bkitna\s+loan\b|.*\bloan\s+(value|size)\b|.*\b(requested|asked\s+for)\s+loan\b)"),
    ("tenure_months", "tenure",
     r"\b(tenure|loan\s+term|term\s+of\s+(the\s+|my\s+)?loan|repayment\s+(period|term)|"
     r"how\s+many\s+months|duration\s+of\s+(the\s+|my\s+)?loan|avadhi|अवधि)\b"),
    ("interest_rate_pct", "interest rate",
     r"\b(interest\s+rate|rate\s+of\s+interest|roi|interest|byaj|ब्याज|व्याज)\b"),
    ("product", "product",
     r"\b(product|loan\s+type|type\s+of\s+loan|kind\s+of\s+loan|scheme)\b"
     r"|\b(which|what)\s+loan\s+(did|have|am|was)\b"),
    ("employment_type", "employment type",
     r"\b(employment(\s+type)?|occupation|job\s+type|profession|work\s+type|"
     r"salaried\s+or\s+self|naukri|रोज़गार|व्यवसाय)\b"),
    ("declared_monthly_income", "declared monthly income",
     r"\b(declared\s+)?(monthly\s+)?income\b(?!\s+(proof|document|evidence|verif\w*))"
     r"|\b(salary\s+declared|declared\s+salary|aay|आय|उत्पन्न)\b"),
    ("declared_monthly_obligations", "declared monthly obligations",
     r"\b(obligations?|existing\s+emis?|monthly\s+emis?|liabilit\w*|outgoings?|"
     r"existing\s+loans?\s+emi|dues)\b"),
    ("property_value", "property value",
     r"\b(property\s+(value|price|worth|cost)|value\s+of\s+(the\s+)?property|"
     r"collateral\s+value|sampatti|संपत्ति)\b"),
    ("mobile", "mobile number",
     r"\b(mobile|phone|cell|contact)(\s+(no|number|num))?\b|\bcontact\s+(details)\b|"
     r"\b(my|the|applicant'?s?|customer'?s?)\s+number\b|\bmobile\s+no\b"),
    ("email", "email",
     r"\b(e-?mail|mail\s+id)(\s+(id|address))?\b"),
    ("date_of_birth", "date of birth",
     r"\b(dob|d\.o\.b|date\s+of\s+birth|birth\s*date|birthday|born|janm\s*tithi|जन्म)\b"),
    ("address", "address",
     r"\baddress\b(?!\s+proof)|\bpata\b|\bपता\b"),
    ("aadhaar_number", "Aadhaar number",
     r"\b(aadhaa?r|adhar|aadhar|uidai|uid)\s*(card\s*)?(number|no|num|id)?\b|\bआधार\b"),
    ("pan_number", "PAN number",
     r"\bpan\s*(card\s*)?(number|no|num)\b|\bpan\s+card\b|\bपैन\b"),
    ("bank_account_number", "bank account number",
     r"\b(bank\s+)?(account|a/c|acct)\s*(number|no|num)\b|\bkhata\s*(number|no)?\b|\bखाता\b"),
    ("case_id", "case ID",
     r"\b(case|file)\s*(id|no|number|num|ref|reference)\b|\bcase\s+id\b"
     r"|\bcase\s+(and|&|or|aur|और)\s+(application|applicant|app)\s*(id|no|number|ids)\b"),
    (APPLICATION_REF, "application reference",
     r"\b(application|app|loan)\s*(id|no|number|num|ref|reference)\b"
     r"|\b(application|app)\s+(and|&|or|aur|और)\s+case\s*(id|no|number|ids)\b"),
    ("applicant_id", "applicant ID",
     r"\b(applicant|customer)\s*(id|no|number|num|ref|reference)\b"
     r"|\b(applicant)\s+(and|&|or|aur|और)\s+case\s*(id|no|number|ids)\b"),
    ("full_name", "name",
     r"\b(name|naam|full\s+name|नाम|नाव)\b(?!\s+(mismatch|match|on\s+(my|the)\s+(pan|aadhaar|bank|passport)))"),
)
_FIELD_INDEX = {name: (label, re.compile(pattern, re.I)) for name, label, pattern in _FIELDS}

#: A RECORDED-VALUE cue: a possessive, "did I / have I", or the record
#: itself. A bare "I" is not one: "I am self-employed, what do I need?" asks
#: about requirements, not about what was recorded.
_OWNERSHIP = re.compile(
    r"\b(my|mine|our|mera|meri|mere|hamara|humara|registered|recorded|provided?|entered|"
    r"submitted|submit|gave|given|filled|on\s+file|this\s+(case|application)|"
    r"the\s+applicant|applicant'?s|application|applied|customer'?s?|borrower'?s?)\b"
    r"|\b(did|have|had)\s+(i|we)\b", re.I)
_ASKING = re.compile(
    r"\b(what|which|whats|how|tell|show|give|confirm|check|do\s+you\s+have|is\s+there|kya|kitna|"
    r"kaunsa|batao|bata|share|provide|display|fetch)\b|\?\s*$", re.I)
#: Words around a bare field name that add nothing to it.
_BARE_FILLER = re.compile(
    r"\b(my|mine|our|mera|meri|mere|please|pls|plz|batao|bata|bataiye|do|the|kya|hai|hain|"
    r"what|is|whats|tell|me|show|give|complete|full|entire|exact|current|registered|recorded|"
    r"customer'?s?|applicant'?s?|of|for|this|that)\b|[?.!,]", re.I)
_WRITE = re.compile(
    r"\b(update|change|correct|edit|modify|set|add|capture|save|replace|fix|remove|delete)\b", re.I)
#: A public identifier the caller named ("show me application APP-…"). The
#: guardrail already refused any id that is not the caller's own.
_PUBLIC_ID = re.compile(r"\b([A-Za-z]{2,12}-[A-Za-z0-9]{4,}|case_[a-z0-9]{8,})\b")

#: Broad requests: everything recorded, about the applicant, the
#: application, or its financial fields. Concept words, not sentences.
_BROAD = re.compile(
    r"\b(details?|information|info|data|all\s+(the\s+)?(information|details|fields|data)|"
    r"particulars|puri\s+jankari|सारी\s+जानकारी|पूरी\s+माहिती|सर्व\s+माहिती)\b", re.I)
#: The whole-picture summary and the applicant card are intents of their own
#: (FULL_SUMMARY, APPLICANT_DETAILS): a request phrased that way keeps them.
_LEGACY_BROAD = re.compile(
    r"\b(history|timeline|findings?|events?|status|stage|"
    r"summary|summari[sz]e|overview|everything|full\s+picture|complete\s+picture|"
    r"applicant\s+details|basic\s+details|who\s+is\s+the\s+applicant|quick\s+overview|"
    r"where\s+(does|is)\s+(this|the)\s+case\s+stand)\b", re.I)
_ABOUT_APPLICANT = re.compile(
    r"\b(applicant|customer|borrower|personal|basic|contact|about\s+me|my\s+(basic\s+|personal\s+)?"
    r"(details|information|info|profile)|grahak)\b", re.I)
_ABOUT_APPLICATION = re.compile(r"\b(application|case|file|loan)\b", re.I)
_FINANCIAL = re.compile(r"\b(financial|finances|money|amounts?|figures|numbers)\b", re.I)
_FIELDS_LISTED = re.compile(
    r"\b(fields?|attributes?|columns?)\b[^?]{0,20}\b(available|recorded|exist|there|have|captured)\b"
    r"|\b(which|what)\s+(fields?|attributes?|information|details)\s+(are|is)\s+(available|recorded)\b",
    re.I)
#: "show me application <id>" / "open my case" / "give details for case ...":
#: the record itself, asked for by a verb of showing and nothing else.
_OPEN_RECORD = re.compile(
    r"^\s*(show|give|open|display|pull\s+up|bring\s+up|fetch|get)\s+(me\s+)?(the\s+|this\s+|my\s+)?"
    r"details?\s+(for|of|about)\s+(the\s+|this\s+|my\s+)?(application|case)\s*(\S+)?\s*[?.!]*\s*$",
    re.I)
#: Income / obligation questions about EVIDENCE, review or eligibility are
#: answered from the recorded verdicts (INCOME_EVIDENCE, ELIGIBILITY), not
#: from the declared form value.
_ASSESSMENT_WORDS = re.compile(
    r"\b(eligib\w*|considered|evidenc\w*|verif\w*|mismatch|review\w*|proof|statement|slip|"
    r"used\s+for|assess\w*|why|computed|calculated|foir)\b", re.I)
_EVERYTHING = re.compile(
    r"\bwhat\s+(all\s+)?(information|details|info|data)\s+(have|did)\s+(i|we)\s+"
    r"(submit\w*|provid\w*|give|given|enter\w*|fill\w*|share\w*)\b"
    r"|\bwhat\s+(all\s+)?(have|did)\s+(i|we)\s+(submit\w*|provid\w*|fill\w*|enter\w*)\b"
    r"|\b(show|give|tell)\s+(me\s+)?my\s+(basic\s+|personal\s+)?(details|information|info|profile)\b"
    r"|^\s*my\s+(basic\s+|personal\s+)?(details|information|info|profile)\s*\??\s*$", re.I)
#: "Are my basic details complete?" -- answered by the missing-information intent.
COMPLETENESS = re.compile(
    r"\b(are|is)\s+(my|the|our)\s+(basic\s+|personal\s+|applicant\s+)?(details|information|info|"
    r"profile|data)\s+(complete|completed|filled|done|full|captured)\b"
    r"|\b(details|information|info)\s+(are\s+|is\s+)?(still\s+)?(missing|pending|left|incomplete)\b"
    r"|\bwhat\s+(details|information|info)\s+(are|is)\s+(still\s+)?(missing|pending|left|needed)\b",
    re.I)
#: Document words: "the name on my PAN" is a document question, left alone.
_DOCUMENT_WORDS = re.compile(
    r"\b(pan|aadhaar|aadhar|passport|voter|licence|license|statement|slip|itr|form\s*16|"
    r"documents?|proof|checklist|upload\w*|verif\w*)\b", re.I)


@dataclass(frozen=True)
class Question:
    field: str          # a field name, a bundle (ALL*, FIELDS_AVAILABLE), or several joined by "+"

    @property
    def fields(self) -> list[str]:
        return [f for f in self.field.split("+") if f]


def _broad(said: str) -> Question | None:
    """A request for everything recorded, or None."""
    if _EVERYTHING.search(said):
        return Question(ALL)
    if _FIELDS_LISTED.search(said):
        return Question(FIELDS_AVAILABLE)
    if _LEGACY_BROAD.search(said) or _ASSESSMENT_WORDS.search(said):
        return None                                  # a verdict / evidence question
    if _BROAD.search(said) and not _DOCUMENT_WORDS.search(said):
        if _FINANCIAL.search(said):
            return Question(ALL_FINANCIAL)
        if _ABOUT_APPLICANT.search(said) and not _ABOUT_APPLICATION.search(said):
            return Question(ALL_APPLICANT)
        if _ABOUT_APPLICATION.search(said):
            return Question(ALL_APPLICATION)
        if _OWNERSHIP.search(said):
            return Question(ALL)
    if _PUBLIC_ID.search(said) and _ABOUT_APPLICATION.search(said) \
            and not _DOCUMENT_WORDS.search(said):
        return Question(ALL_APPLICATION)             # "show me application <own id>"
    if _OPEN_RECORD.search(said) and not _DOCUMENT_WORDS.search(said):
        return Question(ALL_APPLICATION)             # "show me the application" / "open my case"
    if _FINANCIAL.search(said) and re.search(r"\b(key|main|recorded|all)\b", said, re.I):
        return Question(ALL_FINANCIAL)
    return None


def _named_fields(said: str) -> list[str]:
    """Every field the question names, in the order it names them."""
    found: list[tuple[int, str]] = []
    for name, (_label, pattern) in _FIELD_INDEX.items():
        match = pattern.search(said)
        if match:
            found.append((match.start(), name))
    fields = [name for _pos, name in sorted(found)]
    # "loan amount" also matches the loan-amount pattern through "amount":
    # the more specific field wins over a generic one at the same words.
    if "loan_amount" in fields and any(f in fields for f in ("declared_monthly_income",
                                                              "declared_monthly_obligations",
                                                              "property_value")):
        fields.remove("loan_amount")
    if APPLICATION_REF in fields and "applicant_id" in fields:
        fields.remove("applicant_id")
    return fields


def detect(text: str) -> Question | None:
    """Which recorded detail(s) the question asks about, or None."""
    said = " ".join(str(text or "").split())
    if not said or _WRITE.search(said):
        return None
    broad = _broad(said)
    if broad is not None:
        return broad
    asked = bool(_OWNERSHIP.search(said) or _ASKING.search(said))
    if not asked:
        bare = _BARE_FILLER.sub(" ", said).strip()
        if not bare or len(bare.split()) > 4:
            return None
        said = bare
    fields = _named_fields(said)
    if not fields:
        return None
    # "the name on my PAN" / "the address on my Aadhaar" is a DOCUMENT
    # question; a document word beside a field that only a document carries
    # leaves it to DOCUMENT_DETAILS. A field the form records (loan amount,
    # tenure, mobile ...) is answered whatever else the sentence mentions.
    if _DOCUMENT_WORDS.search(said) and all(f in ("full_name", "address", "date_of_birth")
                                            for f in fields):
        return None
    if _ASSESSMENT_WORDS.search(said) and all(f in ("declared_monthly_income",
                                                    "declared_monthly_obligations")
                                              for f in fields):
        return None
    return Question("+".join(fields))


def label(field: str) -> str:
    if field in _FIELD_INDEX:
        return _FIELD_INDEX[field][0]
    return field.replace("_", " ")


def _rupees(raw: str) -> str | None:
    """₹ in Indian grouping, only when the recorded value is a plain number."""
    digits = re.sub(r"[,\s₹]|rs\.?|inr", "", str(raw or ""), flags=re.I)
    if not re.fullmatch(r"\d+(\.\d{1,2})?", digits):
        return None
    whole, _, paise = digits.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        whole = f"{head},{tail}"
    return f"₹{whole}" + (f".{paise}" if paise and paise.strip("0") else "")


def _date(raw: str) -> str:
    from datetime import date

    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(raw or "").strip()[:10])
    if not match:
        return str(raw)
    try:
        return date(int(match[1]), int(match[2]), int(match[3])).strftime("%d %B %Y").lstrip("0")
    except ValueError:
        return str(raw)


def _readable(value: str) -> str:
    return " ".join(w.capitalize() for w in str(value).replace("_", " ").split())


def _values(results: dict[str, Any]) -> dict[str, Any]:
    """The recorded values, from the tool results -- only registry fields."""
    from app.agents.applicant.answer import _get

    applicant = _get(results, "applicant.get", "applicant") or {}
    application = _get(results, "application.get", "application") or {}
    return {**{k: applicant.get(k) for k in capabilities.APPLICANT_FIELDS},
            **{k: application.get(k) for k in capabilities.APPLICATION_FIELDS},
            "_missing": list(applicant.get("missing_fields") or [])}


def _shown(field: str, value: Any) -> str | None:
    """The value as it may be said, or None when the policy withholds it."""
    from app.security import sensitivity

    if field in capabilities.IDENTIFIER_FIELDS:
        return str(value)                        # the caller's own public reference
    disclosure = sensitivity.disclosure(field)
    if disclosure == "withhold":
        return None
    shown = sensitivity.mask(str(value)) if disclosure == "masked" else str(value)
    if field in ("address", "full_name"):
        shown = sensitivity.mask_identifiers(shown)   # a number typed into a text field
    if field in ("loan_amount", "declared_monthly_income", "declared_monthly_obligations",
                 "property_value"):
        return _rupees(value) or shown
    if field == "product":
        return _readable(shown)
    if field == "employment_type":
        return _readable(shown).lower()
    if field == "tenure_months":
        return f"{shown} months"
    if field == "interest_rate_pct":
        return f"{shown}%"
    if field == "date_of_birth":
        return _date(shown) if disclosure == "full" else shown
    return shown


def _said(field: str, value: Any, language: str | None = None) -> str:
    from app.agents.applicant import phrasing

    shown = _shown(field, value)
    if shown is None:
        return f"For your security, I can't show your {label(field)} here."
    # THE SAME VALUE, in one of several sentence shapes (phrasing.py), chosen
    # by the turn's seed -- and in the language the question was typed in.
    varied = phrasing.field_sentence(field, str(shown), language=language,
                                     seed=phrasing.current_seed(field))
    if varied:
        return varied
    if field == "loan_amount":
        return f"Your application records show a loan amount of {shown}."
    if field == "product":
        return f"You applied for a {shown}."
    if field == "employment_type":
        return f"Your employment type is recorded as {shown}."
    if field == "tenure_months":
        return f"Your application has a tenure of {shown}."
    if field == "interest_rate_pct":
        return f"The interest rate on your application is {shown}."
    if field == "declared_monthly_income":
        return f"Your declared monthly income is {shown}."
    if field == "declared_monthly_obligations":
        return f"Your declared monthly obligations are {shown}."
    if field == "property_value":
        return f"The property value recorded on your application is {shown}."
    if field in capabilities.IDENTIFIER_FIELDS:
        return f"Your {label(field)} is {shown}."
    return f"The {label(field)} on your application is {shown}."


def _one(field: str, values: dict[str, Any], results: dict[str, Any] | None = None,
         context: dict[str, Any] | None = None) -> str:
    from app.agents.applicant import field_state

    context = context or {}
    if field == APPLICATION_REF:
        case = values.get("case_id")
        applicant = values.get("applicant_id")
        if not case:
            return "I don't have a case reference recorded for this application yet."
        said = f"This application is referenced by its case ID, {case}"
        said += f", and your applicant ID is {applicant}." if applicant else "."
        return said + " There is no separate application number."
    # THE STATE FIRST (field_state): present, not provided, not available,
    # restricted or unknown -- from the authoritative source, never from one
    # tool's silence; then the sentence, in the caller's language.
    resolved = field_state.resolve(field, results or {}, case_id=context.get("case_id"),
                                   party_id=context.get("party_id"))
    if resolved.state is field_state.FieldState.PRESENT:
        if field in field_state.IDENTITY_FIELDS:
            shown = _shown(field, resolved.value)
            if shown is None:
                return field_state.say(
                    field_state.Resolved(field, field_state.FieldState.RESTRICTED),
                    label=label(field), shown=None, language=context.get("language"))
            return field_state.say(resolved, label=label(field), shown=shown,
                                   language=context.get("language"))
        return _said(field, resolved.value, context.get("language"))
    return field_state.say(resolved, label=label(field), shown=None,
                           language=context.get("language"))


def _bundle(fields: tuple[str, ...], values: dict[str, Any], *, heading: str) -> str:
    recorded = []
    absent = []
    for f in fields:
        value = values.get(f)
        if value in (None, ""):
            absent.append(label(f))
            continue
        shown = _shown(f, value)
        recorded.append(f"{label(f)}: {shown}" if shown is not None
                        else f"{label(f)}: withheld for your security")
    if not recorded:
        return f"None of the {heading} are recorded on this application yet."
    said = f"{heading[0].upper()}{heading[1:]} on record -- " + "; ".join(recorded) + "."
    if absent:
        said += " Not recorded: " + ", ".join(absent) + "."
    return said


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def answer(question: Question, results: dict[str, Any], *, case_id: str | None = None,
           party_id: str | None = None, language: str | None = None) -> str:
    """The answer, from the records the tools returned."""
    values = _values(results)
    context = {"case_id": case_id, "party_id": party_id, "language": language}
    kind = question.field
    if kind == ALL:
        # WHICH details are recorded, not their values: "what have I
        # submitted" is answered as a list of fields (privacy contract).
        listed = capabilities.APPLICATION_FIELDS[2:] + capabilities.APPLICANT_FIELDS
        recorded = [label(f) for f in reversed(listed) if values.get(f) not in (None, "")]
        missing = [label(f) for f in values["_missing"]]
        if not recorded:
            return "I don't have any of your details recorded on this application yet."
        said = f"Your application has your {_and(recorded)} recorded."
        if missing:
            said += f" Still to add: {_and(missing)}."
        return said
    if kind == ALL_APPLICANT:
        return _bundle(capabilities.APPLICANT_FIELDS, values, heading="applicant details")
    if kind == ALL_APPLICATION:
        return _bundle(capabilities.APPLICATION_FIELDS, values, heading="application details")
    if kind == ALL_FINANCIAL:
        return _bundle(capabilities.FINANCIAL_FIELDS, values, heading="financial details")
    if kind == FIELDS_AVAILABLE:
        all_fields = capabilities.APPLICANT_FIELDS + capabilities.APPLICATION_FIELDS
        recorded = [label(f) for f in all_fields if values.get(f) not in (None, "")]
        absent = [label(f) for f in all_fields if values.get(f) in (None, "")]
        said = "Recorded on this application: " + (", ".join(recorded) if recorded else "nothing yet") + "."
        if absent:
            said += " Not recorded: " + ", ".join(absent) + "."
        return said
    return " ".join(_one(f, values, results, context) for f in question.fields)


__all__ = ["ALL", "ALL_APPLICANT", "ALL_APPLICATION", "ALL_FINANCIAL", "APPLICATION_REF",
           "COMPLETENESS", "FIELDS_AVAILABLE", "Question", "answer", "detect", "label"]
