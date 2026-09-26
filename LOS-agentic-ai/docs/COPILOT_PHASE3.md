# Universal Copilot — Phase 3 additions (handover)

Everything here is additive to the existing `POST /api/v1/copilot/query`
contract. Existing fields keep their meaning.

## Pipeline order (truth first, language last)

```
channel -> auth -> ownership -> stage resolver -> conversation context (advisory)
-> input guardrail -> LANGUAGE (detect + canonicalise) -> normalise -> intent + subject
-> CASE_ONLY / KNOWLEDGE_ONLY / MIXED -> MCP tools -> RAG (bounded, scoped)
-> evidence -> history -> impact -> next best action -> optional JEV
-> compact composer context -> Qwen only if required (<= 1 call)
-> UNIFIED VALIDATOR -> output guardrail -> provenance
-> conversation layer (localized fact / tone / handoff) -> response
-> analytics / OTEL / CloudWatch
```

## Request fields

| Field | Meaning |
|---|---|
| `language` | Answer language (`en`, `hi`, `mr`, `hi-Latn`, …). Omitted: the language the question was written in. |
| `channel` | `web`, `mobile`, `whatsapp`, `fos_app`, `agent_desktop`, `api`. Echoed and counted; never changes identity, authorization, tools or facts. |
| `context` | As before, plus `language`, `last_stage`, `unresolved_turns` (advisory only — the records always win). |

## Response fields

| Field | Meaning |
|---|---|
| `language` | `detected`, `script`, `romanized`, `code_mixed`, `review_status` (lexicon review state), `response_language`, `localized`. |
| `sentiment` | `level` (neutral / confused / frustrated / high_frustration), `intensity`, `signals` (families, never words), `affects: TONE_ONLY`. |
| `handoff` | Slice 9 keys (`required`, `reason`, `priority`) **plus** `handoff_required`, `recommended`, `handoff_reason`, `handoff_priority`, `triggers`, `unresolved_turns`, `status: SIGNAL_ONLY`, and `handoff_summary` (codes/labels only) when required or recommended. |
| `channel` | Echoed channel. |
| `response_contract_version` | `"3.0"`. |
| `timings` | Adds `auth_ms`, `routing_ms`, `retrieval_encode_ms` / `retrieval_encode_cache_hits` (question embedding), `jev_ms` (when JEV runs). |

## Multilingual

* `app/agents/applicant/language.py` + `app/config/languages.yaml`.
* Script detection for Devanagari (hi / mr / kok by markers), Bengali (bn / as),
  Gurmukhi, Gujarati, Odia, Tamil, Telugu, Kannada, Malayalam, Arabic (ur);
  romanized Hindi by marker words.
* Lexicon + word-order rewrites turn the question into canonical English,
  which the SAME rules classify. No per-language business logic.
* Localized answers only for facts with a deterministic template
  (currently: current stage, 14 language variants). Everything else is the
  English answer with `language.localized: false`.
* **Lexicons are seeds, not reviewed by native speakers**
  (`review_status: SEED_UNREVIEWED`). Extend them in YAML.

## Unified validator

`app/security/output_validation.py` — one module for every model-written text
(`applicant_answer`, `copilot_composer`, `knowledge_phrase`, `case_summary`,
`los_summary`, `fraud_summary`, `document_workflow`). Common checks: reasoning
removal, leakage (output guardrail), shape, decision language, unsupported
numbers, unsupported dates; then each surface's own checks.

## Observability

* OTEL: `OTEL_ENABLED=true`, `OTEL_TRACES_EXPORTER=otlp|console`. Configured at
  startup; one `http.request` span per request plus `auth.authenticate`,
  `copilot.*`, `mcp.tool`, `rag.embed`, `jev.annotate`, `llm.compose`,
  `copilot.validator`, `copilot.guardrail`. Attributes are filtered — no token,
  header, prompt, message or content can be recorded.
* CloudWatch: `CLOUDWATCH_ENABLED=true`, `CLOUDWATCH_MODE=emf|api`.
  `emf` writes Embedded Metric Format lines (no AWS credentials in the app);
  `api` uses boto3 + the standard AWS credential chain. `GET /ops/analytics`
  reports the real export state (never claims unconfirmed delivery).
* Analytics: `GET /ops/analytics` (JWT + `los.ops.read`, configurable via
  `ANALYTICS_REQUIRED_SCOPE`) and the Prometheus scrape. Aggregate counters
  only; closed-set labels; no prompts, answers or ids.

## Flags

`MULTILINGUAL_ENABLED`, `SENTIMENT_ENABLED`, `HANDOFF_ENABLED`,
`ANALYTICS_ENABLED`, `ANALYTICS_REQUIRED_SCOPE`, `OTEL_ENABLED`,
`OTEL_TRACES_EXPORTER`, `CLOUDWATCH_ENABLED`, `CLOUDWATCH_MODE`,
`CLOUDWATCH_NAMESPACE`, `CLOUDWATCH_REGION`, `COPILOT_WARMUP`,
`AUTH_LOCAL_SCOPES` (default now `los.read`). YAML: `chatbot.sentiment`,
`chatbot.handoff` in `applicant_agent.yaml`; `languages.yaml`.

## Not built (external dependency or deferred)

* No JEV provider exists — the hook is async, bounded and disabled.
* No CREDIT / RCU / decision / fraud Copilot providers — those stages answer
  stage facts and say `CAPABILITY_UNAVAILABLE` for the rest.
* No channel connectors (WhatsApp / mobile), no agent desktop, no human desk.
* CloudWatch delivery is not verified against a real AWS account here.
