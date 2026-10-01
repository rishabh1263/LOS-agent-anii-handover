# Trust anchors for document signatures

Place the CA certificates (PEM or DER: `.pem`, `.crt`, `.cer`, `.der`) that the
business trusts for **digitally signed documents** here — for example the
Controller of Certifying Authorities (CCA) India root and the issuing CAs used
by UIDAI (e-Aadhaar), the Income Tax Department (ITR acknowledgements),
DigiLocker, and each bank whose signed e-statements are accepted.

The directory is intentionally empty in this repository. With no anchor here,
`pdf_signature` still checks that a signed PDF is intact (and fails a broken
signature), but it never reports a document as issuer-confirmed:
`TRUST_ANCHOR_NOT_CONFIGURED`.

Which anchors to trust, and which signer organisations count for which document
type (`expected_signers` in `issuer_verification.yaml`), is a business and
security decision, not a code one.
