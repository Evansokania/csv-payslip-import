"""Unified CSV / XLSX parser for payroll imports.

Both formats resolve to the same shape the rest of the pipeline already expects:
a list of row dicts keyed by *canonical* header, plus a display-label map and
optional section hints (from a banner row above the real header, e.g.
``EARNINGS`` / ``STATUTORY DEDUCTIONS`` / ``OTHER DEDUCTIONS`` / ``MEMO``).

Real-world spreadsheet exports differ from clean CSV in three ways this handles:
  * binary ``.xlsx`` container (not UTF-8 text),
  * a merged category banner row *above* the real header row, and
  * a trailing ``TOTAL`` / summary row that is not an employee.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from app.header_utils import canonical_header, normalize_header

XLSX_MAGIC = b"PK\x03\x04"
PAYROLL_NO_KEY = canonical_header("PAYROLL NO")

# A row whose payroll-number cell is one of these is a summary/total line, not an employee.
_SUMMARY_TOKENS = {"", "TOTAL", "TOTALS", "GRAND TOTAL", "SUB TOTAL", "SUBTOTAL"}

# How many leading rows to scan when locating the header row in a spreadsheet.
_HEADER_SCAN_LIMIT = 20


@dataclass
class ParsedTable:
    rows: list[dict[str, Any]] = field(default_factory=list)
    # canonical header -> original display label (first occurrence)
    raw_headers: dict[str, str] = field(default_factory=dict)
    # canonical header -> section hint ("earning" | "statutory" | "other_deduction" | "memo" | "")
    sections: dict[str, str] = field(default_factory=dict)


def parse_table(file_content: bytes, original_filename: str) -> ParsedTable:
    """Parse CSV or XLSX bytes into a :class:`ParsedTable`. Raises ``ValueError`` on bad input."""
    name = (original_filename or "").lower()
    is_xlsx = name.endswith((".xlsx", ".xlsm")) or file_content[:4] == XLSX_MAGIC
    if is_xlsx:
        return _parse_xlsx(file_content)
    return _parse_csv(file_content)


def _clean_cell(v: Any) -> Any:
    """Trim strings; render integral floats without a trailing ``.0`` (keeps IDs clean)."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _is_summary_row(payload: dict[str, Any]) -> bool:
    pn = str(payload.get(PAYROLL_NO_KEY, "") or "").strip().upper()
    return pn in _SUMMARY_TOKENS


def _parse_csv(file_content: bytes) -> ParsedTable:
    text = file_content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    out = ParsedTable()
    for h in reader.fieldnames:
        if h is None:
            continue
        ck = canonical_header(h)
        if ck and ck not in out.raw_headers:
            out.raw_headers[ck] = str(h).strip() or ck

    for row in reader:
        payload: dict[str, Any] = {}
        for k, v in row.items():
            if k is None:
                continue
            payload[canonical_header(k)] = _clean_cell(v)
        if _is_summary_row(payload):
            continue
        out.rows.append(payload)
    return out


def _section_of(banner: Any) -> str:
    b = normalize_header(str(banner) if banner is not None else "")
    if not b:
        return ""
    if "EARNING" in b:
        return "earning"
    if "STATUTORY" in b:
        return "statutory"
    if "DEDUCTION" in b:  # "OTHER DEDUCTIONS"
        return "other_deduction"
    if "MEMO" in b or "INFORMATIONAL" in b or "NON-CASH" in b or "NON CASH" in b:
        return "memo"
    return ""


def _find_header_row(grid: list[list[Any]]) -> int:
    """Header row = first row (within scan limit) containing the payroll-number column;
    else the row with the most non-empty text cells."""
    scan = grid[:_HEADER_SCAN_LIMIT]
    for i, row in enumerate(scan):
        cants = {canonical_header(str(c)) for c in row if c not in (None, "")}
        if PAYROLL_NO_KEY in cants:
            return i
    best_i, best_cnt = 0, -1
    for i, row in enumerate(scan):
        cnt = sum(1 for c in row if isinstance(c, str) and c.strip())
        if cnt > best_cnt:
            best_i, best_cnt = i, cnt
    return best_i


def _parse_xlsx(file_content: bytes) -> ParsedTable:
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover - dependency guard
        raise ValueError(
            "This is an .xlsx file but the 'openpyxl' package is not installed "
            "(pip install openpyxl)."
        ) from e

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_content), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"Could not read spreadsheet: {e}") from e

    try:
        ws = wb.active
        grid: list[list[Any]] = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    if not grid:
        raise ValueError("Spreadsheet has no rows")

    header_idx = _find_header_row(grid)
    header_row = grid[header_idx]

    # Banner row (category sections) sits directly above the header, forward-filled
    # across merged cells.
    section_by_col: dict[int, str] = {}
    if header_idx > 0:
        banner = grid[header_idx - 1]
        current = ""
        for ci in range(len(header_row)):
            cell = banner[ci] if ci < len(banner) else None
            sec = _section_of(cell)
            if sec:
                current = sec
            section_by_col[ci] = current

    out = ParsedTable()
    col_norm: dict[int, str] = {}
    for ci, cell in enumerate(header_row):
        if cell in (None, ""):
            continue
        raw = str(cell).strip()
        if not raw:
            continue
        ck = canonical_header(raw)
        col_norm[ci] = ck
        if ck not in out.raw_headers:
            out.raw_headers[ck] = raw
            sec = section_by_col.get(ci, "")
            if sec:
                out.sections[ck] = sec

    for row in grid[header_idx + 1:]:
        payload: dict[str, Any] = {}
        any_value = False
        for ci, ck in col_norm.items():
            v = _clean_cell(row[ci]) if ci < len(row) else None
            if v not in (None, ""):
                any_value = True
            # Same canonical header across multiple columns: keep the first non-empty value.
            if ck in payload and payload[ck] not in (None, ""):
                continue
            payload[ck] = v
        if not any_value or _is_summary_row(payload):
            continue
        out.rows.append(payload)

    if not out.raw_headers:
        raise ValueError("Could not locate a header row in the spreadsheet")
    return out
