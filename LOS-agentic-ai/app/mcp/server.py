"""
The LOS MCP server.

Registers the seven LOS capabilities as MCP tools. Every tool is a one-line
forward into app.mcp.capabilities, which is where validation, sandboxing and
error shaping live; this file is registration and docstrings only, because the
docstrings ARE the tool descriptions a model reads when choosing a tool.

Boundaries this server keeps:

  * It adds no business logic. OCR, classification, extraction, verification
    and KYC all stay in their existing services.
  * It never decides PASS/REVIEW/FAIL. Those verdicts arrive from the
    deterministic services and are passed through unchanged, so a model can
    report a verdict but cannot produce or alter one.
  * It reads only inside AGENT_UPLOAD_ROOT. There is no tool that takes an
    arbitrary filesystem path.

The existing servers under app/mcp/document and app/mcp/common are left alone,
so anything already pointed at them keeps working.
"""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from app.mcp import capabilities

load_dotenv()

mcp = FastMCP("los")


# ==========================================================================
# DOCUMENTS
# ==========================================================================


@mcp.tool(name="document.verify")
async def document_verify(document_type: str, file_path: str) -> dict[str, Any]:
    """
    Verify an uploaded LOS document and return its deterministic verdict.

    Returns PASS, REVIEW, FAIL or SKIPPED together with the individual checks
    behind it. The verdict is computed by the Document Agent workflow and must
    be reported as-is: do not re-derive, override or soften it.

    Verification establishes that a document is readable and internally
    consistent. It does not establish that the document is genuine.

    The file must already be inside the agent upload area.
    """
    outcome = await capabilities.document_verify(document_type, file_path)
    return outcome.as_tool_payload()


@mcp.tool(name="document.extract")
async def document_extract(document_type: str, file_path: str) -> dict[str, Any]:
    """
    Extract structured fields from an uploaded document.

    Returns the fields the extractor found and its confidence. This is
    extraction only -- it produces no verdict. Use document.verify when you
    need to know whether a document passes.

    The file must already be inside the agent upload area.
    """
    outcome = await capabilities.document_extract(document_type, file_path)
    return outcome.as_tool_payload()


@mcp.tool(name="document.get")
async def document_get(file_path: str) -> dict[str, Any]:
    """
    Metadata for an uploaded document: name, size, extension.

    Cheap. It does not read, OCR or classify the file, so use it to confirm a
    document is present and plausible before calling document.extract or
    document.verify, both of which are far more expensive.
    """
    outcome = await capabilities.document_get(file_path)
    return outcome.as_tool_payload()


# ==========================================================================
# FINANCIAL
# ==========================================================================


@mcp.tool(name="financial.analyze")
async def financial_analyze(
    file_path: str,
    document_type: str | None = None,
) -> dict[str, Any]:
    """
    Analyse a financial document: bank statement, ITR, salary slip or sale deed.

    Returns normalised income signals and the extracted financial fields.
    `document_type` is an optional hint only; the Financial Agent detects the
    type itself and its detection wins when it is confident.

    The file must already be inside the agent upload area.
    """
    outcome = await capabilities.financial_analyze(file_path, document_type)
    return outcome.as_tool_payload()


@mcp.tool(name="sale_deed.analyze")
async def sale_deed_analyze(
    file_path: str,
    source_id: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """
    Assess a Sale Deed for Property Ownership Proof.

    Reads the e-Stamp certificate cover page and returns a deterministic
    PASS/REVIEW/FAIL with the reason codes behind it, plus the page the
    evidence came from.

    Scope limit, and it matters: the deed BODY -- property schedule, survey
    number, area, full address -- is NOT read. Real deeds arrive as
    photographs of handwritten regional-script forms. Every result carries
    DEED_BODY_NOT_READABLE and OWNERSHIP_NOT_ESTABLISHED. A PASS means a
    well-formed e-Stamp certificate was present, never that the seller owns
    the property or that the deed is genuine.

    The file must already be inside the agent upload area.
    """
    outcome = await capabilities.sale_deed_analyze(
        file_path, source_id, request_id
    )
    return outcome.as_tool_payload()


@mcp.tool(name="business_evidence.analyze")
async def business_evidence_analyze(
    file_path: str,
    slot: str = "BUSINESS_PROOF_1",
    source_id: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """
    Assess a business-premises photograph for Business Proof 1 or 2.

    `slot` is BUSINESS_PROOF_1 or BUSINESS_PROOF_2 -- the same capability
    serves both, because they are the same kind of evidence in two form
    slots.

    Returns where and when the photo was taken (from EXIF, or from a burned-in
    GPS Map Camera overlay when EXIF has been stripped), an image-quality
    assessment, and a deterministic PASS/REVIEW/FAIL.

    Scope limit: this establishes that a photograph exists with certain
    metadata. It does NOT establish that the business exists, that it
    operates, or that the applicant owns it. Every result says so.
    """
    outcome = await capabilities.business_evidence_analyze(
        file_path, slot, source_id, request_id
    )
    return outcome.as_tool_payload()


@mcp.tool(name="signature.verify")
async def signature_verify(
    file_path: str,
    document_type: str,
    reference_path: str = "",
    source_id: str = "",
    request_id: str = "",
) -> dict[str, Any]:
    """
    Verify a signature on a bank sign card, PAN, driving licence or passport.

    `document_type` is BANK_SIGNATURE, PAN_SIGNATURE,
    DRIVING_LICENSE_SIGNATURE or PASSPORT_SIGNATURE. Pass `reference_path`
    when a known-good specimen is available; both files must be inside the
    agent upload area.

    Returns presence, image quality, and -- only when a reference was given
    and both images were good enough -- a comparison with a score.

    Read the verdict carefully. PASS requires a reference comparison that
    matched. Without a reference the answer is REVIEW with comparison
    NOT_COMPARABLE, however obvious the signature looks: a signature being
    present is not evidence that it is genuine, and no result here
    establishes authenticity.
    """
    outcome = await capabilities.signature_verify(
        file_path, document_type, reference_path, source_id, request_id
    )
    return outcome.as_tool_payload()


# ==========================================================================
# KYC
# ==========================================================================


@mcp.tool(name="kyc.run")
async def kyc_run(
    request: dict[str, Any],
    request_id: str = "",
) -> dict[str, Any]:
    """
    Run KYC consistency checks across documents that have already been extracted.

    Takes the extracted documents themselves, not file paths: KYC compares
    documents against each other and never reads a file, so extract first and
    pass the results here.

    Returns a deterministic status with the reason codes and per-check results
    behind it. Report that status as given; it is not yours to adjust.

    KYC establishes consistency BETWEEN documents. It does not establish that
    any of them is genuine.
    """
    outcome = await capabilities.kyc_run(request, request_id)
    return outcome.as_tool_payload()


# ==========================================================================
# CASE AND POLICY
# ==========================================================================


@mcp.tool(name="case.get")
async def case_get(case_id: str) -> dict[str, Any]:
    """
    Fetch a stored case by id.

    This build has no case store, so the call reports CASE_STORE_UNAVAILABLE
    rather than returning a case. Work from the documents directly via
    document.get, document.verify and kyc.run.
    """
    outcome = await capabilities.case_get(case_id)
    return outcome.as_tool_payload()


@mcp.tool(name="policy.get")
async def policy_get(name: str) -> dict[str, Any]:
    """
    Read a named policy: "risk", "verification" or "documents".

    Returns the thresholds and switches the deterministic services are
    currently applying -- useful for explaining WHY a verdict came out as it
    did. Reading a policy does not let you apply a different one.
    """
    outcome = await capabilities.policy_get(name)
    return outcome.as_tool_payload()


# ==========================================================================
# INTROSPECTION
# ==========================================================================

#: The capability surface, in one place, so a test can assert on it.
TOOLS = (
    "document.verify",
    "document.extract",
    "document.get",
    "financial.analyze",
    "sale_deed.analyze",
    "business_evidence.analyze",
    "signature.verify",
    "kyc.run",
    "case.get",
    "policy.get",
)


if __name__ == "__main__":
    mcp.run(transport="stdio")
