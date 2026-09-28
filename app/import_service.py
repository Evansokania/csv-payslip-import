"""CSV parse, column mapping, and snapshot build (CSV = source of truth)."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models import (
    PayrollImportColumnMap,
    PayrollImportRawRow,
    PayrollImportRun,
    PayrollImportSnapshot,
)
from app.payroll_masters import enrich_deduction_line, enrich_earning_line, load_master_context


from app.classification import load_classification_sections
from app.header_utils import normalize_header
from app.master_match import MasterIndex, load_master_index, match_deduction, match_earning
from app.tabular_parse import parse_table


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


def _stage_rows(parsed_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Number the parsed rows and collect their payroll numbers."""
    rows: list[dict[str, Any]] = []
    payroll_numbers: list[str] = []
    for i, payload in enumerate(parsed_rows, start=1):
        rows.append({"row_no": i, "payload": payload})
        pn = str(payload.get(PAYROLL_NO_KEY, "")).strip()
        if pn:
            payroll_numbers.append(pn)
    return rows, payroll_numbers


def _build_import_warnings(missing: list[str], duplicates: list[str]) -> dict[str, Any] | None:
    warnings: dict[str, Any] = {}
    if missing:
        warnings["missing_payroll_numbers"] = {
            "count": len(missing),
            "sample": missing[:50],
            "message": (
                f"{len(missing)} payroll number(s) not found in employees — "
                "those rows are skipped when building snapshots / pushing payroll."
            ),
        }
    if duplicates:
        warnings["duplicate_payroll_numbers_merged_into_one_snapshot"] = duplicates
    return {"import_warnings": warnings} if warnings else None


def _overtime_rate_bucket(label: str) -> int:
    """Classify an overtime line as tier 1 (1.5x) or tier 2 (2x); 0 = unknown."""
    n = normalize_header(label)
    if any(t in n for t in ("2.0", "2.5", "@ 2", "X 2", "OT 2", "OT2", "DOUBLE")):
        return 2
    if any(t in n for t in ("1.5", "@ 1.5", "OT 1", "OT1", "HALF", "TIME AND A")):
        return 1
    return 0


def _merge_by_master(
    lines: list[dict[str, Any]],
    masters: Any,
    id_key: str,
) -> list[dict[str, Any]]:
    """Collapse earning/deduction lines that link to the *same* master into one entry.

    Native allowance/deduction reports list one row per master and match by name, so
    split columns (e.g. Overtime @1.5 + Overtime @2.0 -> one ``Overtime`` master) must
    become a single entry named after the master, or the report under-counts. A
    ``detailed`` breakdown is preserved; overtime also carries ``OT1``/``OT2``.
    """
    passthrough: list[dict[str, Any]] = []
    groups: dict[int, list[dict[str, Any]]] = {}
    for ln in lines:
        mid = ln.get(id_key)
        if mid is None:
            passthrough.append(ln)
        else:
            groups.setdefault(int(mid), []).append(ln)

    is_allow = id_key == "allowance_id"
    by_id = masters.allowances_by_id if is_allow else masters.deductions_by_id

    merged: list[dict[str, Any]] = []
    for mid, grp in groups.items():
        meta = by_id.get(mid)
        name = meta.name if meta else (grp[0].get("name") or grp[0].get("label") or "")
        labels = [str(x.get("label") or x.get("name") or "") for x in grp]
        total = round(sum(float(x.get("amount") or 0) for x in grp), 2)
        # Normalize every linked line to the master name so reports match by name.
        base = dict(grp[0])
        base["name"] = name
        base["label"] = name
        base["amount"] = total
        if len(grp) > 1:
            base["detailed"] = json.dumps(
                [{"name": lbl, "amount": round(float(x.get("amount") or 0), 2)} for lbl, x in zip(labels, grp)]
            )
        if is_allow:
            base["tax_amount"] = total if base.get("taxable", 1) else 0.0
            if "OVERTIME" in normalize_header(name) or any("OVERTIME" in normalize_header(x) for x in labels):
                base["OT1"] = round(
                    sum(float(x.get("amount") or 0) for x, lbl in zip(grp, labels) if _overtime_rate_bucket(lbl) == 1), 2
                )
                base["OT2"] = round(
                    sum(float(x.get("amount") or 0) for x, lbl in zip(grp, labels) if _overtime_rate_bucket(lbl) == 2), 2
                )
        merged.append(base)
    return passthrough + merged


def ingest_csv(
    session: Session,
    *,
    payroll_month: date,
    file_content: bytes,
    original_filename: str,
) -> PayrollImportRun:
    """Create run, parse CSV/XLSX into raw_rows, validate employees exist.

    Missing payroll numbers no longer fail the whole run: matched employees are
    imported and the unmatched ones are recorded as a warning. The run only fails
    when *no* payroll number matches an employee (nothing usable to import).
    """
    sha = hashlib.sha256(file_content).hexdigest()
    parsed = parse_table(file_content, original_filename)
    if not parsed.rows:
        raise ValueError("No data rows found in file")

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

    rows, payroll_numbers = _stage_rows(parsed.rows)
    unique_pn = sorted(set(payroll_numbers))
    pn_counts = Counter(payroll_numbers)
    duplicate_payroll_numbers = sorted([p for p, c in pn_counts.items() if c > 1])
    missing = _find_missing_payroll_numbers(session, unique_pn)

    if not unique_pn or len(missing) >= len(unique_pn):
        run.status = "failed"
        run.error_json = {"missing_payroll_numbers": missing}
        run.row_count = 0
        session.flush()
        return run

    if rows:
        session.execute(
            insert(PayrollImportRawRow),
            [{"import_run_id": run.id, "row_no": item["row_no"], "payload": item["payload"]} for item in rows],
        )
    run.row_count = len(rows)
    run.status = "parsed"
    run.error_json = _build_import_warnings(missing, duplicate_payroll_numbers)
    session.flush()
    rebuild_column_maps(session, run.id, raw_labels=parsed.raw_headers, sections=parsed.sections)
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
    parsed = parse_table(file_content, original_filename)
    if not parsed.rows:
        raise ValueError("No data rows found in file")

    rows, payroll_numbers = _stage_rows(parsed.rows)
    unique_pn = sorted(set(payroll_numbers))
    pn_counts = Counter(payroll_numbers)
    duplicate_payroll_numbers = sorted([p for p, c in pn_counts.items() if c > 1])
    missing = _find_missing_payroll_numbers(session, unique_pn)
    if not unique_pn or len(missing) >= len(unique_pn):
        raise ValueError(
            "New file is not applied — no payroll number matches an employee. Missing: "
            + ", ".join(missing[:50])
            + (" …" if len(missing) > 50 else "")
        )

    session.execute(delete(PayrollImportRawRow).where(PayrollImportRawRow.import_run_id == run_id))
    session.execute(delete(PayrollImportSnapshot).where(PayrollImportSnapshot.import_run_id == run_id))

    run.csv_sha256 = sha
    run.original_filename = original_filename[:500]
    if rows:
        session.execute(
            insert(PayrollImportRawRow),
            [{"import_run_id": run.id, "row_no": item["row_no"], "payload": item["payload"]} for item in rows],
        )
    run.row_count = len(rows)
    run.status = "parsed"
    run.error_json = _build_import_warnings(missing, duplicate_payroll_numbers)
    session.flush()
    rebuild_column_maps(session, run.id, raw_labels=parsed.raw_headers, sections=parsed.sections)
    return run


def _find_missing_payroll_numbers(session: Session, payroll_numbers: list[str]) -> list[str]:
    if not payroll_numbers:
        return ["(no payroll numbers in CSV)"]
    from sqlalchemy import bindparam, text

    # Soft-deleted employees count as found. Someone separated after the payroll month
    # was earned still has to be imported: their pay is part of the month's cost and
    # their P9 / statutory figures are filed for the year, not for who is still on staff.
    # Laravel reads them back the same way — Payroll::employee() is withTrashed().
    stmt = text(
        "SELECT payroll_number FROM employees WHERE payroll_number IN :pns"
    ).bindparams(bindparam("pns", expanding=True))
    found = {r[0] for r in session.execute(stmt, {"pns": payroll_numbers}).fetchall()}
    return [p for p in payroll_numbers if p not in found]


def rebuild_column_maps(
    session: Session,
    run_id: int,
    raw_labels: dict[str, str] | None = None,
    sections: dict[str, str] | None = None,
) -> int:
    """Replace column maps with auto-suggestions.

    ``raw_labels`` maps a canonical header to its original display label; ``sections``
    maps a canonical header to a banner section hint ("earning" / "statutory" /
    "other_deduction" / "memo"). Both are supplied by the parser on import. On a
    standalone rebuild they default to the labels already stored on existing maps.
    """
    # Preserve existing display labels (and reuse them) across a manual rebuild.
    existing_labels: dict[str, str] = {
        m.csv_header_normalized: m.csv_header_raw
        for m in session.scalars(
            select(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id)
        )
    }
    session.execute(delete(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id))

    keys: set[str] = set()
    for (payload,) in session.execute(
        select(PayrollImportRawRow.payload).where(PayrollImportRawRow.import_run_id == run_id)
    ):
        keys.update(payload.keys())

    master_idx = load_master_index(session)
    classification = load_classification_sections(session)

    raw_label = dict(existing_labels)
    if raw_labels:
        raw_label.update(raw_labels)
    sect = dict(sections or {})

    # Rename voluntary/pension lines to the native payroll names so payslips, the
    # payrolls.deductions column and P9 reports read consistently.
    _native_names = {"voluntary_nssf": "Voluntary NSSF", "pension": "Retirement Contribution"}

    count = 0
    unclassified_money: list[str] = []
    for norm in sorted(keys):
        role, match_type, aid, did, conf = _classify_header(
            norm, master_idx, sect.get(norm, ""), classification.get(norm, "")
        )
        # Flag only money columns whose role was *guessed* (banner section / keyword),
        # not those routed by the sheet, a master link, or an explicit statutory rule.
        if (
            classification
            and role in ("earning", "deduction")
            and match_type in ("section", "unresolved")
            and norm not in classification
        ):
            unclassified_money.append(raw_label.get(norm) or norm)
        session.add(
            PayrollImportColumnMap(
                import_run_id=run_id,
                csv_header_raw=raw_label.get(norm) or norm.title(),
                csv_header_normalized=norm,
                role=role,
                match_type=match_type,
                allowance_id=aid,
                deduction_id=did,
                display_label_override=_native_names.get(match_type),
                loan_match_rule=None,
                confidence=conf,
            )
        )
        count += 1

    # Record columns the classification sheet doesn't list (guessed as money) so the
    # operator can add them to the sheet rather than have them silently included.
    if unclassified_money:
        run = session.get(PayrollImportRun, run_id)
        if run is not None:
            ej = dict(run.error_json or {})
            warnings = dict(ej.get("import_warnings") or {})
            warnings["unclassified_columns"] = {
                "count": len(unclassified_money),
                "columns": sorted(set(unclassified_money))[:50],
                "message": "These columns are not in the classification sheet; roles were guessed.",
            }
            ej["import_warnings"] = warnings
            run.error_json = ej

    session.flush()
    return count


_DEDUCTION_KEYWORDS = ("DEDUCTION", "LOAN", "SACCO", "RECOVERY", "HELB", "ADVANCE", "IMREST", "IMPREST")
# Unlisted check / variance columns (e.g. "Variance (chk)") are informational, not money.
_INFORMATIONAL_HINTS = ("VARIANCE", "CHK", "CHECK")


def _classify_header(
    norm: str,
    master_idx: MasterIndex,
    section: str = "",
    classification_section: str = "",
) -> tuple[str, str, int | None, int | None, float | None]:
    if norm in DEFAULT_DIMENSION:
        return "dimension", "fixed", None, None, 1.0
    if norm in DEFAULT_COMPUTED:
        return "computed", "fixed", None, None, 1.0
    if norm in ("NETPAY", "NET") or norm.startswith("NET PAY") or norm.startswith("NET SALARY"):
        return "computed", "heuristic", None, None, 0.95
    if norm in ("NSSF 1", "NSSF 2") or "NSSF TIER" in norm:
        return "computed", "heuristic", None, None, 0.95
    # Voluntary NSSF and pension/retirement are deductions handled like the native
    # payroll (own voluntary_nssf column / pension relief on P9).
    if norm == "VOLUNTARY NSSF":
        return "deduction", "voluntary_nssf", None, None, 1.0
    if norm == "RETIREMENT CONTRIBUTION":
        return "deduction", "pension", None, None, 1.0
    if norm in ("PAYE DUE",):
        return "computed", "heuristic", None, None, 0.9
    # Sub-totals / check columns (e.g. TOTAL STATUTORY, TOTAL OTHER DED.) are informational —
    # keep them out of earnings/deductions so they don't double-count.
    if norm.startswith("TOTAL"):
        return "dimension", "informational", None, None, 0.6
    if "PENSION" in norm and "RELIEF" not in norm:
        # Link to a deduction master when one exists, else stay a heuristic pension line.
        did = match_deduction(norm, master_idx)
        return "deduction", "auto_deduction" if did else "heuristic", None, did, 1.0 if did else 0.85

    # Authoritative: the tenant classification sheet decides the role for listed columns.
    cs = classification_section
    if cs == "EARNING":
        aid = match_earning(norm, master_idx)
        return "earning", "classification_allowance" if aid else "classification", aid, None, 1.0 if aid else 0.9
    if cs == "OTHER DEDUCTION":
        did = match_deduction(norm, master_idx)
        return "deduction", "classification_deduction" if did else "classification", None, did, 1.0 if did else 0.9
    if cs == "STATUTORY DEDUCTION":
        return "computed", "classification", None, None, 0.9
    if cs in ("MEMO", "CALCULATED"):
        return "dimension", "classification_informational", None, None, 0.9

    # Unlisted variance / check columns are informational, never money.
    if any(h in norm for h in _INFORMATIONAL_HINTS):
        return "dimension", "informational", None, None, 0.6

    # Link special/fixed earning & deduction columns to existing (non-statutory) masters.
    # Try the type the column leans toward first so a shared word can't cross-link.
    lean_deduction = section in ("statutory", "other_deduction") or any(k in norm for k in _DEDUCTION_KEYWORDS)
    if lean_deduction and section != "earning":
        did = match_deduction(norm, master_idx)
        if did is not None:
            return "deduction", "auto_deduction", None, did, 1.0
        aid = match_earning(norm, master_idx)
        if aid is not None:
            return "earning", "auto_allowance", aid, None, 1.0
    else:
        aid = match_earning(norm, master_idx)
        if aid is not None:
            return "earning", "auto_allowance", aid, None, 1.0
        did = match_deduction(norm, master_idx)
        if did is not None:
            return "deduction", "auto_deduction", None, did, 1.0

    # Banner section from the spreadsheet (EARNINGS / STATUTORY / OTHER DEDUCTIONS / MEMO).
    if section == "memo":
        return "dimension", "section", None, None, 0.6
    if section in ("statutory", "other_deduction"):
        return "deduction", "section", None, None, 0.6
    if section == "earning":
        return "earning", "section", None, None, 0.6
    # Heuristic for loan-like / deduction-like labels
    if any(x in norm for x in _DEDUCTION_KEYWORDS):
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

        # Separated employees are snapshotted too — see _find_missing_payroll_numbers.
        # A rehire can leave one closed and one open row on the same payroll number, so
        # order the live row last and let it win the assignment.
        stmt = text(
            "SELECT id, payroll_number FROM employees WHERE payroll_number IN :pns "
            "ORDER BY (deleted_at IS NULL), id"
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

    snap_rows: list[dict[str, Any]] = []
    for eid, bucket in by_eid.items():
        earnings = [enrich_earning_line(x, masters) for x in _merge_money_lines(bucket["earnings"])]
        deductions = [enrich_deduction_line(x, masters) for x in _merge_money_lines(bucket["deductions"])]
        # One entry per master so native allowance/deduction reports (which list one row
        # per master and keep the last name-match) count split columns correctly.
        earnings = _merge_by_master(earnings, masters, "allowance_id")
        deductions = _merge_by_master(deductions, masters, "deduction_id")
        comp = dict(bucket["computed"])
        comp[SNAPSHOT_PERSONAL_RELIEF_LABEL] = float(personal_relief_kes)
        norm_map = {normalize_header(k): float(v or 0) for k, v in comp.items()}
        tr = float(norm_map.get(normalize_header("TOTAL RELIEF"), 0))
        if tr <= 0 and float(personal_relief_kes) > 0:
            comp["Total Relief"] = float(personal_relief_kes)
        dim = bucket["dimensions"]
        snap_rows.append(
            {
                "import_run_id": run_id,
                "employee_id": eid,
                "earnings_lines": earnings,
                "deduction_lines": deductions,
                "computed": comp or None,
                "dimensions": dim or None,
            }
        )
    if snap_rows:
        session.execute(insert(PayrollImportSnapshot), snap_rows)
    n = len(snap_rows)

    run = session.get(PayrollImportRun, run_id)
    if run:
        run.status = "snapshotted"
    session.flush()
    return n
