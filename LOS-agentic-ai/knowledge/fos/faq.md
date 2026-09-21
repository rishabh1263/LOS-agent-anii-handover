# Frequently asked questions

## Can I upload several documents at once?

Yes. `POST /api/v1/fos/copilot` with `action=UPLOAD_DOCUMENT` accepts multiple
files in one multipart request. Each file is classified and verified
independently, and the response carries a separate outcome for each one.

One document failing does not stop the others. A request carrying a PAN, a
driving licence and a dummy image returns a pass for the first two and a
failure for the third.

## Do I have to say what each document is?

No. If `document_types` is omitted the service classifies each file itself.

If it is supplied it is treated as an assertion and checked: a file that is
not the type asserted fails with DOCUMENT_TYPE_MISMATCH. The service never
silently reclassifies a file to match what was claimed.

## Why did my document fail?

The `reason_codes` on the document say why. The most common causes are the
wrong file being uploaded for a slot, a photograph too poor to read, and a
screenshot or photocopy where the original was expected.

## A document says REVIEW. What do I do?

REVIEW means a person has to look at it. The most common cause is a required
field the service could not read — a date of birth that did not survive being
photographed, for example. Re-photographing the document in better light
often resolves it.

REVIEW is not a rejection and it is not a pass.

## The applicant name is spelled differently on two documents. Is that a problem?

Not by itself. The service compares names across documents and tolerates case,
spacing, punctuation, ordering and expanded initials. A genuine difference —
a married name, a corrected record — is reported for a person to resolve, and
never rejects the applicant automatically.

## What is the KYC check?

KYC here means **cross-document consistency**: do the documents describe the
same person? It compares name, date of birth, PAN, father's name and address
across the documents that passed verification.

It does not establish that any document is genuine, and it is not a credit or
fraud decision. A mismatch routes the case to a human.

## What is the difference between match score and confidence on a KYC field?

`match_score` is how closely the values agree with each other.
`confidence` is how far that answer can be relied on.

They are different questions. Two barely-legible documents that agree exactly
score 100 for match and much lower for confidence. Two cleanly-read documents
that plainly differ score 0 for match and high for confidence, because the
disagreement is real and worth acting on.

## Can I see the applicant credit score?

No. Credit, bureau, risk, fraud and lending decisions all belong to later
stages. Asking about them returns a `route_to` naming the stage that owns the
question, and no answer — the FOS stage does not hold that information and
will not guess at it.

## Does the same file uploaded to two cases get mixed up?

No. Documents are held against the applicant and the case, never against the
filename. Uploading `PAN.pdf` to two different cases produces two independent
records.

## What file types can I upload?

Images (JPEG, PNG) and PDFs. A scanned PDF is read by OCR; a digital PDF is
read from its text layer, which is faster and more accurate.

## The upload is slow. Why?

OCR is the floor. A photographed document has to be rasterised and read
before anything else can happen, and that dominates the time. Uploading
several documents in one request is faster than several requests, because
they are processed together.
