from __future__ import annotations

"""
DOCUMENT EXTRACTION AGENT
=========================

Current OCR:
    RapidOCR / PP-OCRv4

Current extraction:
    PAN primary name

Architecture:

    Uploaded file
         |
         v
      RapidOCR
         |
         v
    OCRToken list
         |
         v
   PAN Name Extractor
         |
         v
   ExtractionResult

Surya is NOT used.
"""

import time
from pathlib import Path
from typing import Any

from app.agents.document_agent.ocr import (
    get_engine,
    get_ocr_executor,
)

from app.agents.document_extraction.schemas import (
    ExtractionResult,
)

from app.agents.document_extraction.pan_name_extractor import (
    extract_name,
)


class DocumentExtractionAgent:
    """
    Main document extraction agent.

    Current implemented extraction:
        PAN -> name

    OCR:
        RapidOCR / PP-OCRv4
    """

    def __init__(self, debug: bool = True) -> None:
        self.debug = debug

    # ======================================================================
    # DOCUMENT TYPE
    # ======================================================================

    @staticmethod
    def _normalize_document_type(
        document_type: Any,
    ) -> str:

        value = str(
            document_type or ""
        ).strip().upper()

        value = value.replace("-", "_")

        value = "_".join(
            value.split()
        )

        return value

    # ======================================================================
    # DOCUMENT FAMILY
    # ======================================================================

    @classmethod
    def _document_family(
        cls,
        document_type: Any,
    ) -> str:

        normalized = cls._normalize_document_type(
            document_type
        )

        if normalized in {
            "PAN",
            "PAN_CARD",
            "PERMANENT_ACCOUNT_NUMBER",
        }:
            return "PAN"

        if normalized in {
            "AADHAAR",
            "AADHAR",
            "AADHAAR_CARD",
            "AADHAR_CARD",
        }:
            return "AADHAAR"

        if normalized in {
            "VOTER",
            "VOTER_ID",
            "EPIC",
            "EPIC_CARD",
            "ELECTION_CARD",
        }:
            return "VOTER_ID"

        if normalized == "PASSPORT":
            return "PASSPORT"

        if normalized in {
            "DL",
            "DRIVING_LICENSE",
            "DRIVING_LICENCE",
            "DRIVING_LICENSE_CARD",
        }:
            return "DRIVING_LICENSE"

        return "GENERIC"

    # ======================================================================
    # OCR
    # ======================================================================

    @staticmethod
    def _run_ocr(
        file_path: str,
    ):
        """
        Run RapidOCR against a file path.

        IMPORTANT:
            RapidOCR adapter's read() expects a path.

        Returns:
            tuple[list[OCRToken], float]
        """

        executor = get_ocr_executor()
        engine = get_engine()

        future = executor.submit(
            engine.read,
            file_path,
        )

        return future.result()

    # ======================================================================
    # OCR VALIDATION
    # ======================================================================

    @staticmethod
    def _validate_ocr_tokens(
        tokens: Any,
    ) -> None:

        if tokens is None:
            raise ValueError(
                "RapidOCR returned None."
            )

        if not isinstance(tokens, list):
            raise ValueError(
                "RapidOCR returned invalid token collection."
            )

        if not tokens:
            raise ValueError(
                "RapidOCR returned no text."
            )

        valid_tokens = [
            token
            for token in tokens
            if str(
                getattr(
                    token,
                    "text",
                    "",
                ) or ""
            ).strip()
        ]

        if not valid_tokens:
            raise ValueError(
                "RapidOCR returned no readable text."
            )

    # ======================================================================
    # NAME EXTRACTION
    # ======================================================================

    def _extract_primary_name(
        self,
        tokens: list[Any],
        document_type: str,
    ) -> Any:

        family = self._document_family(
            document_type
        )

        # ------------------------------------------------------------------
        # PAN
        # ------------------------------------------------------------------

        if family == "PAN":

            return extract_name(tokens)

        # ------------------------------------------------------------------
        # Other documents are not implemented yet.
        # ------------------------------------------------------------------

        print(
            "NAME EXTRACTION:",
            f"No extractor implemented for family={family}",
        )

        return None

    # ======================================================================
    # SUCCESS
    # ======================================================================

    @staticmethod
    def _build_success_result(
        document_type: str,
        name_result: Any,
    ) -> ExtractionResult:

        value = getattr(
            name_result,
            "value",
            None,
        )

        confidence = getattr(
            name_result,
            "confidence",
            None,
        )

        if confidence is None:
            confidence = getattr(
                name_result,
                "score",
                0.0,
            )

        try:
            confidence = float(
                confidence or 0.0
            )
        except (TypeError, ValueError):
            confidence = 0.0

        return ExtractionResult(
            document_type=document_type,
            status="SUCCESS",
            confidence=round(
                confidence,
                4,
            ),
            fields={
                "name": value,
            },
        )

    # ======================================================================
    # FAILED
    # ======================================================================

    @staticmethod
    def _build_failed_result(
        document_type: str,
    ) -> ExtractionResult:

        return ExtractionResult(
            document_type=document_type,
            status="FAILED",
            confidence=0.0,
            fields={
                "name": None,
            },
        )

    # ======================================================================
    # MAIN EXTRACTION
    # ======================================================================

    def extract(
        self,
        file_path: str | Path,
        document_type: str,
    ) -> ExtractionResult:

        started = time.perf_counter()

        normalized_document_type = (
            self._normalize_document_type(
                document_type
            )
        )

        family = self._document_family(
            normalized_document_type
        )

        print(
            "\n========== DOCUMENT EXTRACTION =========="
        )

        print(
            "DOCUMENT TYPE:",
            normalized_document_type,
        )

        print(
            "DOCUMENT FAMILY:",
            family,
        )

        print(
            "EXTRACTION:",
            "NAME",
        )

        print(
            "OCR ENGINE:",
            "RapidOCR / PP-OCRv4",
        )

        print(
            "=========================================="
        )

        # ==================================================================
        # FILE VALIDATION
        # ==================================================================

        path = Path(file_path)

        if not path.is_file():

            print(
                "FILE ERROR:",
                f"File not found: {path}",
            )

            result = self._build_failed_result(
                normalized_document_type
            )

            self._print_final_result(
                result,
                started,
            )

            return result

        # ==================================================================
        # OCR
        # ==================================================================

        try:

            print(
                "\n========== DOCUMENT OCR =========="
            )

            ocr_started = time.perf_counter()

            tokens, engine_ocr_ms = (
                self._run_ocr(
                    str(path)
                )
            )

            local_ocr_ms = round(
                (
                    time.perf_counter()
                    - ocr_started
                )
                * 1000,
                2,
            )

            self._validate_ocr_tokens(
                tokens
            )

            print(
                "OCR STATUS:",
                "SUCCESS",
            )

            print(
                "OCR TOKENS:",
                len(tokens),
            )

            print(
                "OCR ENGINE TIME:",
                round(
                    float(
                        engine_ocr_ms or 0.0
                    ),
                    2,
                ),
                "ms",
            )

            print(
                "OCR TOTAL TIME:",
                local_ocr_ms,
                "ms",
            )

            print(
                "==================================="
            )

        except Exception as exc:

            print(
                "\n========== DOCUMENT OCR =========="
            )

            print(
                "OCR STATUS:",
                "FAILED",
            )

            print(
                "OCR ERROR:",
                repr(exc),
            )

            print(
                "==================================="
            )

            result = self._build_failed_result(
                normalized_document_type
            )

            self._print_final_result(
                result,
                started,
            )

            return result

        # ==================================================================
        # NAME EXTRACTION
        # ==================================================================

        print(
            "\n========== NAME EXTRACTION =========="
        )

        print(
            "DOCUMENT FAMILY:",
            family,
        )

        print(
            "NAME EXTRACTOR:",
            (
                "PAN_NAME_EXTRACTOR"
                if family == "PAN"
                else "NOT_IMPLEMENTED"
            ),
        )

        print(
            "OCR TOKENS:",
            len(tokens),
        )

        print(
            "====================================="
        )

        try:

            name_result = (
                self._extract_primary_name(
                    tokens,
                    normalized_document_type,
                )
            )

        except Exception as exc:

            print(
                "\n========== NAME EXTRACTION ERROR =========="
            )

            print(
                "ERROR:",
                repr(exc),
            )

            print(
                "==========================================="
            )

            result = self._build_failed_result(
                normalized_document_type
            )

            self._print_final_result(
                result,
                started,
            )

            return result

        # ==================================================================
        # NAME NOT FOUND
        # ==================================================================

        if name_result is None:

            print(
                "\n========== NAME EXTRACTION =========="
            )

            print(
                "STATUS:",
                "NAME_NOT_FOUND",
            )

            print(
                "====================================="
            )

            result = self._build_failed_result(
                normalized_document_type
            )

            self._print_final_result(
                result,
                started,
            )

            return result

        # ==================================================================
        # SUCCESS
        # ==================================================================

        result = self._build_success_result(
            normalized_document_type,
            name_result,
        )

        self._print_final_result(
            result,
            started,
            name_result=name_result,
        )

        return result

    # ======================================================================
    # COMPATIBILITY
    # ======================================================================

    def extract_document(
        self,
        file_path: str | Path,
        document_type: str,
    ) -> ExtractionResult:

        return self.extract(
            file_path=file_path,
            document_type=document_type,
        )

    # ======================================================================
    # DEBUG
    # ======================================================================

    @staticmethod
    def _print_final_result(
        result: ExtractionResult,
        started: float,
        name_result: Any = None,
    ) -> None:

        total_time_ms = round(
            (
                time.perf_counter()
                - started
            )
            * 1000,
            2,
        )

        print(
            "\n========== DOCUMENT EXTRACTION =========="
        )

        print(
            "DOCUMENT TYPE:",
            result.document_type,
        )

        print(
            "STATUS:",
            result.status,
        )

        print(
            "CONFIDENCE:",
            result.confidence,
        )

        print(
            "FIELDS:",
            result.fields,
        )

        if name_result is not None:

            print(
                "NAME SOURCE:",
                getattr(
                    name_result,
                    "source",
                    None,
                ),
            )

            print(
                "NAME SCORE:",
                getattr(
                    name_result,
                    "score",
                    None,
                ),
            )

        print(
            "TIME:",
            total_time_ms,
            "ms",
        )

        print(
            "=========================================="
        )


# ==========================================================================
# SHARED AGENT
# ==========================================================================

document_extraction_agent = (
    DocumentExtractionAgent(
        debug=True,
    )
)


# ==========================================================================
# PUBLIC FUNCTION
# ==========================================================================

def extract_document(
    file_path: str | Path,
    document_type: str,
) -> ExtractionResult:

    return document_extraction_agent.extract(
        file_path=file_path,
        document_type=document_type,
    )


# ==========================================================================
# API COMPATIBILITY ENTRY POINT
# ==========================================================================

async def run_extraction_agent(
    *,
    document_type: str,
    file_path: str,
) -> ExtractionResult:

    return document_extraction_agent.extract(
        file_path=file_path,
        document_type=document_type,
    )


# ==========================================================================
# EXPORTS
# ==========================================================================

__all__ = [
    "DocumentExtractionAgent",
    "document_extraction_agent",
    "extract_document",
    "run_extraction_agent",
]