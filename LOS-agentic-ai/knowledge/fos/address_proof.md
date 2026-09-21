# Address proof

ADDRESS_PROOF is a checklist slot, not a document type. No document is called
an "address proof"; several documents can serve as one.

## Accepted documents

For every product currently configured, the ADDRESS_PROOF slot accepts:

- **Driving licence**
- **Passport**
- **Voter ID**

Any one of them satisfies the slot. A case does not need more than one.

## What is not accepted

A **PAN card is not an address proof.** A PAN card carries a name, a father's
name, a date of birth and a permanent account number. It does not carry an
address, so it cannot prove one. Uploading a PAN against the ADDRESS_PROOF
slot fails with DOCUMENT_TYPE_MISMATCH.

Utility bills, rent agreements and ration cards are commonly used as address
proof in the industry, but this service has no classifier or extractor for
them, so it cannot accept them. Only the three types above are configured.

## Uploading against the slot

A field officer can upload a driving licence either as DRIVING_LICENCE or as
ADDRESS_PROOF. Both work. The service classifies the file, confirms the class
is one the slot accepts, and marks the slot satisfied.
