# Credit document policies

One `*.credit.json` file per product (and scheme, when a scheme differs). `example_policy.json` documents the format and
is never loaded. Keys starting with `_` are comments. Any other unknown key makes the file **invalid**
(`POLICY_INVALID`), so a typo can never silently drop a rule.

## What a file holds

| Key | Rule | Meaning |
|---|---|---|
| `status`, `approval` | — | `SAMPLE` / `APPROVED` / `RETIRED`. `APPROVED` needs `approval.approved_by` and `approved_on`. A `SAMPLE` policy is refused outside development. |
| `effective_from`, `effective_to` | — | Applies only on these dates. |
| `applies_to` | — | Products, schemes (empty means any) and the stage (`CREDIT`). Exactly one policy may match a case. If none matches, the result is `POLICY_NOT_CONFIGURED`; if two match, `POLICY_AMBIGUOUS`. |
| `base` | 1, 3, 4, 5 | The requirements come from the existing product document policy (`app/config/policies/<product>.yaml` + `app/config/stage_requirements.yaml`), cumulative through `through_stage`. They are not copied here. |
| `document_states` | 3 | Which recorded document states count as satisfied, pending, under review or rejected, and which verification verdicts mean failure. |
| `parties` | 2 | What each role must provide: `base` (the product policy), the existing co-applicant checklist (`applicant_agent.yaml`), or a `policy` list. A role on the case that is not listed here is `UNRESOLVED`. |
| `period_and_freshness` | 6 | Months covered, minimum count, maximum age (on a named date field) and expiry. Numbers can come from the product policy (`*_from: "product_policy"`). An unreadable date is `UNRESOLVED`. |
| `supporting_documents` | 7 | A document that must come with another one. |
| `same_kind` | 5 | Types a customer may offer instead that the policy does **not** accept. These are reported, never counted. |
| `cross_stage` | 8 | Earlier-stage documents are reused. Types in `collect_at_stage` must be uploaded after the case entered this stage. |
| `exceptions` | 9 | Which deviation codes may waive which slots. Only an **APPROVED** deviation from the deviation / maker-checker workflow waives a slot. |
| `display_names` | — | How names are written in reasons (`PAN`, `ITR`). |
| `unevaluated_conditions_block` | 4 | Whether a condition the case cannot answer (for example, a missing employment type) blocks. |

## Changing a policy

1. Copy the file and raise `version`.
2. Have it approved, then set `status: APPROVED` and fill in `approval`.
3. Set `effective_from` / `effective_to` so that exactly one version is in force on any date.
4. Run `pytest tests/agents/test_docs_verify_credit_agent.py`.

Every evaluation records the file's SHA-256, so an audit always shows which file decided.
