# How the document checklist is decided

A case's checklist is not a fixed list. It is resolved, per case, by the
document policy engine from a configured policy file for the product. This
page explains what that means when a field officer is standing in front of a
customer asking why a document is needed.

## Where the rules come from

Each product may have a policy file. The file holds:

- **Base requirements** — the documents every application for that product
  needs, whatever the amount.
- **Amount rules** — bands that can add documents above a threshold.
- **Conditional rules** — rules keyed on something about the applicant, such
  as employment type.
- **Document evidence requirements** — what has to be readable on a
  document for it to satisfy its slot.

Rules only ever ADD. No combination of amount and attributes can remove a
base requirement, so no case ends up needing less identity evidence than
every other case.

## Why a document is on the checklist

Every checklist row names the rule that produced it, in `rule_ids`, and the
response names the policy version those rules came from. A row also carries
a `reason` written for a person, and `applicable_conditions` when it applies
because of something specific about this case.

Asking the copilot "why does the checklist require this?" returns that
explanation: the policy id, the version, which rules applied, and what made
each conditional requirement apply.

## Requirement and fulfilment are two different things

A checklist row answers two questions, and they have separate fields.

`requirement` — how strongly the case needs the slot:

- **REQUIRED** — needed for this case whatever its attributes.
- **CONDITIONAL** — needed because of something about this case. It is just
  as binding as REQUIRED once the condition has matched; the distinction is
  there so the reason can be shown.
- **OPTIONAL** — offered. Never blocks the handoff.
- **NOT_APPLICABLE** — declared by policy but excluded for this case.

`fulfilment` — how far the slot has got:

- **SATISFIED** — a verified document fills it.
- **MISSING** — nothing has been collected.
- **IN_PROGRESS** — collected, not yet concluded.
- **UNDER_REVIEW** — collected, and a person needs to look at it.
- **FAILED** — collected and rejected. Collect it again.

A row can be REQUIRED and MISSING at the same time; that is the ordinary
state of a new case.

## When the checklist is not final

Some rules cannot be evaluated until the case captures something. A rule
that depends on the loan amount cannot apply to a case with no amount
recorded; a rule that depends on employment type cannot apply to a case that
has not recorded one.

The engine does not guess. It does not impose the requirement, and it does
not silently drop the rule. It reports it in `policy.unevaluated_rules`,
naming the missing attribute and what the rule would have asked for.

This matters in the field. A checklist that quietly omits a rule looks
complete, the officer collects to it, and the customer is called back. A
checklist that says "the loan amount has not been captured, so amount-based
rules were not applied" tells the officer what to do about it.

## Placeholder thresholds

A policy file carries a `status`. `UNCONFIRMED` means the thresholds in it
are placeholders that no lender has signed off — they exist so the engine
can be exercised end to end, and they are not this lender's policy, an
industry standard, or a regulatory requirement.

Every checklist row derived from an unconfirmed policy carries
`policy_status: UNCONFIRMED`, so it can be shown as provisional rather than
presented as a rule.

## The version a case was opened under

A case records the policy version it was first assessed under. Policy files
are edited while applications are in flight, and without that record an
applicant told on Monday to bring three documents could be told on Wednesday
to bring five with nothing explaining the change.

The service resolves against the CURRENT file — it does not keep superseded
versions and cannot re-run one. When the current version differs from the
one a case was opened under, the response says so in `version_changed` and
reports both, rather than printing the pinned version next to a checklist
built from a different one.

## What the policy engine does not decide

It says what should be collected. It does not say whether a collected
document is genuine — that is verification — and it does not say anything
about the applicant's creditworthiness, risk or KYC status. Those belong to
stages after FOS.
