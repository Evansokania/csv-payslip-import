"""Safe read/write helpers for Laravel payroll-related tables (FK and payment integrity)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import MetaData, bindparam, inspect, insert, text, update
from sqlalchemy.orm import Session


@dataclass
class PayrollRowState:
    """Existing ``payrolls`` rows for one employee + month."""

    ids: list[int] = field(default_factory=list)
    any_finalized: bool = False
    keep_id: int | None = None

    @property
    def must_skip(self) -> bool:
        return self.any_finalized


@dataclass
class PushIntegrityReport:
    skipped_finalized: list[int] = field(default_factory=list)
    skipped_missing_employee: list[int] = field(default_factory=list)
    duplicate_payrolls_removed: int = 0
    salary_arrears_updated: int = 0
    salary_arrears_inserted: int = 0
    salary_arrears_skipped_finalized: int = 0
    salary_arrears_skipped_has_payments: int = 0


def has_table(session: Session, name: str) -> bool:
    try:
        return inspect(session.get_bind()).has_table(name)
    except Exception:
        return False


def fetch_employee_for_payroll(session: Session, employee_id: int) -> dict[str, Any] | None:
    """
    Load employee fields needed for payroll insert.

    ``department_id`` mirrors ``Employee::getUserDepartmentIdAttribute()`` (first assignment).
    ``beneficiary_name`` prefers ``employee_payment_methods`` when that table exists.
    """
    if not has_table(session, "employees"):
        return None
    row = session.execute(
        text(
            """
            SELECT id, first_name, last_name, payroll_number, identification_type,
                   identification_number, kra_pin, hr_subdept_id,
                   retirement_contribution, mortgage_relief, relief
            FROM employees
            WHERE id = :eid AND deleted_at IS NULL
            LIMIT 1
            """
        ),
        {"eid": employee_id},
    ).mappings().first()
    if not row:
        return None
    emp = dict(row)
    dept_id: int | None = None
    if has_table(session, "assignments") and has_table(session, "departments"):
        dept = session.execute(
            text(
                """
                SELECT d.id
                FROM assignments a
                INNER JOIN departments d ON d.id = a.department_id
                WHERE a.employee_id = :eid
                ORDER BY a.department_id ASC
                LIMIT 1
                """
            ),
            {"eid": employee_id},
        ).scalar()
        if dept is not None:
            dept_id = int(dept)
    if dept_id is None and emp.get("hr_subdept_id"):
        try:
            dept_id = int(emp["hr_subdept_id"])
        except (TypeError, ValueError):
            dept_id = None
    emp["department_id"] = dept_id

    beneficiary = ""
    if has_table(session, "employee_payment_methods"):
        bn = session.execute(
            text(
                """
                SELECT beneficiary_name
                FROM employee_payment_methods
                WHERE employee_id = :eid AND beneficiary_name IS NOT NULL AND beneficiary_name != ''
                ORDER BY id ASC
                LIMIT 1
                """
            ),
            {"eid": employee_id},
        ).scalar()
        if bn and str(bn).strip():
            beneficiary = str(bn).strip()
    emp["beneficiary_name"] = beneficiary
    emp["retirement_contribution"] = float(emp.get("retirement_contribution") or 0)
    emp["mortgage_relief"] = float(emp.get("mortgage_relief") or 0)
    emp["employee_relief"] = float(emp.get("relief") or 0)
    return emp


def load_payroll_row_state(session: Session, employee_id: int, payroll_date: date) -> PayrollRowState:
    """Inspect existing payroll rows; detect finalized records we must not overwrite."""
    state = PayrollRowState()
    if not has_table(session, "payrolls"):
        return state
    rows = session.execute(
        text(
            """
            SELECT id, finalized
            FROM payrolls
            WHERE employee_id = :eid AND payroll_date = :pd
            ORDER BY id ASC
            """
        ),
        {"eid": employee_id, "pd": payroll_date},
    ).mappings().all()
    for r in rows:
        rid = int(r["id"])
        state.ids.append(rid)
        fin = r.get("finalized")
        if fin in (1, True, "1"):
            state.any_finalized = True
    if state.ids:
        state.keep_id = state.ids[0]
    return state


def remove_duplicate_payrolls(
    session: Session,
    state: PayrollRowState,
    report: PushIntegrityReport,
) -> None:
    """
    Drop extra duplicate ``payrolls`` rows for the same employee + month.

    Only removes unfinalized duplicates; never deletes a finalized row.
    """
    if len(state.ids) <= 1 or state.keep_id is None:
        return
    extras = [i for i in state.ids[1:] if i != state.keep_id]
    if not extras:
        return
    session.execute(
        text("DELETE FROM payrolls WHERE id IN :ids AND (finalized = 0 OR finalized IS NULL)").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": extras},
    )
    report.duplicate_payrolls_removed += len(extras)


def reflect_payrolls_table(session: Session) -> tuple[Any, dict[str, str]]:
    """Return (SQLAlchemy Table, lowercase->actual column name map)."""
    engine = session.get_bind()
    md = MetaData()
    md.reflect(bind=engine, only=["payrolls"])
    table = md.tables.get("payrolls")
    if table is None:
        table = next((t for t in md.tables.values() if t.name.lower() == "payrolls"), None)
    if table is None:
        raise RuntimeError("Could not reflect table `payrolls`.")
    col_map = {c.name.lower(): c.name for c in table.c}
    return table, col_map


def filter_payload_to_table(payload: dict[str, Any], col_map: dict[str, str]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for k, v in payload.items():
        lk = str(k).lower()
        if lk in col_map:
            row[col_map[lk]] = v
    return row


def upsert_payroll_row(
    session: Session,
    *,
    payrolls_table: Any,
    col_map: dict[str, str],
    payload: dict[str, Any],
    state: PayrollRowState,
    now: datetime,
) -> int | None:
    """Insert or update one payroll row. Returns payroll id."""
    row = filter_payload_to_table(payload, col_map)
    if state.keep_id:
        upd = dict(row)
        for drop in ("id", "created_at"):
            k = col_map.get(drop)
            if k and k in upd:
                del upd[k]
        ut = col_map.get("updated_at")
        if ut and ut in payrolls_table.c:
            upd[ut] = now
        session.execute(update(payrolls_table).where(payrolls_table.c.id == state.keep_id).values(**upd))
        return state.keep_id
    session.execute(insert(payrolls_table).values(**row))
    new_id = session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    return int(new_id) if new_id else None


def _salary_arrears_has_payments(session: Session, arrears_id: int) -> bool:
    if not has_table(session, "salary_arrears_payments"):
        return False
    n = session.execute(
        text("SELECT COUNT(*) FROM salary_arrears_payments WHERE salary_arrears_id = :aid"),
        {"aid": arrears_id},
    ).scalar()
    return int(n or 0) > 0


def upsert_salary_arrears(
    session: Session,
    *,
    employee_id: int,
    payroll_date: date,
    net_pay: float,
    filter_label: str,
    now: datetime,
    report: PushIntegrityReport,
) -> None:
    """
    Mirror native payroll generation companion row in ``salary_arrears``.

    Preserves ``paid_amount`` when ``salary_arrears_payments`` exist; skips finalized arrears.
    """
    if not has_table(session, "salary_arrears"):
        return
    existing = session.execute(
        text(
            """
            SELECT id, paid_amount, balance, finalized
            FROM salary_arrears
            WHERE employee_id = :eid AND payroll_date = :pd
            ORDER BY id ASC
            LIMIT 1
            """
        ),
        {"eid": employee_id, "pd": payroll_date},
    ).mappings().first()

    net_r = round(float(net_pay), 2)
    flt = filter_label[:255] if len(filter_label) > 255 else filter_label

    if existing:
        if existing.get("finalized") in (1, True, "1"):
            report.salary_arrears_skipped_finalized += 1
            return
        aid = int(existing["id"])
        paid = float(existing.get("paid_amount") or 0)
        if _salary_arrears_has_payments(session, aid):
            balance = round(max(0.0, net_r - paid), 2)
            session.execute(
                text(
                    """
                    UPDATE salary_arrears
                    SET net_pay = :net, balance = :bal, filter = :flt, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"net": net_r, "bal": balance, "flt": flt, "now": now, "id": aid},
            )
            report.salary_arrears_skipped_has_payments += 1
            report.salary_arrears_updated += 1
            return
        session.execute(
            text(
                """
                UPDATE salary_arrears
                SET net_pay = :net, balance = :bal, paid_amount = 0, filter = :flt, updated_at = :now
                WHERE id = :id
                """
            ),
            {"net": net_r, "bal": net_r, "flt": flt, "now": now, "id": aid},
        )
        report.salary_arrears_updated += 1
        return

    session.execute(
        text(
            """
            INSERT INTO salary_arrears
                (employee_id, payroll_date, net_pay, paid_amount, balance, filter, finalized, created_at, updated_at)
            VALUES
                (:eid, :pd, :net, 0, :bal, :flt, 0, :now, :now)
            """
        ),
        {"eid": employee_id, "pd": payroll_date, "net": net_r, "bal": net_r, "flt": flt, "now": now},
    )
    report.salary_arrears_inserted += 1


def reflect_krap9_table(session: Session) -> Any | None:
    if not has_table(session, "krap9"):
        return None
    engine = session.get_bind()
    md = MetaData()
    md.reflect(bind=engine, only=["krap9"])
    table = md.tables.get("krap9")
    if table is None:
        table = next((t for t in md.tables.values() if t.name.lower() == "krap9"), None)
    return table
