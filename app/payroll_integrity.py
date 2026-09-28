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


# Table existence is stable for an engine's lifetime; ``has_table`` was previously
# hitting information_schema on every call (~7x per employee during a push).
_TABLE_CACHE: dict[int, set[str]] = {}


def _table_set(session: Session) -> set[str]:
    engine = session.get_bind()
    key = id(engine)
    cached = _TABLE_CACHE.get(key)
    if cached is None:
        try:
            cached = {t.lower() for t in inspect(engine).get_table_names()}
        except Exception:
            cached = set()
        _TABLE_CACHE[key] = cached
    return cached


def clear_table_cache() -> None:
    _TABLE_CACHE.clear()


def has_table(session: Session, name: str) -> bool:
    return name.lower() in _table_set(session)


def _expand(ids: list[int]):
    return bindparam("ids", expanding=True), [int(i) for i in ids]


def fetch_employees_for_payroll(session: Session, employee_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Batch equivalent of :func:`fetch_employee_for_payroll` — 3 queries total, not 3 per row.

    Soft-deleted (separated) employees are resolved like any other: a month they earned
    still belongs in that month's payroll, P9 and statutory files.
    """
    ids = sorted({int(i) for i in employee_ids if i is not None})
    if not ids or not has_table(session, "employees"):
        return {}
    param, values = _expand(ids)
    out: dict[int, dict[str, Any]] = {}
    rows = session.execute(
        text(
            """
            SELECT id, first_name, last_name, payroll_number, identification_type,
                   identification_number, kra_pin, hr_subdept_id,
                   retirement_contribution, mortgage_relief, relief
            FROM employees WHERE id IN :ids
            """
        ).bindparams(param),
        {"ids": values},
    ).mappings().all()
    for r in rows:
        emp = dict(r)
        emp["retirement_contribution"] = float(emp.get("retirement_contribution") or 0)
        emp["mortgage_relief"] = float(emp.get("mortgage_relief") or 0)
        emp["employee_relief"] = float(emp.get("relief") or 0)
        emp["department_id"] = None
        emp["beneficiary_name"] = ""
        out[int(emp["id"])] = emp

    if has_table(session, "assignments") and has_table(session, "departments"):
        param, values = _expand(ids)
        drows = session.execute(
            text(
                """
                SELECT a.employee_id AS eid, MIN(a.department_id) AS did
                FROM assignments a INNER JOIN departments d ON d.id = a.department_id
                WHERE a.employee_id IN :ids GROUP BY a.employee_id
                """
            ).bindparams(param),
            {"ids": values},
        ).mappings().all()
        for r in drows:
            e = out.get(int(r["eid"]))
            if e is not None and r["did"] is not None:
                e["department_id"] = int(r["did"])
    for e in out.values():
        if e.get("department_id") is None and e.get("hr_subdept_id"):
            try:
                e["department_id"] = int(e["hr_subdept_id"])
            except (TypeError, ValueError):
                e["department_id"] = None

    if has_table(session, "employee_payment_methods"):
        param, values = _expand(ids)
        brows = session.execute(
            text(
                """
                SELECT employee_id, beneficiary_name
                FROM employee_payment_methods
                WHERE employee_id IN :ids AND beneficiary_name IS NOT NULL AND beneficiary_name != ''
                ORDER BY employee_id, id
                """
            ).bindparams(param),
            {"ids": values},
        ).mappings().all()
        seen: set[int] = set()
        for r in brows:
            eid = int(r["employee_id"])
            if eid in seen:
                continue
            seen.add(eid)
            e = out.get(eid)
            if e is not None:
                e["beneficiary_name"] = str(r["beneficiary_name"]).strip()
    return out


def load_payroll_states(
    session: Session, employee_ids: list[int], payroll_date: date
) -> dict[int, PayrollRowState]:
    """Batch equivalent of :func:`load_payroll_row_state` — one query for all employees."""
    states: dict[int, PayrollRowState] = {int(i): PayrollRowState() for i in employee_ids}
    ids = sorted({int(i) for i in employee_ids if i is not None})
    if not ids or not has_table(session, "payrolls"):
        return states
    param, values = _expand(ids)
    rows = session.execute(
        text(
            "SELECT id, employee_id, finalized FROM payrolls "
            "WHERE payroll_date = :pd AND employee_id IN :ids ORDER BY employee_id, id"
        ).bindparams(param),
        {"pd": payroll_date, "ids": values},
    ).mappings().all()
    for r in rows:
        st = states.setdefault(int(r["employee_id"]), PayrollRowState())
        st.ids.append(int(r["id"]))
        if r.get("finalized") in (1, True, "1"):
            st.any_finalized = True
    for st in states.values():
        if st.ids:
            st.keep_id = st.ids[0]
    return states


def load_salary_arrears_states(
    session: Session, employee_ids: list[int], payroll_date: date
) -> dict[int, dict[str, Any]]:
    """First existing ``salary_arrears`` row per employee for the month (one query)."""
    out: dict[int, dict[str, Any]] = {}
    ids = sorted({int(i) for i in employee_ids if i is not None})
    if not ids or not has_table(session, "salary_arrears"):
        return out
    param, values = _expand(ids)
    rows = session.execute(
        text(
            "SELECT id, employee_id, paid_amount, balance, finalized FROM salary_arrears "
            "WHERE payroll_date = :pd AND employee_id IN :ids ORDER BY employee_id, id"
        ).bindparams(param),
        {"pd": payroll_date, "ids": values},
    ).mappings().all()
    for r in rows:
        eid = int(r["employee_id"])
        if eid not in out:
            out[eid] = dict(r)
    return out


def salary_arrears_ids_with_payments(session: Session, arrears_ids: list[int]) -> set[int]:
    ids = sorted({int(i) for i in arrears_ids if i is not None})
    if not ids or not has_table(session, "salary_arrears_payments"):
        return set()
    param, values = _expand(ids)
    rows = session.execute(
        text(
            "SELECT DISTINCT salary_arrears_id FROM salary_arrears_payments WHERE salary_arrears_id IN :ids"
        ).bindparams(param),
        {"ids": values},
    ).all()
    return {int(r[0]) for r in rows}


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
            WHERE id = :eid
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
    return_id: bool = True,
) -> int | None:
    """Insert or update one payroll row. Returns payroll id (skipped when ``return_id`` is False)."""
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
    if not return_id:
        return None
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


_UNSET = object()


def upsert_salary_arrears(
    session: Session,
    *,
    employee_id: int,
    payroll_date: date,
    net_pay: float,
    filter_label: str,
    now: datetime,
    report: PushIntegrityReport,
    existing_row: Any = _UNSET,
    has_payments: bool | None = None,
) -> None:
    """
    Mirror native payroll generation companion row in ``salary_arrears``.

    Preserves ``paid_amount`` when ``salary_arrears_payments`` exist; skips finalized arrears.
    ``existing_row``/``has_payments`` may be supplied (batch push) to skip per-row reads.
    """
    if not has_table(session, "salary_arrears"):
        return
    if existing_row is _UNSET:
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
    else:
        existing = existing_row

    net_r = round(float(net_pay), 2)
    flt = filter_label[:255] if len(filter_label) > 255 else filter_label

    if existing:
        if existing.get("finalized") in (1, True, "1"):
            report.salary_arrears_skipped_finalized += 1
            return
        aid = int(existing["id"])
        paid = float(existing.get("paid_amount") or 0)
        has_pay = has_payments if has_payments is not None else _salary_arrears_has_payments(session, aid)
        if has_pay:
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
