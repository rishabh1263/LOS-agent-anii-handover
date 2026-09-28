"""
The semantic eval cases: one MEANING, many phrasings.

Each case states the expected frame (task / object), the stage referent
when the phrasing carries one, the intent the frame must route to and the
language. UNSEEN holds phrasings that appear nowhere in configuration or
code -- they are the demonstration that understanding generalises.
"""

from __future__ import annotations

LR, LP, LS, CV = "LIST_REQUIREMENTS", "LIST_PENDING", "LIST_SUBMITTED", "CHECK_VERIFICATION"
D = "DOCUMENTS"


def _c(cid, q, task, obj, intent, *, stage_ref=None, language="en", max_ms=3000):
    return {"id": cid, "q": q, "task": task, "object": obj, "intent": intent,
            "stage_ref": stage_ref, "language": language, "max_ms": max_ms}


CASES = [
    # English
    _c("en_mandatory", "What documents are mandatory?", LR, D, "DOCUMENTS_REQUIRED"),
    _c("en_mandatory_stage", "What documents are mandatory for this stage?", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT"),
    _c("en_need", "Which documents do I need?", LR, D, "DOCUMENTS_REQUIRED"),
    _c("en_paperwork", "Which paperwork is required?", LR, D, "DOCUMENTS_REQUIRED"),
    _c("en_paperwork_stage", "What paperwork do I need at this stage?", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT"),
    _c("en_submit", "What do I need to submit?", LR, D, "DOCUMENTS_MISSING"),
    _c("en_compulsory", "What documents are compulsory?", LR, D, "DOCUMENTS_REQUIRED"),
    _c("en_malformed", "What is the mandatory document required for", LR, D,
       "DOCUMENTS_REQUIRED"),
    _c("en_need_stage", "Which documents do I need at this stage?", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT"),
    _c("en_named_stage", "Which documents do I need for CPA?", LR, D, "DOCUMENTS_REQUIRED",
       stage_ref="CPA"),
    _c("en_pending", "What documents are still pending?", LP, D, "DOCUMENTS_PENDING"),
    _c("en_left", "which docs are left?", LP, D, "DOCUMENTS_MISSING"),
    _c("en_uploaded", "Which documents have been uploaded?", LS, D, "DOCUMENTS_UPLOADED"),
    _c("en_verified", "are my documents verified?", CV, D, "DOCUMENT_VERIFICATION"),
    _c("en_stage", "What stage am I in?", "CURRENT_STAGE", "STAGE", "APPLICATION_STAGE"),
    _c("en_status", "what is my application status?", "STATUS", "APPLICATION",
       "APPLICATION_STATUS"),
    _c("en_next", "what should i do next?", "NEXT_ACTION", "NONE", "NEXT_ACTION"),
    _c("en_pending_items", "what is pending?", LP, "NONE", "PENDING_ITEMS"),
    _c("en_kyc", "What is KYC?", "EXPLAIN", "PROCESS_TERM", "FOS_KNOWLEDGE"),
    _c("en_define_mandatory", "What does mandatory document mean?", "EXPLAIN", D,
       "FOS_KNOWLEDGE"),
    # Hinglish
    _c("hi_latn_chahiye", "Kaunse documents chahiye?", LR, D, "DOCUMENTS_REQUIRED",
       language="hi-Latn"),
    _c("hi_latn_stage_papers", "Is stage pe kaunse papers chahiye?", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT", language="hi-Latn"),
    _c("hi_latn_submit_now", "Abhi kya kya submit karna hai?", LR, D, "DOCUMENTS_MISSING",
       language="hi-Latn"),
    _c("hi_latn_mandatory", "Mandatory docs kya hain?", LR, D, "DOCUMENTS_REQUIRED",
       language="hi-Latn"),
    _c("hi_latn_stage_liye", "Is stage ke liye kya chahiye?", LR, D, "DOCUMENTS_REQUIRED",
       stage_ref="CURRENT", language="hi-Latn"),
    _c("hi_latn_stage_submit", "Is stage pe kya submit karna hai?", LR, D,
       "DOCUMENTS_MISSING", stage_ref="CURRENT", language="hi-Latn"),
    _c("hi_latn_baaki", "kaunse documents baaki hain?", LP, D, "DOCUMENTS_PENDING",
       language="hi-Latn"),
    # Hindi
    _c("hi_stage_docs", "इस चरण में कौन से दस्तावेज़ चाहिए?", LR, D, "DOCUMENTS_REQUIRED",
       stage_ref="CURRENT", language="hi"),
    _c("hi_mandatory", "कौन से दस्तावेज़ अनिवार्य हैं?", LR, D, "DOCUMENTS_REQUIRED",
       language="hi"),
    # Marathi
    _c("mr_stage_docs", "या स्टेजसाठी कोणती कागदपत्रे लागतील?", LR, D, "DOCUMENTS_REQUIRED",
       language="mr"),
    _c("mr_mandatory", "कोणते documents mandatory आहेत?", LR, D, "DOCUMENTS_REQUIRED",
       language="mr"),
    # typos
    _c("typo_mandtory", "mandtory docs?", LR, D, "DOCUMENTS_REQUIRED"),
    _c("typo_documnts", "documnts required?", LR, D, "DOCUMENTS_REQUIRED"),
    _c("typo_documnets", "which documnets i need?", LR, D, "DOCUMENTS_REQUIRED"),
]

#: Phrasings that appear NOWHERE in configuration, code or the cases above.
UNSEEN = [
    _c("unseen_current_step", "Tell me what papers I need for the current step.", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT"),
    _c("unseen_at_this_point", "At this point in my application, what documents do I still need?",
       LP, D, "DOCUMENTS_MISSING", stage_ref="CURRENT"),
    _c("unseen_upload_now", "What do I need to upload now?", LR, D, "DOCUMENTS_MISSING",
       stage_ref="CURRENT"),
    _c("unseen_compulsory_stage", "Which paperwork is compulsory at this stage?", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT"),
    _c("unseen_lagenge", "Is stage pe kya documents lagenge?", LR, D, "DOCUMENTS_REQUIRED",
       stage_ref="CURRENT", language="hi-Latn"),
    _c("unseen_ab_next", "Ab next kya submit karna hai?", "NEXT_ACTION", "NONE", "NEXT_ACTION",
       language="hi-Latn"),
    _c("unseen_essential", "which papers are essential for my file at the moment", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT"),
    _c("unseen_hand_in", "what proofs must I hand in for this step", LR, D,
       "DOCUMENTS_MISSING", stage_ref="CURRENT"),
    _c("unseen_outstanding", "anything outstanding on my paperwork?", LP, D,
       "DOCUMENTS_PENDING"),
    _c("unseen_checked", "have my papers been checked yet", CV, D, "DOCUMENT_VERIFICATION"),
    _c("unseen_hi_kagaz", "kaunse kagaz zaroori hain is step ke liye", LR, D,
       "DOCUMENTS_REQUIRED", stage_ref="CURRENT", language="hi-Latn"),
    _c("unseen_mr_pending", "कोणती कागदपत्रे बाकी आहेत?", LP, D, "DOCUMENTS_PENDING",
       language="mr"),
]

FOLLOW_UP = {"first": "What stage am I in?",
             "second": "What documents are mandatory for this stage?"}

#: Bare words: clarified, with no tool, retrieval or model call.
AMBIGUOUS = [{"id": f"ambiguous_{w.lower()}", "q": w, "head": h} for w, h in
             (("name", "name"), ("status", "status"), ("documents", "documents"),
              ("income", "income"), ("PAN", "pan"), ("address", "address"), ("docs", "documents"))]

#: Bare words settled by the previous turn: (first question, short follow-up, expected intent)
SHORT_FOLLOW_UPS = [
    ("What stage am I in?", "documents", "DOCUMENTS_REQUIRED"),
    ("Which documents are pending?", "status", "DOCUMENT_VERIFICATION"),
    ("What is my application status?", "status", "APPLICATION_STATUS"),
]


def _chat(cid, q, *intents, language="en", clarify=None, max_ms=3000):
    """A free-form question graded on WHAT IT MEANS: any of `intents` is a
    correct reading; `clarify` names the clarification reason a targeted
    question back must carry instead of an answer."""
    return {"id": cid, "q": q, "intents": set(intents), "language": language,
            "clarify": clarify, "max_ms": max_ms}


#: CHATGPT-LIKE PHRASINGS: colloquial, indirect, Hinglish, Hindi, Marathi,
#: typos, context references. None appears in any pattern list. Expected
#: intents are set by MEANING before the run; a phrasing the rules cannot
#: read is a recorded gap (see the report), not a sentence to be added.
CHAT = [
    _chat("chat_where_things_stand", "can you tell me where things stand with my file",
          "APPLICATION_STATUS"),
    _chat("chat_looked_at", "has anything on my file been looked at yet",
          "DOCUMENT_VERIFICATION"),
    _chat("chat_green_light", "which of my papers got the green light", "DOCUMENT_VERIFICATION"),
    _chat("chat_bank_wants", "remind me what the bank still wants from me",
          "DOCUMENTS_MISSING", "DOCUMENTS_PENDING"),
    _chat("chat_everything_in", "is everything in for this step or not",
          "PENDING_ITEMS", "DOCUMENTS_PENDING"),
    _chat("chat_go_through", "did my salary slip go through", "DOCUMENT_VERIFICATION"),
    _chat("chat_done_paperwork", "am I done with the paperwork",
          "DOCUMENTS_PENDING", "PENDING_ITEMS"),
    _chat("chat_moving_forward", "is my application moving forward", "APPLICATION_STATUS"),
    _chat("chat_need_me_to_send", "anything you need me to send",
          "NEXT_ACTION", "DOCUMENTS_MISSING"),
    _chat("chat_holdup", "what's the holdup", "APPLICATION_STATUS", "PENDING_ITEMS"),
    _chat("chat_got_everything", "have you got everything you need",
          "PENDING_ITEMS", "DOCUMENTS_PENDING"),
    _chat("chat_anything_wrong", "is there anything wrong with what I sent",
          "DOCUMENT_VERIFICATION"),
    _chat("chat_supposed_to_upload", "What am I supposed to upload now?",
          "DOCUMENTS_MISSING"),
    _chat("chat_why_pending", "why is this still pending?", "CASE_HISTORY", "PENDING_ITEMS"),
    _chat("chat_other_applicant", "what about the other applicant?", clarify="TASK_UNCLEAR"),
    _chat("chat_how_far", "how far along is my loan", "APPLICATION_STATUS", "APPLICATION_STAGE"),
    _chat("chat_stage_next", "which stage comes next", "NEXT_ACTION", "STAGE_PROCESS"),
    _chat("chat_move_ahead", "is my file ready to move ahead", "READINESS"),
    _chat("chat_do_next", "what should I do next", "NEXT_ACTION"),
    _chat("chat_loan_amount", "whats my loan amount", "APPLICANT_PROFILE"),
    _chat("chat_apply_for", "how much did I apply for", "APPLICANT_PROFILE"),
    _chat("chat_received", "which papers have you already received from me",
          "DOCUMENTS_UPLOADED"),
    _chat("chat_whats_kyc", "what's KYC", "FOS_KNOWLEDGE"),
    _chat("chat_how_verification", "how does the verification step work",
          "FOS_KNOWLEDGE", "STAGE_PROCESS"),
    _chat("chat_pan_verified", "can u chk if my pan is verified", "DOCUMENT_VERIFICATION"),
    _chat("chat_uploaded_what_now", "i uploaded everything, what now", "NEXT_ACTION"),
    _chat("chat_missing_my_side", "anything missing from my side?",
          "DOCUMENTS_MISSING", "DOCUMENTS_PENDING"),
    _chat("chat_next_step_for_me", "what is the next step for me", "NEXT_ACTION"),
    _chat("chat_good_to_go", "are we good to go", "READINESS"),
    _chat("chat_typo_pending", "wich documnts r pending", "DOCUMENTS_PENDING"),
    _chat("chat_typo_status", "statsu of my aplication", "APPLICATION_STATUS"),
    # Hinglish
    _chat("chat_hi_dena_hai", "Abhi mereko kya kya dena hai?", "DOCUMENTS_MISSING",
          language="hi-Latn"),
    _chat("chat_hi_reject_hua", "koi document reject hua kya", "DOCUMENT_VERIFICATION",
          language="hi-Latn"),
    _chat("chat_hi_theek", "sab documents theek hain?", "DOCUMENT_VERIFICATION",
          language="hi-Latn"),
    _chat("chat_hi_verify_ho_gaya", "kya sab kuch verify ho gaya", "DOCUMENT_VERIFICATION",
          language="hi-Latn"),
    _chat("chat_hi_nahi_aaya", "kaunsa document abhi tak nahi aaya",
          "DOCUMENTS_MISSING", "DOCUMENTS_PENDING", language="hi-Latn"),
    _chat("chat_hi_kaha_tak", "mera case kaha tak pahuncha",
          "APPLICATION_STATUS", "APPLICATION_STAGE", language="hi-Latn"),
    _chat("chat_hi_aage_kya", "ab aage kya karna hai", "NEXT_ACTION", language="hi-Latn"),
    _chat("chat_hi_kis_stage", "mera file kis stage pe hai", "APPLICATION_STAGE",
          language="hi-Latn"),
    _chat("chat_hi_dene_honge", "kya kya documents dene honge", "DOCUMENTS_MISSING",
          "DOCUMENTS_REQUIRED", language="hi-Latn"),
    # Hindi / Marathi script
    _chat("chat_hi_baki", "मेरे कौन से दस्तावेज़ बाकी हैं?", "DOCUMENTS_PENDING", language="hi"),
    _chat("chat_hi_kahan_tak", "मेरा आवेदन कहाँ तक पहुँचा?", "APPLICATION_STATUS",
          "APPLICATION_STAGE", language="hi"),
    _chat("chat_mr_file_kuthe", "माझी फाईल कुठे आहे?", "APPLICATION_STATUS",
          "APPLICATION_STAGE", language="mr"),
    _chat("chat_mr_tapasle", "माझे कागदपत्र तपासले का?", "DOCUMENT_VERIFICATION", language="mr"),
]

#: Small talk: answered from nothing -- no record, tool, retrieval or model.
GREETINGS = ["hello", "hi there", "thanks a lot", "thank you so much", "ok bye", "namaste"]

#: Two case questions in one sentence: both halves answered, both records read.
COMPOUND = {"q": "Are my documents verified and what is my loan amount?",
            "intents": ["DOCUMENT_VERIFICATION", "APPLICANT_PROFILE"],
            "reads": {"list_documents", "get_application"}}

#: A context reference after a real question: the party carries over.
CONTEXT_REFERENCES = [
    ("Which documents are pending?", "and for the co-applicant?",
     {"PENDING_ITEMS", "DOCUMENTS_PENDING", "DOCUMENTS_MISSING"}),
    ("What stage am I in?", "what is pending here", {"PENDING_ITEMS", "DOCUMENTS_PENDING"}),
]

__all__ = ["AMBIGUOUS", "CASES", "CHAT", "COMPOUND", "CONTEXT_REFERENCES", "FOLLOW_UP",
           "GREETINGS", "SHORT_FOLLOW_UPS", "UNSEEN"]
