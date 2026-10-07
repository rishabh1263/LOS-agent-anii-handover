# New APIs -- request / response (Phase 3)

Generated from REAL responses (tests/integration/test_frontend_contract.py, every Phase 3 flag on, fake
model). Each block shows the request and the response fields the UI uses; the full response of each is in
`docs/frontend/examples/<name>.json`. Contract and rendering rules: `FRONTEND_API.md`.

Every call: header `Authorization: Bearer <JWT>`. Always send back the `context` object of the previous
response. Request ids, timestamps and signed tokens below are placeholders.

## List my cases (typed: "mere cases dikhao" -- or action LIST_CASES)

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "CUSTOM_QUERY",
  "message": "mere cases dikhao"
}
```

Response:
```json
{
  "intent": "CASE_LIST",
  "answer": "📂 **Your cases** -- 2 total · ⏳ 2 pending\n\n1. **CASE-F7AD07E53966** (Priya V.) · 📍 **FOS (Field Officer Sales)** · ⏳ 3 docs pending\n2. **CASE-AB355D618BF7** (Rahul S.) · 📍 FOS · ⏳ 3 docs pending\n\n👉 Kis case mein jaana hai?",
  "context": {
    "workspace_id": "default"
  },
  "response_source": "STRUCTURED",
  "presentation": {
    "case_list": [
      {
        "number": 1,
        "case_id": "CASE-F7AD07E53966",
        "applicant_id": "APP-592D56C9BB98",
        "applicant_name": "Priya V.",
        "stage": "FOS",
        "status_label": "⏳ 3 docs pending",
        "status_kind": "DOCS",
        "emoji": "⏳",
        "action": {
          "type": "open_case",
          "case_id": "CASE-F7AD07E53966"
        }
      },
      {
        "number": 2,
        "case_id": "CASE-AB355D618BF7",
        "applicant_id": "APP-B597637EF37D",
        "applicant_name": "Rahul S.",
        "stage": "FOS",
        "status_label": "⏳ 3 docs pending",
        "status_kind": "DOCS",
        "emoji": "⏳",
        "action": {
          "type": "open_case",
          "case_id": "CASE-AB355D618BF7"
        }
      }
    ],
    "counts": {
      "total": 2,
      "pending": 2
    },
    "page": 0,
    "has_more": false
  }
}
```

## Open a case (a click on a case card)

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "OPEN_CASE",
  "case_id": "CASE-AB355D618BF7"
}
```

Response:
```json
{
  "intent": "CASE_OPENED",
  "case_id": "CASE-AB355D618BF7",
  "answer": "🔓 **CASE-AB355D618BF7 (Rahul Sharma)** opened.\n📍 Stage: **FOS**\n⚠️ **KYC (Know Your Customer)** name (the applicant's): KYC has not run for this party.\n\n👉 Try: \"Kya baaki hai?\"",
  "context": {
    "workspace_id": "default"
  },
  "response_source": "STRUCTURED",
  "presentation": {
    "workspace": {
      "case_id": "CASE-AB355D618BF7",
      "header": "📍 CASE-AB355D618BF7",
      "buttons": [
        {
          "type": "exit_case",
          "label": "🔒 Exit"
        },
        {
          "type": "switch_case",
          "label": "🔁 Switch case"
        },
        {
          "type": "ask",
          "label": "Kya baaki hai?",
          "message": "Kya baaki hai?"
        },
        {
          "type": "ask",
          "label": "Kyu atka hai?",
          "message": "Kyu atka hai?"
        },
        {
          "type": "ask",
          "label": "Co-applicant",
          "message": "Co-applicant"
        },
        {
          "type": "ask",
          "label": "Summary",
          "message": "Summary"
        }
      ]
    }
  }
}
```

## A question inside the open case (no case_id needed)

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "CUSTOM_QUERY",
  "message": "kya baaki hai?"
}
```

Response:
```json
{
  "intent": "DOCUMENTS_PENDING",
  "case_id": "CASE-AB355D618BF7",
  "answer": "📍 CASE-AB355D618BF7\n⏳ Kripya sahi documents upload karein:\n📄 **PAN (Permanent Account Number)** [Upload] — Too little text could be read from this document. Re-upload a clearer copy.\nAbhi baaki:\n📄 Address Proof [Upload]\n📄 Bank Statement [Upload]\n\n👉 Upload these to move the case to **CPA**.",
  "document_actions": {
    "reupload": [
      {
        "party": "PRIMARY_APPLICANT",
        "party_label": "Applicant",
        "document_type": "PAN",
        "label": "PAN",
        "document_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
        "reasons": [
          "Too little text could be read from this document. Re-upload a clearer copy."
        ],
        "source": "VERIFICATION",
        "action": {
          "type": "UPLOAD_DOCUMENT",
          "document_type": "PAN",
          "party": "PRIMARY_APPLICANT",
          "label": "Upload PAN"
        }
      }
    ],
    "pending": [
      {
        "party": "PRIMARY_APPLICANT",
        "party_label": "Applicant",
        "document_type": "ADDRESS_PROOF",
        "label": "Address Proof",
        "action": {
          "type": "UPLOAD_DOCUMENT",
          "document_type": "ADDRESS_PROOF",
          "party": "PRIMARY_APPLICANT",
          "label": "Upload Address Proof"
        }
      },
      {
        "party": "PRIMARY_APPLICANT",
        "party_label": "Applicant",
        "document_type": "BANK_STATEMENT",
        "label": "Bank Statement",
        "action": {
          "type": "UPLOAD_DOCUMENT",
          "document_type": "BANK_STATEMENT",
          "party": "PRIMARY_APPLICANT",
          "label": "Upload Bank Statement"
        }
      }
    ],
    "under_review": [],
    "kyc_issues": []
  },
  "context": {
    "conversation_id": "be0615132ee542a9812bc70d79c5b8d4",
    "case_id": "CASE-AB355D618BF7",
    "last_query_type": "CASE_FACT",
    "last_intent": "DOCUMENTS_PENDING",
    "last_slot": "PAN",
    "last_documents": [
      "PAN",
      "ADDRESS_PROOF",
      "BANK_STATEMENT"
    ],
    "last_document": null,
    "last_field": null,
    "last_aspect": "PENDING",
    "last_subject": null,
    "last_task": "LIST_PENDING",
    "last_object": "DOCUMENTS",
    "last_stage": "FOS",
    "last_source": "STRUCTURED",
    "last_language": "hi-Latn"
  },
  "response_source": "STRUCTURED",
  "presentation": {
    "workspace": {
      "case_id": "CASE-AB355D618BF7",
      "header": "📍 CASE-AB355D618BF7",
      "buttons": [
        {
          "type": "exit_case",
          "label": "🔒 Exit"
        },
        {
          "type": "switch_case",
          "label": "🔁 Switch case"
        },
        {
          "type": "ask",
          "label": "Kya baaki hai?",
          "message": "Kya baaki hai?"
        },
        {
          "type": "ask",
          "label": "Kyu atka hai?",
          "message": "Kyu atka hai?"
        },
        {
          "type": "ask",
          "label": "Co-applicant",
          "message": "Co-applicant"
        },
        {
          "type": "ask",
          "label": "Summary",
          "message": "Summary"
        }
      ]
    }
  }
}
```

## "verify karna hai" -> the diagnosis

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "CUSTOM_QUERY",
  "message": "verify karna hai"
}
```

Response:
```json
{
  "intent": "PENDING_ITEMS",
  "case_id": "CASE-AB355D618BF7",
  "answer": "📍 CASE-AB355D618BF7\n⏳ Please upload the correct documents:\n📄 **PAN (Permanent Account Number)** [Upload] — Too little text could be read from this document. Re-upload a clearer copy.\nStill pending:\n📄 Address Proof [Upload]\n📄 Bank Statement [Upload]\n\n👉 Clear these to move the case to **CPA**.",
  "document_actions": {
    "reupload": [
      {
        "party": "PRIMARY_APPLICANT",
        "party_label": "Applicant",
        "document_type": "PAN",
        "label": "PAN",
        "document_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
        "reasons": [
          "Too little text could be read from this document. Re-upload a clearer copy."
        ],
        "source": "VERIFICATION",
        "action": {
          "type": "UPLOAD_DOCUMENT",
          "document_type": "PAN",
          "party": "PRIMARY_APPLICANT",
          "label": "Upload PAN"
        }
      }
    ],
    "pending": [
      {
        "party": "PRIMARY_APPLICANT",
        "party_label": "Applicant",
        "document_type": "ADDRESS_PROOF",
        "label": "Address Proof",
        "action": {
          "type": "UPLOAD_DOCUMENT",
          "document_type": "ADDRESS_PROOF",
          "party": "PRIMARY_APPLICANT",
          "label": "Upload Address Proof"
        }
      },
      {
        "party": "PRIMARY_APPLICANT",
        "party_label": "Applicant",
        "document_type": "BANK_STATEMENT",
        "label": "Bank Statement",
        "action": {
          "type": "UPLOAD_DOCUMENT",
          "document_type": "BANK_STATEMENT",
          "party": "PRIMARY_APPLICANT",
          "label": "Upload Bank Statement"
        }
      }
    ],
    "under_review": [],
    "kyc_issues": []
  },
  "context": {
    "conversation_id": null,
    "case_id": "CASE-AB355D618BF7",
    "last_query_type": "CASE_FACT",
    "last_intent": "PENDING_ITEMS",
    "last_slot": "PAN",
    "last_documents": [
      "PAN",
      "ADDRESS_PROOF",
      "BANK_STATEMENT"
    ],
    "last_document": null,
    "last_field": null,
    "last_aspect": null,
    "last_subject": null,
    "last_task": null,
    "last_object": null,
    "last_stage": null,
    "last_source": "STRUCTURED",
    "last_language": null
  },
  "response_source": "STRUCTURED",
  "presentation": {
    "workspace": {
      "case_id": "CASE-AB355D618BF7",
      "header": "📍 CASE-AB355D618BF7",
      "buttons": [
        {
          "type": "exit_case",
          "label": "🔒 Exit"
        },
        {
          "type": "switch_case",
          "label": "🔁 Switch case"
        },
        {
          "type": "ask",
          "label": "Kya baaki hai?",
          "message": "Kya baaki hai?"
        },
        {
          "type": "ask",
          "label": "Kyu atka hai?",
          "message": "Kyu atka hai?"
        },
        {
          "type": "ask",
          "label": "Co-applicant",
          "message": "Co-applicant"
        },
        {
          "type": "ask",
          "label": "Summary",
          "message": "Summary"
        }
      ]
    }
  }
}
```

## Raise a query -> a DRAFT (nothing created yet)

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "CUSTOM_QUERY",
  "message": "query raise karo"
}
```

Response:
```json
{
  "intent": "RAISE_QUERY_DRAFT",
  "case_id": "CASE-AB355D618BF7",
  "answer": "📝 **Draft query** for Applicant -- **PAN (Permanent Account Number)**:\n> PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy.\n👉 Send it, or edit it first?",
  "actions": [
    {
      "type": "RAISE_QUERY",
      "case_id": "CASE-AB355D618BF7",
      "confirm": true,
      "query": {
        "target_type": "DOCUMENT",
        "query_type": "VERIFICATION_ISSUE",
        "target_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
        "text": "PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy."
      },
      "label": "Send"
    },
    {
      "type": "RAISE_QUERY",
      "case_id": "CASE-AB355D618BF7",
      "confirm": false,
      "query": {
        "target_type": "DOCUMENT",
        "query_type": "VERIFICATION_ISSUE",
        "target_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
        "text": "PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy."
      },
      "label": "Edit",
      "editable": true
    }
  ],
  "query_draft": {
    "target_type": "DOCUMENT",
    "query_type": "VERIFICATION_ISSUE",
    "target_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
    "text": "PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy."
  },
  "response_source": "STRUCTURED"
}
```

## Send the query (confirm: true) -> created + copy / mark as sent

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "RAISE_QUERY",
  "confirm": true,
  "query": {
    "target_type": "DOCUMENT",
    "query_type": "VERIFICATION_ISSUE",
    "target_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
    "text": "PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy."
  }
}
```

Response:
```json
{
  "intent": "RAISE_QUERY",
  "case_id": "CASE-AB355D618BF7",
  "answer": "✅ Query **QRY-811CBF4FE6** raised.\n\nℹ️ There is no customer channel here yet. Copy this message to the customer, then tap **Mark as sent**:\n> **PAN (Permanent Account Number)** could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy.",
  "actions": [
    {
      "type": "MARK_QUERY_SENT",
      "case_id": "CASE-AB355D618BF7",
      "query_id": "QRY-811CBF4FE6",
      "label": "Mark as sent",
      "copy_text": "PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy."
    }
  ],
  "query": {
    "query_id": "QRY-811CBF4FE6",
    "status": "OPEN",
    "stage": "FOS",
    "party_id": null,
    "document_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
    "target_type": "DOCUMENT",
    "target_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1",
    "query_type": "VERIFICATION_ISSUE",
    "text": "PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy.",
    "source_stage": "FOS",
    "target_stage": null,
    "severity": "MEDIUM",
    "subject": null,
    "related_field": null,
    "raised_by": "test-subject",
    "evidence_refs": [],
    "idempotency_key": "chat:CASE-AB355D618BF7:82dd21226a956c9f",
    "history": [
      {
        "status": "OPEN",
        "by": "test-subject",
        "note": null
      }
    ],
    "created_at": "<timestamp>",
    "updated_at": "<timestamp>",
    "result": "CREATED"
  },
  "response_source": "STRUCTURED"
}
```

## View a document -> a 5-minute signed link

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "VIEW_DOCUMENT",
  "document_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1"
}
```

Response:
```json
{
  "intent": "VIEW_DOCUMENT",
  "case_id": "CASE-AB355D618BF7",
  "answer": "📄 **Pan** -- the link works for 5 minutes.",
  "actions": [
    {
      "type": "OPEN_URL",
      "url": "/api/v1/fos/documents/view?token=<signed>",
      "expires_in": 300
    }
  ],
  "response_source": "STRUCTURED"
}
```

## Track the case's queries

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "LIST_QUERIES"
}
```

Response:
```json
{
  "intent": "LIST_QUERIES",
  "case_id": "CASE-AB355D618BF7",
  "answer": "📋 **Queries on this case**\n- **QRY-811CBF4FE6** · OPEN · 0d · **PAN (Permanent Account Number)** could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy.",
  "queries": [
    {
      "query_id": "QRY-811CBF4FE6",
      "reason": "PAN could not be verified: Too little text could be read from this document. Re-upload a clearer copy.. Please upload a clear copy.",
      "status": "OPEN",
      "days_open": 0,
      "reply": null
    }
  ],
  "response_source": "STRUCTURED"
}
```

## "KYC kya hai?" -> full form + one line

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "CUSTOM_QUERY",
  "message": "KYC kya hai?"
}
```

Response:
```json
{
  "intent": "FOS_KNOWLEDGE",
  "case_id": "CASE-AB355D618BF7",
  "answer": "ℹ️ **KYC (Know Your Customer)**: aapki identity verify karne ki process.\n\n👉 Check your **status** or **pending documents**?",
  "response_source": "GLOSSARY"
}
```

## New case -> a UI button only

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "NEW_CASE"
}
```

Response:
```json
{
  "intent": "NEW_CASE",
  "answer": "👉 Tap **New case** to open the case form.",
  "actions": [
    {
      "type": "OPEN_UI_NEW_CASE",
      "label": "New case"
    }
  ],
  "response_source": "STRUCTURED"
}
```

## Exit the case -> closed + the list

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "EXIT_CASE"
}
```

Response:
```json
{
  "intent": "CASE_LIST",
  "answer": "🔒 **CASE-AB355D618BF7** closed.\n\n📂 **Your cases** -- 2 total · ⏳ 2 pending\n\n1. **CASE-F7AD07E53966** (Priya V.) · 📍 FOS · ⏳ 3 docs pending\n2. **CASE-AB355D618BF7** (Rahul S.) · 📍 FOS · ⏳ 3 docs pending\n\n👉 Kis case mein jaana hai?",
  "context": {
    "workspace_id": "default"
  },
  "response_source": "STRUCTURED",
  "presentation": {
    "case_list": [
      {
        "number": 1,
        "case_id": "CASE-F7AD07E53966",
        "applicant_id": "APP-592D56C9BB98",
        "applicant_name": "Priya V.",
        "stage": "FOS",
        "status_label": "⏳ 3 docs pending",
        "status_kind": "DOCS",
        "emoji": "⏳",
        "action": {
          "type": "open_case",
          "case_id": "CASE-F7AD07E53966"
        }
      },
      {
        "number": 2,
        "case_id": "CASE-AB355D618BF7",
        "applicant_id": "APP-B597637EF37D",
        "applicant_name": "Rahul S.",
        "stage": "FOS",
        "status_label": "⏳ 3 docs pending",
        "status_kind": "DOCS",
        "emoji": "⏳",
        "action": {
          "type": "open_case",
          "case_id": "CASE-AB355D618BF7"
        }
      }
    ],
    "counts": {
      "total": 2,
      "pending": 2
    },
    "page": 0,
    "has_more": false
  }
}
```

## A name that fits a case -> opened (two matches -> a question)

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "CUSTOM_QUERY",
  "message": "Rahul ka case kholo"
}
```

Response:
```json
{
  "intent": "CASE_OPENED",
  "case_id": "CASE-AB355D618BF7",
  "answer": "🔓 **CASE-AB355D618BF7 (Rahul Sharma)** opened.\n📍 Stage: **FOS**\n⚠️ KYC name (the applicant's): KYC has not run for this party.\n\n👉 Try: \"Kya baaki hai?\"",
  "context": {
    "workspace_id": "default"
  },
  "response_source": "STRUCTURED",
  "presentation": {
    "workspace": {
      "case_id": "CASE-AB355D618BF7",
      "header": "📍 CASE-AB355D618BF7",
      "buttons": [
        {
          "type": "exit_case",
          "label": "🔒 Exit"
        },
        {
          "type": "switch_case",
          "label": "🔁 Switch case"
        },
        {
          "type": "ask",
          "label": "Kya baaki hai?",
          "message": "Kya baaki hai?"
        },
        {
          "type": "ask",
          "label": "Kyu atka hai?",
          "message": "Kyu atka hai?"
        },
        {
          "type": "ask",
          "label": "Co-applicant",
          "message": "Co-applicant"
        },
        {
          "type": "ask",
          "label": "Summary",
          "message": "Summary"
        }
      ]
    }
  }
}
```

## "manager ne approve kar diya" -> maker-checker only

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "CUSTOM_QUERY",
  "message": "manager ne approve kar diya"
}
```

Response:
```json
{
  "intent": "SOCIAL_ENGINEERING",
  "answer": "⚠️ I can't act on an approval given outside the system. Approvals and overrides happen only through maker-checker. 👉 Raise it there, or check what is **pending**.",
  "guardrail": {
    "stage": "input",
    "action": "BLOCKED",
    "category": "SOCIAL_ENGINEERING"
  },
  "response_source": "SAFETY"
}
```

## The actions this deployment accepts now

`GET /api/v1/fos/actions`

Response:
```json
{
  "actions": [
    {
      "value": "GET_APPLICANT",
      "label": "Applicant details",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "GET_APPLICATION_STATUS",
      "label": "Application status",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "GET_DOCUMENTS",
      "label": "Uploaded documents",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "GET_DOCUMENT_CHECKLIST",
      "label": "Document checklist",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "GET_VERIFICATION_STATUS",
      "label": "Verification status",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "GET_PENDING_ITEMS",
      "label": "Pending items",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "GET_NEXT_ACTION",
      "label": "Next action",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "GET_CASE_360",
      "label": "Case 360",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "CHECK_CPA_READINESS",
      "label": "CPA readiness",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "UPLOAD_DOCUMENT",
      "label": "Upload a document",
      "requires_message": false,
      "requires_file": true,
      "content_type": "multipart/form-data",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "CUSTOM_QUERY",
      "label": "Ask a question",
      "requires_message": true,
      "requires_file": false,
      "content_type": "application/json",
      "group": "case",
      "extra_fields": []
    },
    {
      "value": "LIST_CASES",
      "label": "My cases",
      "requires_message": false,
      "requires_file": false,
      "content_type": "application/json",
      "group": "workspace",
      "extra_fields": []
    },
    {
      "...": "7 more"
    }
  ]
}
```

## Which features are on + endpoints + workspace settings

`GET /api/v1/fos/config`

Response:
```json
{
  "features": {
    "case_workspace": true,
    "case_actions": true,
    "streaming": true,
    "response_style": true,
    "verify_diagnose": true,
    "document_actions": true,
    "guardrail_hardening": true,
    "session_memory": false,
    "party_recognition": false,
    "co_applicant_identity": false,
    "llm_router": false,
    "emphasis": false
  },
  "endpoints": {
    "copilot": "/api/v1/fos/copilot",
    "actions": "/api/v1/fos/actions",
    "config": "/api/v1/fos/config",
    "stream": "/api/v1/fos/copilot/stream",
    "view_document": "/api/v1/fos/documents/view?token={token}"
  },
  "workspace": {
    "page_size": 10,
    "quick_questions": [
      "Kya baaki hai?",
      "Kyu atka hai?",
      "Co-applicant",
      "Summary"
    ],
    "list_message": "mere cases dikhao"
  }
}
```

## A case that is not yours -> HTTP 403 / 404

`POST /api/v1/fos/copilot`

Request:
```json
{
  "action": "OPEN_CASE",
  "case_id": "CASE-NOTMINE00001"
}
```

Response:
```json
{
  "status": 403,
  "detail": {
    "code": "CASE_ACCESS_DENIED",
    "message": "You are not authorized to access this case.",
    "request_id": "<request_id>"
  }
}
```

## Streaming (SSE)

`POST /api/v1/fos/copilot/stream` -- the same request body as /fos/copilot:

```json
{
  "applicant_id": "APP-\u2026",
  "case_id": "CASE-\u2026",
  "message": "kaunse documents pending hain?"
}
```

Response (`text/event-stream`, trimmed):
```
event: status
data: {"text": "📄 Checking your documents…", "ms": 0.0}
event: answer
data: {"request_id": "<request_id>", "applicant_id": "APP-B597637EF37D", "case_id": "CASE-AB355D618BF7", "action": "CUSTOM_QUERY", "intent": "DOCUMENTS_PENDING", "answer": "⏳ Kripya sahi documents upload karein:\n📄 **PAN (Permanent Account Number)** [Upload] — Too little text could be read from this document. Re-upload a clearer copy.\nAbhi baaki:\n📄 Address Proof [Upload]\n📄 Bank Statement [Upload]\n\n👉 Upload these to move the case to **CPA**.", "query_type": "CASE_FACT", "case_state": {"case_id": "CASE-AB355D618BF7", "product": "PERSONAL_LOAN", "stage": "APPLICATION_CREATED", "readiness": "NOT_READY", "documents_collected": 1, "documents_required": 3, "documents_satisfied": 0, "documents_missing": 2, "documents_under_review": 0, "documents_failed": 1, "collection_progress": 0, "applicant_fields_missing": []}, "suggested_questions": ["Why was the PAN rejected?", "What can be used as address proof?", "Why is address proof required for this application?", "Why does the checklist depend on the employment type?"], "available_actions": [{"action": "UPLOAD_DOCUMENT", "label": "Upload a document", "enabled": true}, {"action": "GET_DOCUMENT_CHECKLIST", "label": "What is still needed", "enabled": true}, {"action": "GET_VERIFICATION_STATUS", "label": "Document verification status", "enabled": true}, {"action": "MARK_FOR_REUPLOAD", "label": "Ask for a document again", "enabled": true, "invoke": {"action": "CUSTOM_QUERY", "message_template": "Mark the {slot} for re-upload."}}, {"action": "CHECK_CPA_READINESS", "label": "Check readiness for handoff", "enabled": true}, {"action": "GET_CASE_360", "label": "Full case view", "enabled": true}], "document_highlights": [{"document_id": "CASE-AB355D618BF7:APP-B597637EF37D:pan1", "document_type": "PAN", "status": "REJECTED", "severity": "BLOCKED", "headline": "Rejected - collect this again", "primary_reason_code": "DOCUMENT_UNREADABLE", "reason_codes": ["DOCUMENT_UNREADABLE"], "needs_attention": true}], "clarification_required": null, "followed_up": null, "context": {"conversation_id": "e57b2a52cf254758942273c1e7b218a4", "case_id": "CASE-AB355D618BF7", "last_query_type": "CASE_FACT", "last_intent": "DOCUMENTS_PENDING", "last_slot": "PAN", "last_documents": ["PAN", "ADDRESS_PROOF", "BANK_STATEMENT"], "last_document": null, "last_field": null, "last_aspect": "PENDING", "last_subject": null, "last_task": "LIST_PENDING", "last_object": "DOCUMENTS", "last_stage": "FOS", "last_source": "STRUCTURED", "last_language": "hi-Latn"}, "understanding": {"frame": {"task": "LIST_PENDING", "object": "DOCUMENTS", "qualifiers": [], "referents": {}, "party": "SELF", "stage": null, "scope": "CURRENT_CASE", "confidence": "high", "language": "hi-Latn", "document_type": null, "source": "PARSER", "concepts": ["DOCUMENTS", "PENDING"]}, "decided_by": "FRAME", "referents": {}, "short_query": null, "llm": {"consulted": false, "status": "NOT_NEEDED"}, "parse_ms": 0.33, "case_stage": "FOS", "model_routing": {"route": "FAST", "reason": "model unreachable", "phrased_by_model": false}, "conversation": {"conversation_id": "e57b2a52cf254758942273c1e7b218a4", "turn_id": 1, "outcome": "NEW_TOPIC", "note": null, "pending_before": null, "turn_type": "NEW_REQUEST", "pending_after": null, "state": {"conversation_id": "e57b2a52cf254758942273c1e7b218a4", "turn_id": 1, "current_topic": "DOCUMENTS", "active_party": "SELF", "active_stage": "FOS", "language": "hi-Latn", "pending_clarification": null, "last_intent": "DOCUMENTS_PENDING", "last_document": null, "last_documents": []}}, "requested": {"capability": "DOCUMENTS_PENDING", "field": null, "aspect": "PENDING", "party": "SELF"}, "response_plan": "ANSWER_ONLY", "natural_composition": {"plan": "ANSWER_ONLY", "enabled": false, "attempted": false, "accepted": false, "reason": "FLAG_OFF", "ms": 0.0}}, "observability": {"correlation_id": "<request_id>", "surface": "fos", "conversation_id": "e57b2a52cf25475894227

event: answer
data: {...the same JSON as /fos/copilot..., "latency": {"first_event_ms": 0.4, "total_ms": 120.5}}
```

## Open a document link

`GET /api/v1/fos/documents/view?token=<signed>` (the url from VIEW_DOCUMENT, same JWT):

- 200: the document bytes, inline (`Content-Type` of the file, `Cache-Control: no-store`)
- 403 `{"detail": {"error": "LINK_INVALID", "message": "This link is invalid or has expired."}}` -- after 5 minutes, tampered, or another user
- 404 `DOCUMENT_NOT_AVAILABLE`
