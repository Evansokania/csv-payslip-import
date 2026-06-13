"""CSV parse, column mapping, and snapshot build (CSV = source of truth)."""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Allowance,
    Deduction,
    PayrollImportColumnMap,
    PayrollImportRawRow,
    PayrollImportRun,
    PayrollImportSnapshot,
)
from app.payroll_masters import enrich_deduction_line, enrich_earning_line, load_master_context


from app.header_utils import normalize_header


PAYROLL_NO_KEY = normalize_header("PAYROLL NO")

# Stored on each snapshot's ``computed`` JSON; must normalize to ``PERSONAL RELIEF`` for statutory lookup.
SNAPSHOT_PERSONAL_RELIEF_LABEL = "Personal Relief"
DEFAULT_PERSONAL_RELIEF_KES = 2400.0

# Headers treated as aggregate / statutory display from CSV (not matched to allowance/deduction masters)
DEFAULT_COMPUTED = {
    normalize_header(x)
    for x in (
        "GROSS AMOUNT",
        "GROSS PAY",
        "NETTPAY",
        "NET PAY",
        "NETPAY",
        "NET SALARY",
        "NET",
        "PAYE DUE",
        "NSSF 1",
        "NSSF 2",
        "TOTAL DEDUCTION",
        "TOTAL DEDUCTIONS",
        "A THIRD RULE",
        "THIRD RULE",
        "NSSF",
        "NSSF GROSS",
        "PENSIONABLE PAY",
        "PENSIONABLE",
        "PENSIONABLE INCOME",
        "TAXABLE PAY",
        "NSSF TIER 1",
        "NSSF TIER 2",
        "NSSF T1",
        "NSSF T2",
        "TIER 1 NSSF",
        "TIER 2 NSSF",
        "NHIF",
        "SHIF",
        "PAYE",
        "TAX CHARGED",
        "GROSS TAX",
        "PERSONAL RELIEF",
        "INSURANCE RELIEF",
        "TOTAL RELIEF",
        "AFFORDABLE HOUSING LEVY",
        "AHL",
        "HOUSING LEVY",
        "AHL RELIEF",
        "SHIF RELIEF",
        "NHDFLEVYEMPLOYEE",
    )
}

DEFAULT_DIMENSION = {
    normalize_header(x)
    for x in (
        "SRNO",
        "BRANCHNAME",
        "CATEGORYNAME",
        "DEPARTMENTNAME",
        "DESIGNATIONNAME",
        "PAYROLL NO",
        "EMPLOYEE NAME",
    )
}


def payroll_month_last_day(year_month: str) -> date:
    """Accept 'YYYY-MM' -> last calendar day of that month."""
    parts = year_month.strip().split("-")
    if len(parts) != 2:
        raise ValueError("payroll_month must be YYYY-MM")
    y, m = int(parts[0]), int(parts[1])
    last = calendar.monthrange(y, m)[1]
    return date(y, m, last)


def cell_decimal(value: Any) -> float:
    """Parse a spreadsheet cell to float; non-numeric / decorated text -> 0.0 (no crash)."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    s = s.replace(",", "").replace(" ", "")
    try:
        return float(Decimal(s))
    except InvalidOperation:
        # e.g. "KES 1,234.00", "(100)", "—", "N/A", "1 234.56"
        m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
        if not m:
            return 0.0
        try:
            return float(Decimal(m.group(0)))
        except InvalidOperation:
            return 0.0


def _merge_money_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum amounts when the same label / allowance / deduction appears across multiple CSV rows."""
    merged: dict[tuple[str, int | None, int | None], dict[str, Any]] = {}
    for line in lines:
        label = str(line.get("label", ""))
        aid = line.get("allowance_id")
        did = line.get("deduction_id")
        a = int(aid) if aid is not None else None
        d = int(did) if did is not None else None
        key = (label, a, d)
        amt = float(line.get("amount", 0) or 0)
        if key not in merged:
            merged[key] = {**line, "amount": amt}
        else:
            merged[key]["amount"] = float(merged[key]["amount"]) + amt
    return list(merged.values())


def ingest_csv(
    session: Session,
    *,
    payroll_month: date,
    file_content: bytes,
    original_filename: str,
) -> PayrollImportRun:
    """Create run, parse CSV into raw_rows, validate employees exist."""
    sha = hashlib.sha256(file_content).hexdigest()
    text = file_content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    run = PayrollImportRun(
        payroll_month=payroll_month,
        status="parsing",
        csv_sha256=sha,
        original_filename=original_filename[:500],
        row_count=0,
        error_json=None,
    )
    session.add(run)
    session.flush()

    rows: list[dict[str, Any]] = []
    payroll_numbers: list[str] = []
    for i, row in enumerate(reader, start=1):
        payload: dict[str, Any] = {}
        for k, v in row.items():
            if k is None:
                continue
            nk = normalize_header(k)
            if isinstance(v, str):
                payload[nk] = v.strip()
            else:
                payload[nk] = v
        rows.append({"row_no": i, "payload": payload})
        pn = str(payload.get(PAYROLL_NO_KEY, "")).strip()
        if pn:
            payroll_numbers.append(pn)

    unique_pn = sorted(set(payroll_numbers))
    from collections import Counter

    pn_counts = Counter(payroll_numbers)
    duplicate_payroll_numbers = sorted([p for p, c in pn_counts.items() if c > 1])
    missing = _find_missing_payroll_numbers(session, unique_pn)
    if missing:
        run.status = "failed"
        run.error_json = {"missing_payroll_numbers": missing}
        run.row_count = 0
        session.flush()
        return run

    for item in rows:
        session.add(
            PayrollImportRawRow(
                import_run_id=run.id,
                row_no=item["row_no"],
                payload=item["payload"],
            )
        )
    run.row_count = len(rows)
    run.status = "parsed"
    if duplicate_payroll_numbers:
        run.error_json = {
            "import_warnings": {
                "duplicate_payroll_numbers_merged_into_one_snapshot": duplicate_payroll_numbers,
                "message": "Multiple CSV rows for the same payroll number were merged for this import run.",
            }
        }
    session.flush()
    rebuild_column_maps(session, run.id)
    return run


def replace_run_csv(
    session: Session,
    *,
    run_id: int,
    file_content: bytes,
    original_filename: str,
) -> PayrollImportRun:
    """
    Replace the CSV stored on an existing run (same payroll month).

    Validates the new file first; on success, deletes raw rows and snapshots, re-imports,
    and rebuilds the column map. Caller should rebuild snapshots and re-push to Laravel if needed.
    """
    run = session.get(PayrollImportRun, run_id)
    if not run:
        raise ValueError("Run not found")

    sha = hashlib.sha256(file_content).hexdigest()
    raw_text = file_content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw_text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    rows: list[dict[str, Any]] = []
    payroll_numbers: list[str] = []
    for i, row in enumerate(reader, start=1):
        payload: dict[str, Any] = {}
        for k, v in row.items():
            if k is None:
                continue
            nk = normalize_header(k)
            if isinstance(v, str):
                payload[nk] = v.strip()
            else:
                payload[nk] = v
        rows.append({"row_no": i, "payload": payload})
        pn = str(payload.get(PAYROLL_NO_KEY, "")).strip()
        if pn:
            payroll_numbers.append(pn)

    unique_pn = sorted(set(payroll_numbers))
    from collections import Counter

    pn_counts = Counter(payroll_numbers)
    duplicate_payroll_numbers = sorted([p for p, c in pn_counts.items() if c > 1])
    missing = _find_missing_payroll_numbers(session, unique_pn)
    if missing:
        raise ValueError(
            "New CSV is not applied — payroll numbers missing in employees: "
            + ", ".join(missing[:50])
            + (" …" if len(missing) > 50 else "")
        )

    session.execute(delete(PayrollImportRawRow).where(PayrollImportRawRow.import_run_id == run_id))
    session.execute(delete(PayrollImportSnapshot).where(PayrollImportSnapshot.import_run_id == run_id))

    run.csv_sha256 = sha
    run.original_filename = original_filename[:500]
    run.error_json = None
    for item in rows:
        session.add(
            PayrollImportRawRow(
                import_run_id=run.id,
                row_no=item["row_no"],
                payload=item["payload"],
            )
        )
    run.row_count = len(rows)
    run.status = "parsed"
    if duplicate_payroll_numbers:
        run.error_json = {
            "import_warnings": {
                "duplicate_payroll_numbers_merged_into_one_snapshot": duplicate_payroll_numbers,
                "message": "Multiple CSV rows for the same payroll number were merged for this import run.",
            }
        }
    session.flush()
    rebuild_column_maps(session, run.id)
    return run


def _find_missing_payroll_numbers(session: Session, payroll_numbers: list[str]) -> list[str]:
    if not payroll_numbers:
        return ["(no payroll numbers in CSV)"]
    from sqlalchemy import bindparam, text

    stmt = text(
        "SELECT payroll_number FROM employees WHERE payroll_number IN :pns AND deleted_at IS NULL"
    ).bindparams(bindparam("pns", expanding=True))
    found = {r[0] for r in session.execute(stmt, {"pns": payroll_numbers}).fetchall()}
    return [p for p in payroll_numbers if p not in found]


def rebuild_column_maps(session: Session, run_id: int) -> int:
    """Replace column maps with auto-suggestions."""
    session.execute(delete(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id))

    keys: set[str] = set()
    for (payload,) in session.execute(
        select(PayrollImportRawRow.payload).where(PayrollImportRawRow.import_run_id == run_id)
    ):
        keys.update(payload.keys())

    allowances = {normalize_header(a.name): a.id for a in session.scalars(select(Allowance))}
    deductions = {normalize_header(d.name): d.id for d in session.scalars(select(Deduction))}

    # raw header display: take from first row occurrence
    raw_label: dict[str, str] = {}
    for (payload,) in session.execute(
        select(PayrollImportRawRow.payload).where(PayrollImportRawRow.import_run_id == run_id).limit(1)
    ):
        # DictReader keys were normalized; use normalized as display unless we stored raw - we only have normalized keys
        for k in payload:
            raw_label[k] = k.replace(" ", " ").title()

    count = 0
    for norm in sorted(keys):
        role, match_type, aid, did, conf = _classify_header(norm, allowances, deductions)
        session.add(
            PayrollImportColumnMap(
                import_run_id=run_id,
                csv_header_raw=raw_label.get(norm, norm),
                csv_header_normalized=norm,
                role=role,
                match_type=match_type,
                allowance_id=aid,
                deduction_id=did,
                display_label_override=None,
                loan_match_rule=None,
                confidence=conf,
            )
        )
        count += 1
    session.flush()
    return count


def _classify_header(
    norm: str,
    allowances: dict[str, int],
    deductions: dict[str, int],
) -> tuple[str, str, int | None, int | None, float | None]:
    if norm in DEFAULT_DIMENSION:
        return "dimension", "fixed", None, None, 1.0
    if norm in DEFAULT_COMPUTED:
        return "computed", "fixed", None, None, 1.0
    if norm in ("NETPAY", "NET") or norm.startswith("NET PAY") or norm.startswith("NET SALARY"):
        return "computed", "heuristic", None, None, 0.95
    if norm in ("NSSF 1", "NSSF 2") or "NSSF TIER" in norm:
        return "computed", "heuristic", None, None, 0.95
    if norm in ("PAYE DUE",):
        return "computed", "heuristic", None, None, 0.9
    if "PENSION" in norm and "RELIEF" not in norm:
        return "deduction", "heuristic", None, None, 0.85
    if norm in allowances:
        return "earning", "auto_allowance", allowances[norm], None, 1.0
    if norm in deductions:
        return "deduction", "auto_deduction", None, deductions[norm], 1.0
    # Heuristic for loan-like / deduction-like labels
    blob = norm
    if any(x in blob for x in ("DEDUCTION", "LOAN", "SACCO", "RECOVERY", "HELB", "ADVANCE", "IMREST", "IMPREST")):
        return "deduction", "unresolved", None, None, 0.4
    return "earning", "unresolved", None, None, 0.4


def _opt_positive_int(value: Any) -> int | None:
    """Parse form / JSON allowance or deduction id; invalid or empty -> None."""
    if value is None or value == "":
        return None
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if n > 0 else None


def apply_column_map_overrides(
    session: Session,
    run_id: int,
    overrides: dict[str, dict[str, Any]],
) -> None:
    """
    overrides: normalized_header -> {role, allowance_id, deduction_id, display_label_override}
    """
    maps = session.scalars(
        select(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id)
    ).all()
    by_norm = {m.csv_header_normalized: m for m in maps}
    for norm, data in overrides.items():
        m = by_norm.get(normalize_header(norm))
        if not m:
            continue
        if "role" in data:
            m.role = str(data["role"])
        if "allowance_id" in data:
            m.allowance_id = _opt_positive_int(data["allowance_id"])
        if "deduction_id" in data:
            m.deduction_id = _opt_positive_int(data["deduction_id"])
        if "display_label_override" in data and data["display_label_override"]:
            m.display_label_override = str(data["display_label_override"])[:500]
        m.match_type = "manual"
    session.flush()


def build_snapshots(
    session: Session,
    run_id: int,
    *,
    personal_relief_kes: float = DEFAULT_PERSONAL_RELIEF_KES,
) -> int:
    """
    Build one ``PayrollImportSnapshot`` per employee from raw rows and column maps.

    ``personal_relief_kes`` is written into each snapshot's ``computed`` under
    ``Personal Relief`` so P9 / PAYE / Laravel ``krap9`` see relief even when the CSV
    has no relief column. Default matches Kenya monthly personal relief (2400).
    """
    session.execute(delete(PayrollImportSnapshot).where(PayrollImportSnapshot.import_run_id == run_id))

    maps = session.scalars(
        select(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id)
    ).all()
    by_norm = {m.csv_header_normalized: m for m in maps}

    raw_rows = session.scalars(
        select(PayrollImportRawRow)
        .where(PayrollImportRawRow.import_run_id == run_id)
        .order_by(PayrollImportRawRow.row_no)
    ).all()

    pay_nums = sorted(
        {str(rr.payload.get(PAYROLL_NO_KEY, "")).strip() for rr in raw_rows if rr.payload.get(PAYROLL_NO_KEY)}
    )
    by_pn: dict[str, int] = {}
    if pay_nums:
        from sqlalchemy import bindparam, text

        stmt = text(
            "SELECT id, payroll_number FROM employees WHERE payroll_number IN :pns AND deleted_at IS NULL"
        ).bindparams(bindparam("pns", expanding=True))
        for rid, pn in session.execute(stmt, {"pns": pay_nums}).fetchall():
            by_pn[str(pn).strip()] = int(rid)

    # One snapshot per employee per run (unique import_run_id + employee_id). Multiple CSV
    # rows may reference the same payroll number — merge lines and numeric computed fields.
    masters = load_master_context(session)
    by_eid: dict[int, dict[str, Any]] = {}
    for rr in raw_rows:
        payload = dict(rr.payload)
        pn = str(payload.get(PAYROLL_NO_KEY, "")).strip()
        eid = by_pn.get(pn)
        if not eid:
            continue

        earnings: list[dict[str, Any]] = []
        deductions: list[dict[str, Any]] = []
        computed: dict[str, Any] = {}
        dimensions: dict[str, Any] = {}

        for norm, cmap in by_norm.items():
            raw_val = payload.get(norm, "")
            amount = cell_decimal(raw_val)
            label = cmap.display_label_override or cmap.csv_header_raw
            if cmap.role == "dimension":
                dimensions[label] = raw_val
            elif cmap.role == "computed":
                computed[label] = amount
            elif cmap.role == "earning":
                if amount != 0:
                    line = enrich_earning_line(
                        {"label": label, "amount": amount, "allowance_id": cmap.allowance_id},
                        masters,
                    )
                    earnings.append(line)
            elif cmap.role == "deduction":
                if amount != 0:
                    line = enrich_deduction_line(
                        {"label": label, "amount": amount, "deduction_id": cmap.deduction_id},
                        masters,
                    )
                    deductions.append(line)
            # ignore

        if eid not in by_eid:
            by_eid[eid] = {
                "earnings": [],
                "deductions": [],
                "computed": {},
                "dimensions": {},
            }
        bucket = by_eid[eid]
        bucket["earnings"].extend(earnings)
        bucket["deductions"].extend(deductions)
        for k, v in computed.items():
            bucket["computed"][k] = float(bucket["computed"].get(k, 0)) + float(v)
        bucket["dimensions"].update(dimensions)

    n = 0
    for eid, bucket in by_eid.items():
        earnings = [enrich_earning_line(x, masters) for x in _merge_money_lines(bucket["earnings"])]
        deductions = [enrich_deduction_line(x, masters) for x in _merge_money_lines(bucket["deductions"])]
        comp = dict(bucket["computed"])
        comp[SNAPSHOT_PERSONAL_RELIEF_LABEL] = float(personal_relief_kes)
        norm_map = {normalize_header(k): float(v or 0) for k, v in comp.items()}
        tr = float(norm_map.get(normalize_header("TOTAL RELIEF"), 0))
        if tr <= 0 and float(personal_relief_kes) > 0:
            comp["Total Relief"] = float(personal_relief_kes)
        dim = bucket["dimensions"]
        session.add(
            PayrollImportSnapshot(
                import_run_id=run_id,
                employee_id=eid,
                earnings_lines=earnings,
                deduction_lines=deductions,
                computed=comp or None,
                dimensions=dim or None,
            )
        )
        n += 1

    run = session.get(PayrollImportRun, run_id)
    if run:
        run.status = "snapshotted"
    session.flush()
    return n
