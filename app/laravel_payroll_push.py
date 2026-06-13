"""Insert/update Laravel ``payrolls`` (+ ``salary_arrears``, ``krap9``) from CSV import snapshots."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.models import PayrollImportRun, PayrollImportSnapshot
from app.payroll_format import _filter_value, _krap9_row_from_payroll_kra_json
from app.payroll_integrity import (
    PushIntegrityReport,
    fetch_employee_for_payroll,
    has_table,
    load_payroll_row_state,
    reflect_krap9_table,
    reflect_payrolls_table,
    remove_duplicate_payrolls,
    upsert_payroll_row,
    upsert_salary_arrears,
)
from app.payroll_record_builder import build_payroll_record, fetch_prescribed_rate


def push_snapshots_to_laravel_payrolls(
    session: Session,
    run_id: int,
) -> tuple[int, str, dict[str, Any]]:
    """
    Upsert Laravel payroll rows from snapshots (CSV = source of truth).

    Integrity rules:
    - Skips employees whose payroll for the month is **finalized** (finalize owns
      ``deduction_payments``, ``loan_payments``, ``krap9``, etc.).
    - Upserts companion ``salary_arrears`` without resetting ``paid_amount`` when
      ``salary_arrears_payments`` exist.
    - Removes only **unfinalized** duplicate ``payrolls`` rows per employee + month.
    - ``employee_id`` must exist in ``employees`` (FK on ``payrolls``).

    Returns ``(rows_written, filter_label, integrity_report_dict)``.
    """
    run = session.get(PayrollImportRun, run_id)
    if not run:
        raise ValueError("Run not found")
    if run.status == "failed":
        raise ValueError("Run failed — fix import before pushing to payroll")

    if not has_table(session, "payrolls"):
        raise RuntimeError("Table `payrolls` not found — connect to the Laravel tenant database.")
    if not has_table(session, "employees"):
        raise RuntimeError("Table `employees` not found")

    payrolls_table, col_map = reflect_payrolls_table(session)
    col_names_lower = set(col_map.keys())
    krap9_table = reflect_krap9_table(session)

    flt = _filter_value(run_id)
    payroll_date: date = run.payroll_month
    if isinstance(payroll_date, datetime):
        payroll_date = payroll_date.date()

    snaps = session.scalars(
        select(PayrollImportSnapshot).where(PayrollImportSnapshot.import_run_id == run_id)
    ).all()
    if not snaps:
        raise ValueError("No snapshots — build snapshots first")

    prescribed_rate = fetch_prescribed_rate(session)
    now = datetime.utcnow()
    report = PushIntegrityReport()
    written = 0

    for snap in snaps:
        employee = fetch_employee_for_payroll(session, int(snap.employee_id))
        if not employee:
            report.skipped_missing_employee.append(int(snap.employee_id))
            continue

        state = load_payroll_row_state(session, int(snap.employee_id), payroll_date)
        if state.must_skip:
            report.skipped_finalized.append(int(snap.employee_id))
            continue

        remove_duplicate_payrolls(session, state, report)

        payload = build_payroll_record(
            session,
            snap=snap,
            employee=employee,
            payroll_date=payroll_date,
            filter_label=flt,
            prescribed_rate=prescribed_rate,
            now=now,
            col_names_lower=col_names_lower,
        )

        upsert_payroll_row(
            session,
            payrolls_table=payrolls_table,
            col_map=col_map,
            payload=payload,
            state=state,
            now=now,
        )

        upsert_salary_arrears(
            session,
            employee_id=int(snap.employee_id),
            payroll_date=payroll_date,
            net_pay=float(payload.get("net_pay") or 0),
            filter_label=flt,
            now=now,
            report=report,
        )

        kra_json = str(payload.get("kra") or "")
        if krap9_table is not None and kra_json:
            session.execute(
                text("DELETE FROM krap9 WHERE employee_id = :eid AND for_month = :pd"),
                {"eid": int(snap.employee_id), "pd": payroll_date},
            )
            k9_row = _krap9_row_from_payroll_kra_json(kra_json, created_at=now, krap9_table=krap9_table)
            session.execute(insert(krap9_table).values(**k9_row))

        written += 1

    if written == 0 and (report.skipped_finalized or report.skipped_missing_employee):
        parts: list[str] = []
        if report.skipped_finalized:
            parts.append(f"{len(report.skipped_finalized)} employee(s) skipped (payroll already finalized)")
        if report.skipped_missing_employee:
            parts.append(f"{len(report.skipped_missing_employee)} missing/deleted employee(s)")
        raise ValueError("No payroll rows written — " + "; ".join(parts))

    return written, flt, asdict(report)
