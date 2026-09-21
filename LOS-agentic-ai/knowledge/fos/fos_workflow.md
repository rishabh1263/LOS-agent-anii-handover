# The FOS stage

The Field Officer Sales (FOS) stage is the first stage of a loan application.
A field officer meets the applicant, captures their details, collects their
documents, and hands the case to the CPA desk once the file is complete.

## What the FOS stage is responsible for

- Capturing applicant information: name, mobile, date of birth, address
- Creating the application and choosing the loan product
- Collecting the documents the product requires
- Making sure each document is readable and is the document it claims to be
- Handing a complete file to CPA

## What the FOS stage is NOT responsible for

The FOS stage does not decide anything about the loan. It does not assess
credit, risk or fraud, it does not run bureau checks, and it does not approve
or reject an application. Those decisions belong to later stages, and a field
officer who is asked about them should route the question onward rather than
answer it.

## The lifecycle

A case moves through four states:

1. **APPLICATION_CREATED** — the applicant and application records exist,
   no documents have been collected yet.
2. **DOCUMENT_COLLECTION** — documents are being uploaded.
3. **BASIC_DOCUMENT_VERIFICATION** — at least one document has a verification
   verdict against it.
4. **READY_FOR_CPA** — every mandatory checklist slot is satisfied and nothing
   is outstanding.

The stage is derived from the records on each request, not stored and trusted.
A case whose documents have all been verified reports READY_FOR_CPA even if
nothing explicitly wrote that transition.
