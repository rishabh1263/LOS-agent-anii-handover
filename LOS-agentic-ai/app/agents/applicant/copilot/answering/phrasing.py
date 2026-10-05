"""
PHRASING -- the same fact, said the way a colleague would say it.

A deterministic answer is built from records as ONE fixed sentence. Read
twenty times in a conversation, one fixed sentence sounds like a form. This
layer varies the WORDING, never the fact:

  * VARIANTS: a fact family (a recorded field, a state) has several
    sentence shapes; one is chosen by a stable per-turn seed, so the same
    turn always reads the same and successive turns do not all start with
    "Your application records ...".
  * ACKNOWLEDGEMENTS: a correction, a resolved clarification or a repeat is
    acknowledged in a few words before the answer ("Got it --", "Sure --"),
    in the language the question was typed in.
  * LANGUAGE: a variant exists per supported language; a language without
    one keeps the English sentence (never a machine translation of a fact).

No model runs here. Nothing is added: every variant carries exactly the
same value(s) the fixed sentence carried, which the fidelity guard can check.
"""

from __future__ import annotations

import contextvars
import hashlib
from typing import Any

#: The seed of the turn being answered, set by the conversation layer so
#: every sentence built for this turn varies together and stably.
TURN_SEED: contextvars.ContextVar[int] = contextvars.ContextVar("copilot_turn_seed", default=0)
#: The language a WHOLE-SENTENCE answer was written in (a field or state
#: sentence in Hinglish, Hindi, Marathi), so the response's language contract
#: says what the prose really is. An acknowledgement prefix does not count.
PRESENTED: contextvars.ContextVar[str | None] = contextvars.ContextVar("copilot_presented", default=None)


def _as_reply(language: str | None) -> str | None:
    """The language a question in `language` is answered in: Roman Marathi
    ("mr-Latn") is answered with the Marathi wording (languages.yaml reply_as)."""
    if not language:
        return language
    from app.agents.applicant import language as _languages

    return _languages.reply_as(language)


def _note(variants: dict[str, tuple[str, ...]], language: str | None) -> None:
    language = _as_reply(language)
    if language and language != "en" and variants.get(language):
        PRESENTED.set(language)


def current_seed(field: str = "") -> int:
    base = TURN_SEED.get()
    if not base or not field:
        return base
    digest = hashlib.sha256(f"{base}:{field}".encode()).digest()
    return int.from_bytes(digest[:4], "big")

#: Acknowledgements by TURN TYPE and language. Short, then the answer.
_ACK: dict[str, dict[str, tuple[str, ...]]] = {
    "CORRECTION": {
        "en": ("Got it --", "Okay --", "Right --"),
        "hi-Latn": ("Theek hai --", "Samajh gaya --", "Ji --"),
        "hi": ("ठीक है --", "समझ गया --", "जी --"),
        "mr": ("ठीक आहे --", "समजले --", "बरं --"),
    },
    "CLARIFICATION_RESPONSE": {
        "en": ("Sure --", "Okay --", "Here you go --"),
        "hi-Latn": ("Ji --", "Theek hai --", "Ye rahi jaankari --"),
        "hi": ("जी --", "ठीक है --", "ये रही जानकारी --"),
        "mr": ("ठीक आहे --", "हे घ्या --", "बरं --"),
    },
    "REPLAY": {
        "en": ("Once more:", "Again:", "Sure, again:"),
        "hi-Latn": ("Phir se:", "Dobara:", "Ek baar aur:"),
        "hi": ("फिर से:", "दोबारा:", "एक बार और:"),
        "mr": ("पुन्हा:", "परत:", "पुन्हा एकदा:"),
    },
}

#: Sentence shapes per recorded field: {value} is the value as disclosed.
#: Every shape says exactly the value and nothing else.
_FIELD_VARIANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "loan_amount": {
        "en": ("Your application records show a loan amount of {value}.",
               "The loan amount on your application is {value}.",
               "You applied for {value}."),
        "hi-Latn": ("Aapke application mein loan amount {value} hai.",
                    "Aapne {value} ka loan apply kiya hai.",
                    "Loan amount {value} record hai."),
        "hi": ("आपके आवेदन में ऋण राशि {value} है।", "आपने {value} का ऋण आवेदन किया है।"),
        "mr": ("तुमच्या अर्जात कर्जाची रक्कम {value} आहे.", "तुम्ही {value} च्या कर्जासाठी अर्ज केला आहे."),
        "ne": ("तपाईंको आवेदनमा ऋण रकम {value} छ।",),
    },
    "mobile": {
        "en": ("The mobile number on your application is {value}.",
               "Your registered mobile number is {value}.",
               "We have {value} as your mobile number."),
        "hi-Latn": ("Aapka registered mobile number {value} hai.",
                    "Application par mobile number {value} hai."),
        "hi": ("आपका पंजीकृत मोबाइल नंबर {value} है।", "आवेदन पर मोबाइल नंबर {value} है।"),
        "mr": ("तुमचा नोंदणीकृत मोबाइल क्रमांक {value} आहे.", "अर्जावर मोबाइल क्रमांक {value} आहे."),
    },
    "email": {
        "en": ("The email on your application is {value}.",
               "Your registered email is {value}.",
               "We have {value} as your email address."),
        "hi-Latn": ("Aapka registered email {value} hai.", "Application par email {value} hai."),
        "hi": ("आपका पंजीकृत ईमेल {value} है।",),
        "mr": ("तुमचा नोंदणीकृत ईमेल {value} आहे.",),
    },
    "full_name": {
        "en": ("The name on your application is {value}.",
               "Your application is in the name of {value}.",
               "We have your name as {value}."),
        "hi-Latn": ("Aapke application par naam {value} hai.", "Aapka naam {value} record hai."),
        "hi": ("आपके आवेदन पर नाम {value} है।",),
        "mr": ("तुमच्या अर्जावर नाव {value} आहे.",),
    },
    "date_of_birth": {
        "en": ("The date of birth on your application is {value}.",
               "Your recorded date of birth is {value}.",
               "We have {value} as your date of birth."),
        "hi-Latn": ("Aapki janm tithi {value} record hai.", "Application par date of birth {value} hai."),
        "hi": ("आपकी जन्म तिथि {value} दर्ज है।",),
        "mr": ("तुमची जन्मतारीख {value} नोंदवली आहे.",),
    },
    "address": {
        "en": ("The address on your application is {value}.",
               "Your recorded address is {value}.",
               "We have your address as {value}."),
        "hi-Latn": ("Aapka registered address {value} hai.", "Application par pata {value} hai."),
        "hi": ("आपका पंजीकृत पता {value} है।",),
        "mr": ("तुमचा नोंदणीकृत पत्ता {value} आहे.",),
    },
    "product": {
        "en": ("You applied for a {value}.", "The product on your application is {value}.",
               "Your application is for a {value}."),
        "hi-Latn": ("Aapne {value} ke liye apply kiya hai.", "Aapka product {value} hai."),
        "hi": ("आपने {value} के लिए आवेदन किया है।",),
        "mr": ("तुम्ही {value} साठी अर्ज केला आहे.",),
    },
    "employment_type": {
        "en": ("Your employment type is recorded as {value}.",
               "The application records you as {value}.",
               "We have your employment type as {value}."),
        "hi-Latn": ("Aapka employment type {value} record hai.", "Aap {value} ke roop mein record hain."),
        "hi": ("आपका रोज़गार प्रकार {value} दर्ज है।",),
        "mr": ("तुमचा रोजगार प्रकार {value} नोंदवला आहे.",),
    },
    "tenure_months": {
        "en": ("Your application has a tenure of {value}.", "The recorded tenure is {value}.",
               "The loan tenure on your application is {value}."),
        "hi-Latn": ("Aapke loan ki tenure {value} hai.", "Tenure {value} record hai."),
        "hi": ("आपके ऋण की अवधि {value} है।",),
        "mr": ("तुमच्या कर्जाचा कालावधी {value} आहे.",),
    },
    "interest_rate_pct": {
        "en": ("The interest rate on your application is {value}.",
               "The application records an interest rate of {value}.",
               "Your recorded interest rate is {value}."),
        "hi-Latn": ("Aapke application par interest rate {value} hai.", "Byaj dar {value} record hai."),
        "hi": ("आपके आवेदन पर ब्याज दर {value} है।",),
        "mr": ("तुमच्या अर्जावर व्याजदर {value} आहे.",),
    },
    "declared_monthly_income": {
        "en": ("Your declared monthly income is {value}.",
               "The application records a declared monthly income of {value}.",
               "You declared a monthly income of {value}."),
        "hi-Latn": ("Aapki declared monthly income {value} hai.", "Aapne monthly income {value} declare ki hai."),
        "hi": ("आपकी घोषित मासिक आय {value} है।",),
        "mr": ("तुमचे घोषित मासिक उत्पन्न {value} आहे.",),
    },
    "declared_monthly_obligations": {
        "en": ("Your declared monthly obligations are {value}.",
               "The application records monthly obligations of {value}.",
               "You declared monthly obligations of {value}."),
        "hi-Latn": ("Aapki declared monthly obligations {value} hain.",),
        "hi": ("आपकी घोषित मासिक देनदारियाँ {value} हैं।",),
        "mr": ("तुमच्या घोषित मासिक देणी {value} आहेत.",),
    },
    "property_value": {
        "en": ("The property value recorded on your application is {value}.",
               "Your application records a property value of {value}.",
               "The recorded property value is {value}."),
        "hi-Latn": ("Aapke application par property value {value} record hai.",),
        "hi": ("आपके आवेदन पर संपत्ति मूल्य {value} दर्ज है।",),
        "mr": ("तुमच्या अर्जावर मालमत्तेचे मूल्य {value} नोंदवले आहे.",),
    },
    "case_id": {
        "en": ("Your case ID is {value}.", "This case's ID is {value}.",
               "The case reference for your application is {value}."),
        "hi-Latn": ("Aapka case ID {value} hai.", "Is case ka ID {value} hai."),
        "hi": ("आपका केस आईडी {value} है।",),
        "mr": ("तुमचा केस आयडी {value} आहे.",),
    },
    "applicant_id": {
        "en": ("Your applicant ID is {value}.", "The applicant ID on this case is {value}.",
               "You are recorded under applicant ID {value}."),
        "hi-Latn": ("Aapka applicant ID {value} hai.",),
        "hi": ("आपका आवेदक आईडी {value} है।",),
        "mr": ("तुमचा अर्जदार आयडी {value} आहे.",),
    },
    "pan_number": {
        "en": ("Your PAN number is {value}.", "The PAN on your documents is {value}.",
               "We have your PAN as {value}."),
        "hi-Latn": ("Aapka PAN number {value} hai.", "Documents par PAN {value} hai."),
        "hi": ("आपका पैन नंबर {value} है।",),
        "mr": ("तुमचा पॅन क्रमांक {value} आहे.",),
    },
    "aadhaar_number": {
        "en": ("Your Aadhaar number is {value}.", "The Aadhaar on your documents is {value}.",
               "We have your Aadhaar as {value}."),
        "hi-Latn": ("Aapka Aadhaar number {value} hai.",),
        "hi": ("आपका आधार नंबर {value} है।",),
        "mr": ("तुमचा आधार क्रमांक {value} आहे.",),
    },
    "bank_account_number": {
        "en": ("Your bank account number is {value}.", "The account number on your bank statement is {value}.",
               "We have your account number as {value}."),
        "hi-Latn": ("Aapka bank account number {value} hai.",),
        "hi": ("आपका बैंक खाता नंबर {value} है।",),
        "mr": ("तुमचा बँक खाते क्रमांक {value} आहे.",),
    },
}

#: Sentence shapes per FIELD STATE: {field} is the field's label.
_STATE_VARIANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "NOT_PROVIDED": {
        "en": ("You haven't provided your {field} yet.", "Your {field} hasn't been given to us yet.",
               "We don't have your {field} -- it hasn't been provided yet."),
        "hi-Latn": ("Aapne apna {field} abhi tak provide nahi kiya hai.",
                    "Aapka {field} abhi tak nahi diya gaya hai."),
        "hi": ("आपने अपना {field} अभी तक नहीं दिया है।",),
        "mr": ("तुम्ही तुमचा {field} अजून दिलेला नाही.",),
    },
    "NOT_AVAILABLE": {
        "en": ("I don't have your {field} recorded on this application yet.",
               "Your {field} isn't available on the application yet.",
               "That information -- your {field} -- isn't recorded yet."),
        "hi-Latn": ("Aapka {field} abhi available nahi hai.", "Aapka {field} abhi record nahi hua hai."),
        "hi": ("आपका {field} अभी उपलब्ध नहीं है।",),
        "mr": ("तुमचा {field} अजून उपलब्ध नाही.",),
    },
    "RESTRICTED": {
        "en": ("Your {field} is on record, but for your security I can't share the full value here.",
               "I can see your {field} is recorded, but I can't show the full value for security reasons."),
        "hi-Latn": ("Aapka {field} record mein hai, lekin security ke liye main poora number share nahi kar sakta.",),
        "hi": ("आपका {field} रिकॉर्ड में है, लेकिन सुरक्षा के लिए मैं पूरा नंबर साझा नहीं कर सकता।",),
        "mr": ("तुमचा {field} नोंदीत आहे, पण सुरक्षेसाठी मी पूर्ण क्रमांक सांगू शकत नाही.",),
    },
    "UNKNOWN": {
        "en": ("I couldn't verify your {field} from the available records right now.",
               "I can't check your {field} at the moment -- please try again shortly."),
        "hi-Latn": ("Main abhi aapka {field} check nahi kar pa raha hoon. Thodi der baad phir try karein.",),
        "hi": ("मैं अभी आपका {field} जाँच नहीं पा रहा हूँ। कृपया थोड़ी देर बाद फिर कोशिश करें।",),
        "mr": ("मी सध्या तुमचा {field} तपासू शकत नाही. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा.",),
    },
}


def seed_for(conversation_id: str | None, turn_id: int | None, field: str = "") -> int:
    """A stable per-turn seed: the same turn always reads the same."""
    # THE FIRST ANSWER OF A CONVERSATION KEEPS THE CANONICAL SHAPE (variant
    # 0, the sentence the records have always been said in); wording varies
    # from the second turn on, when repetition would start to show.
    if not turn_id or int(turn_id) <= 1:
        return 0
    digest = hashlib.sha256(f"{conversation_id or ''}:{turn_id or 0}:{field}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def _pick(variants: dict[str, tuple[str, ...]], language: str | None, seed: int) -> str | None:
    language = _as_reply(language)
    shapes = variants.get(language or "en") or variants.get("en") or ()
    if not shapes:
        return None
    return shapes[seed % len(shapes)]


#: ONE FIELD OF ANOTHER PARTY ON THE CASE (the co-applicant), in each state.
#: {who} is the party ("the co-applicant"), {Who} the same capitalised.
_PARTY_VARIANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "PRESENT": {
        "en": ("{Who}'s {field} is {value}.", "{Who}'s {field} on record is {value}."),
        "hi-Latn": ("Co-applicant ka {field} {value} hai.",),
        "hi": ("सह-आवेदक का {field} {value} है।",),
    },
    "NOT_PROVIDED": {
        "en": ("{Who}'s {field} hasn't been provided yet.",
               "{Who} hasn't provided their {field} yet."),
        "hi-Latn": ("Co-applicant ne apna {field} abhi tak nahi diya hai.",),
        "hi": ("सह-आवेदक ने अपना {field} अभी तक नहीं दिया है।",),
    },
    "NOT_AVAILABLE": {
        "en": ("{Who}'s {field} isn't recorded on this application yet.",),
        "hi-Latn": ("Co-applicant ka {field} abhi record nahi hua hai.",),
        "hi": ("सह-आवेदक का {field} अभी दर्ज नहीं है।",),
    },
    "RESTRICTED": {
        "en": ("{Who}'s {field} is on record, but for security I can't share the full value here.",),
        "hi-Latn": ("Co-applicant ka {field} record mein hai, lekin security ke liye main poora "
                    "number share nahi kar sakta.",),
    },
    "UNKNOWN": {
        "en": ("I couldn't verify {who}'s {field} from the available records right now.",),
        "hi-Latn": ("Main abhi co-applicant ka {field} check nahi kar pa raha hoon.",),
    },
}


def party_sentence(state: str, who: str, field: str, value: str | None = None, *,
                   language: str | None = None, seed: int = 0) -> str:
    """One field of a named party, in its state and the caller's language."""
    table = _PARTY_VARIANTS.get(state) or _PARTY_VARIANTS["UNKNOWN"]
    _note(table, language)
    shapes = table.get(_as_reply(language) or "en") or table["en"]
    shape = shapes[seed % len(shapes)]
    return shape.format(who=who, Who=who[:1].upper() + who[1:], field=field,
                        value="" if value is None else value)


def field_sentence(field: str, value: str, *, language: str | None, seed: int) -> str | None:
    """A variant sentence for a PRESENT field, or None when the family has none."""
    variants = _FIELD_VARIANTS.get(field, {})
    shape = _pick(variants, language, seed)
    if shape:
        _note(variants, language)
    return shape.format(value=value) if shape else None


def state_sentence(state: str, label: str, *, language: str | None, seed: int) -> str | None:
    variants = _STATE_VARIANTS.get(str(state).upper(), {})
    shape = _pick(variants, language, seed)
    if shape:
        _note(variants, language)
    return shape.format(field=label) if shape else None


def acknowledge(answer: str, *, turn_type: str | None, language: str | None, seed: int) -> str:
    """The answer with a short acknowledgement when the turn calls for one."""
    family = _ACK.get(str(turn_type or "").upper())
    if not family or not answer or not answer.strip():
        return answer
    prefix = _pick(family, language, seed)
    if not prefix or answer.lstrip().startswith(prefix):
        return answer
    body = answer.strip()
    first = body.split(" ", 1)[0]
    if prefix.rstrip().endswith(("--", ",", "-", "—")) and first in ("Your", "The", "You", "This",
                                                                      "There", "It", "No"):
        body = body[0].lower() + body[1:]       # a sentence continued, not restarted
    return f"{prefix} {body}"


def styles() -> dict[str, Any]:
    """What this layer can vary, for the trace."""
    return {"fields": sorted(_FIELD_VARIANTS), "states": sorted(_STATE_VARIANTS),
            "acknowledgements": sorted(_ACK)}


__all__ = ["acknowledge", "field_sentence", "seed_for", "state_sentence", "styles"]
