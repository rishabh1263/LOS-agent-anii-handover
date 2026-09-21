"""
Column reconstruction for scanned statements.

A scanned page has no cell structure, so an earlier attempt read each OCR line
as text and guessed which numbers were the movement and the balance. It was
wrong on every row.

Bank statements are ruled tables, and those rules survive scanning. Detecting
the printed grid lines gives real cell boundaries, and each cell can then be
read on its own -- which is the same guarantee pdfplumber provides for digital
PDFs, obtained a different way.

Credit: the morphological line-detection approach follows the technique used
in the bank-statement-to-Tally tool.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# A real column is at least this wide in pixels at 200 dpi. Narrower splits are
# sub-dividers inside a header box, not column boundaries.
MIN_COLUMN_WIDTH = 150
MIN_ROW_HEIGHT = 18


def _merge_close(values: list[int], threshold: int = 15) -> list[int]:
    """Collapse near-duplicate line positions into one."""
    if not values:
        return values
    merged = [values[0]]
    for value in values[1:]:
        if value - merged[-1] > threshold:
            merged.append(value)
    return merged


def detect_grid(image_gray) -> tuple[list[int], list[int]]:
    """
    Return (row_ys, col_xs): the y and x positions of the printed table rules.

    Horizontal and vertical strokes are isolated with long, thin morphological
    kernels, so body text -- which has no long runs in either direction -- does
    not register as a line.
    """
    import cv2

    _, binary = cv2.threshold(image_gray, 180, 255, cv2.THRESH_BINARY_INV)

    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (80, 1))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    contours, _ = cv2.findContours(
        horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    row_ys = sorted(
        {cv2.boundingRect(c)[1] for c in contours if cv2.boundingRect(c)[2] > 200}
    )

    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)
    contours, _ = cv2.findContours(
        vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    col_xs = sorted(
        {cv2.boundingRect(c)[0] for c in contours if cv2.boundingRect(c)[3] > 50}
    )

    row_ys = _merge_close(row_ys, 15)
    col_xs = _merge_close(col_xs, 15)

    # Drop boundaries that would create implausibly narrow columns.
    if len(col_xs) > 2:
        kept = [col_xs[0]]
        for index in range(1, len(col_xs)):
            if col_xs[index] - kept[-1] >= MIN_COLUMN_WIDTH:
                kept.append(col_xs[index])
            elif index == len(col_xs) - 1:
                kept.append(col_xs[index])
        col_xs = kept

    return row_ys, col_xs


def cells_from_tokens(
    tokens,
    row_ys: list[int],
    col_xs: list[int],
) -> list[list[str]]:
    """
    Place OCR tokens into grid cells.

    Each token is assigned by the position of its centre, so a token that
    overhangs a rule still lands in the cell it belongs to. Tokens outside the
    grid -- page headers, footers, the bank's masthead -- are discarded.
    """
    if len(row_ys) < 2 or len(col_xs) < 2:
        return []

    rows: list[list[list[str]]] = [
        [[] for _ in range(len(col_xs) - 1)] for _ in range(len(row_ys) - 1)
    ]

    # Widen the outer boundaries so a value printed slightly outside the ruled
    # box is still captured. Four-digit amounts ("1517.41") overflow their
    # column on a tight scan and lost their trailing characters, which turned
    # 1517.41 into 151 and broke the balance check on those rows.
    left = col_xs[0] - MIN_COLUMN_WIDTH
    right = col_xs[-1] + MIN_COLUMN_WIDTH
    bounds = [left] + list(col_xs[1:-1]) + [right]

    for token in tokens:
        cx, cy = token.cx, token.cy

        row_index = None
        for index in range(len(row_ys) - 1):
            if row_ys[index] <= cy < row_ys[index + 1]:
                row_index = index
                break
        if row_index is None:
            continue

        col_index = None
        for index in range(len(bounds) - 1):
            if bounds[index] <= cx < bounds[index + 1]:
                col_index = index
                break
        if col_index is None:
            continue

        rows[row_index][col_index].append((token.x0, token.text))

    out: list[list[str]] = []
    for row in rows:
        cells = [
            " ".join(text for _, text in sorted(cell, key=lambda p: p[0]))
            for cell in row
        ]
        if any(cell.strip() for cell in cells):
            out.append(cells)
    return out


__all__ = ["detect_grid", "cells_from_tokens", "MIN_COLUMN_WIDTH"]
