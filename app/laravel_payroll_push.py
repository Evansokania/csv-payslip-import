"""Insert/update Laravel ``payrolls`` (+ ``salary_arrears``, ``krap9``) from CSV import snapshots."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.models import PayrollImportRun, PayrollImportSnapshot
from app.payroll_format import _filter_value, _krap9_row_from_payroll_kra_json
from sqlalchemy import bindparam

from app.payroll_integrity import (
    PayrollRowState,
    PushIntegrityReport,
    fetch_employees_for_payroll,
    has_table,
    load_payroll_states,
    load_salary_arrears_states,
    reflect_krap9_table,
    reflect_payrolls_table,
    remove_duplicate_payrolls,
    salary_arrears_ids_with_payments,
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

    # Batch-load everything the per-row loop needs (one query each) instead of
    # ~7 round-trips per employee — the dominant cost for ~900-row imports.
    emp_ids = [int(s.employee_id) for s in snaps]
    employees = fetch_employees_for_payroll(session, emp_ids)
    states = load_payroll_states(session, emp_ids, payroll_date)
    arrears_states = load_salary_arrears_states(session, emp_ids, payroll_date)
    arrears_with_payments = salary_arrears_ids_with_payments(
        session, [int(a["id"]) for a in arrears_states.values()]
    )

    # Delete krap9 once for the rows we will (re)write — never touch finalized/missing.
    writable_ids = [
        eid for eid in emp_ids
        if employees.get(eid) and not states.get(eid, PayrollRowState()).must_skip
    ]
    if krap9_table is not None and writable_ids:
        session.execute(
            text("DELETE FROM krap9 WHERE for_month = :pd AND employee_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"pd": payroll_date, "ids": writable_ids},
        )

    krap9_rows: list[dict[str, Any]] = []
    for snap in snaps:
        eid = int(snap.employee_id)
        employee = employees.get(eid)
        if not employee:
            report.skipped_missing_employee.append(eid)
            continue

        state = states.get(eid) or PayrollRowState()
        if state.must_skip:
            report.skipped_finalized.append(eid)
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
            return_id=False,
        )

        arr = arrears_states.get(eid)
        upsert_salary_arrears(
            session,
            employee_id=eid,
            payroll_date=payroll_date,
            net_pay=float(payload.get("net_pay") or 0),
            filter_label=flt,
            now=now,
            report=report,
            existing_row=arr,
            has_payments=bool(arr) and int(arr["id"]) in arrears_with_payments,
        )

        kra_json = str(payload.get("kra") or "")
        if krap9_table is not None and kra_json:
            krap9_rows.append(_krap9_row_from_payroll_kra_json(kra_json, created_at=now, krap9_table=krap9_table))

        written += 1

    # One executemany bulk insert for all P9 rows.
    if krap9_table is not None and krap9_rows:
        session.execute(insert(krap9_table), krap9_rows)

    if written == 0 and (report.skipped_finalized or report.skipped_missing_employee):
        parts: list[str] = []
        if report.skipped_finalized:
            parts.append(f"{len(report.skipped_finalized)} employee(s) skipped (payroll already finalized)")
        if report.skipped_missing_employee:
            parts.append(f"{len(report.skipped_missing_employee)} missing/deleted employee(s)")
        raise ValueError("No payroll rows written — " + "; ".join(parts))

    return written, flt, asdict(report)
