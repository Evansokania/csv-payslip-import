"""Plan (and optionally apply) allowance/deduction master creation from a classification.

The tenant models allowances/deductions three ways:
  * fixed allowance  -> ``allowances`` master
  * fixed deduction  -> ``deductions`` master
  * special (variable) allowance/deduction -> ``special_cat_names`` (type 0 / type 1)

Given a classification spreadsheet (Wage Type / Description / Section / Nature), this
routes each non-statutory earning/deduction to the right table and reports whether it
needs creating, already exists, or exists in a *different* representation (conflict).

Statutory deductions, memo, and calculated rows are skipped. Basic pay is skipped.
``plan_master_sync`` never writes; ``apply_master_sync`` performs the inserts.
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.header_utils import normalize_header

# Section (from classification) -> whether we manage it, and as allowance vs deduction.
_EARNING = "earning"
_OTHER_DED = "other deduction"
_SKIP_SECTIONS = {"statutory deduction", "memo", "calculated"}

# Names that are basic pay / net / gross totals — never a master.
_SKIP_NAMES = {
    normalize_header(x)
    for x in ("BASIC SALARY", "BASIC PAY", "GROSS PAY", "TOTAL GROSS AMOUNT", "NET PAYMENT", "NET PAY")
}

SPECIAL_TYPE_ALLOWANCE = 0
SPECIAL_TYPE_DEDUCTION = 1


@dataclass
class SyncAction:
    code: str
    name: str
    section: str
    nature: str
    kind: str            # "allowance" | "deduction"
    target: str          # "fixed" | "special"
    table: str           # "allowances" | "deductions" | "special_cat_names"
    action: str          # "create" | "exists" | "conflict"
    note: str = ""


@dataclass
class SyncPlan:
    actions: list[SyncAction] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            key = f"{a.table}:{a.action}"
            out[key] = out.get(key, 0) + 1
        out["skipped"] = len(self.skipped)
        return out


def _read_classification(content: bytes) -> list[dict[str, str]]:
    """Parse the classification workbook into rows with code/description/section/nature."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        ws = wb.active
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    # Locate the header row (contains "Wage Type" and "Section").
    header_idx = None
    for i, row in enumerate(grid[:10]):
        cells = {normalize_header(str(c)) for c in row if c not in (None, "")}
        if "WAGE TYPE" in cells and "SECTION" in cells:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Classification sheet: could not find the 'Wage Type'/'Section' header row")

    header = [normalize_header(str(c)) for c in grid[header_idx]]
    col = {name: idx for idx, name in enumerate(header)}
    need = ("WAGE TYPE", "DESCRIPTION", "SECTION", "NATURE")
    for n in need:
        if n not in col:
            raise ValueError(f"Classification sheet missing column: {n}")

    rows: list[dict[str, str]] = []
    for row in grid[header_idx + 1:]:
        def cell(name: str) -> str:
            i = col[name]
            v = row[i] if i < len(row) else None
            return "" if v is None else str(v).strip()

        section = cell("SECTION")
        desc = cell("DESCRIPTION")
        if not desc or not section:
            continue  # legend / blank rows
        rows.append(
            {"code": cell("WAGE TYPE"), "name": desc, "section": section, "nature": cell("NATURE")}
        )
    return rows


def _load_existing(session: Session) -> dict[str, Any]:
    insp = inspect(session.get_bind())
    allow: dict[str, int] = {}
    ded: dict[str, int] = {}
    ded_stat: set[str] = set()
    spec_allow: dict[str, int] = {}
    spec_ded: dict[str, int] = {}

    if insp.has_table("allowances"):
        for r in session.execute(text("SELECT id, name FROM allowances")).mappings():
            allow[normalize_header(r["name"])] = int(r["id"])
    if insp.has_table("deductions"):
        cols = {c["name"].lower() for c in insp.get_columns("deductions")}
        stat_sel = ", is_statutory" if "is_statutory" in cols else ""
        for r in session.execute(text(f"SELECT id, name{stat_sel} FROM deductions")).mappings():
            n = normalize_header(r["name"])
            ded[n] = int(r["id"])
            if stat_sel and int(r.get("is_statutory") or 0) == 1:
                ded_stat.add(n)
    if insp.has_table("special_cat_names"):
        for r in session.execute(
            text("SELECT id, name, type FROM special_cat_names WHERE deleted_at IS NULL")
        ).mappings():
            n = normalize_header(r["name"])
            if int(r["type"] or 0) == SPECIAL_TYPE_DEDUCTION:
                spec_ded[n] = int(r["id"])
            else:
                spec_allow[n] = int(r["id"])
    return {
        "allow": allow, "ded": ded, "ded_stat": ded_stat,
        "spec_allow": spec_allow, "spec_ded": spec_ded,
        "has_special": insp.has_table("special_cat_names"),
    }


def _is_fixed(nature: str) -> bool:
    return normalize_header(nature) == "FIXED"


def plan_master_sync(session: Session, classification_content: bytes) -> SyncPlan:
    """Dry-run: classify each wage type into a target table + action. Writes nothing."""
    rows = _read_classification(classification_content)
    ex = _load_existing(session)
    plan = SyncPlan()

    for row in rows:
        name = row["name"]
        norm = normalize_header(name)
        section = normalize_header(row["section"])
        nature = row["nature"]

        if norm in _SKIP_NAMES or section in _SKIP_SECTIONS:
            plan.skipped.append({"name": name, "section": row["section"], "reason": "statutory/memo/calculated/basic"})
            continue
        if section == normalize_header(_EARNING):
            kind = "allowance"
        elif section == normalize_header(_OTHER_DED):
            kind = "deduction"
        else:
            plan.skipped.append({"name": name, "section": row["section"], "reason": "unmanaged section"})
            continue

        fixed = _is_fixed(nature)
        target = "fixed" if fixed else "special"

        if kind == "allowance":
            table = "allowances" if fixed else "special_cat_names"
            in_target = norm in (ex["allow"] if fixed else ex["spec_allow"])
            in_other = norm in (ex["spec_allow"] if fixed else ex["allow"])
        else:
            table = "deductions" if fixed else "special_cat_names"
            in_target = norm in (ex["ded"] if fixed else ex["spec_ded"])
            in_other = norm in (ex["spec_ded"] if fixed else ex["ded"])

        note = ""
        if in_target:
            action = "exists"
        elif in_other:
            action = "conflict"
            note = f"already exists as {'special' if fixed else 'fixed'} {kind}"
        else:
            action = "create"

        plan.actions.append(
            SyncAction(
                code=row["code"], name=name, section=row["section"], nature=nature,
                kind=kind, target=target, table=table, action=action, note=note,
            )
        )
    return plan


def plan_as_dict(plan: SyncPlan) -> dict[str, Any]:
    return {
        "summary": plan.summary(),
        "actions": [asdict(a) for a in plan.actions],
        "skipped": plan.skipped,
    }


# --------------------------------------------------------------------------- apply


def _existing_columns(session: Session, table: str) -> set[str]:
    try:
        return {c["name"].lower() for c in inspect(session.get_bind()).get_columns(table)}
    except Exception:
        return set()


def _default_currency_id(session: Session) -> int:
    """Tenant's working currency: most-used on employee_allowances, else company profile, else 1."""
    for stmt in (
        "SELECT currency_id FROM employee_allowances GROUP BY currency_id ORDER BY COUNT(*) DESC LIMIT 1",
        "SELECT currency_id FROM company_profiles WHERE currency_id IS NOT NULL LIMIT 1",
        "SELECT id FROM currencies ORDER BY id LIMIT 1",
    ):
        try:
            v = session.execute(text(stmt)).scalar()
            if v:
                return int(v)
        except Exception:
            continue
    return 1


def _insert_row(session: Session, table: str, values: dict[str, Any]) -> int:
    cols = _existing_columns(session, table)
    data = {k: v for k, v in values.items() if k in cols}
    collist = ",".join(f"`{k}`" for k in data)
    placeholders = ",".join(f":{k}" for k in data)
    session.execute(text(f"INSERT INTO {table} ({collist}) VALUES ({placeholders})"), data)
    return int(session.execute(text("SELECT LAST_INSERT_ID()")).scalar())


def _allowance_values(name: str, currency_id: int, now: Any) -> dict[str, Any]:
    is_house = "HOUSE" in normalize_header(name)
    return {
        "name": name, "non_cash": 0, "currency_id": currency_id, "type": "per_employee",
        "rate": 0, "in_basic": 0, "taxable": 1, "tax_rate": 100.0, "has_relief": 0,
        "system_install": 0, "car_benefit": 0, "in_common_paye": 1,
        "is_house_allowance": 1 if is_house else 0, "in_house_levy": 1,
        "frequency": "0", "is_daily": 0, "taxable_percentage": 100,
        "created_at": now, "updated_at": now,
    }


def _deduction_values(name: str, now: Any) -> dict[str, Any]:
    return {
        "name": name, "due_date": 0, "type": "per_employee", "threshold": 0, "rate": None,
        "has_relief": 0, "is_statutory": 0, "included_in_costing_report": 0, "is_active": 1,
        "created_at": now, "updated_at": now,
    }


def _special_cat_values(name: str, type_int: int, now: Any) -> dict[str, Any]:
    return {
        "name": name, "type": type_int, "description": name,
        "included_in_costing_report": 0, "created_at": now, "updated_at": now,
    }


def apply_master_sync(
    session: Session,
    classification_content: bytes,
    *,
    reclassify_conflicts: bool = True,
) -> dict[str, Any]:
    """Create the planned masters. Conflicts (fixed-classified items that exist as special)
    are, when ``reclassify_conflicts``, created as fixed and their special category
    soft-deleted. Returns a report; the caller is responsible for commit.
    """
    from datetime import datetime

    plan = plan_master_sync(session, classification_content)
    ex = _load_existing(session)
    now = datetime.utcnow()
    currency_id = _default_currency_id(session)
    has_spec_softdelete = "deleted_at" in _existing_columns(session, "special_cat_names")

    created: list[dict[str, Any]] = []
    reclassified: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for a in plan.actions:
        if a.action == "exists":
            skipped.append({"name": a.name, "reason": "already exists in target", "table": a.table})
            continue
        if a.action == "conflict" and not reclassify_conflicts:
            skipped.append({"name": a.name, "reason": "conflict — left as special", "table": a.table})
            continue

        norm = normalize_header(a.name)
        if a.kind == "allowance" and a.target == "fixed":
            new_id = _insert_row(session, "allowances", _allowance_values(a.name, currency_id, now))
            target_tbl = "allowances"
        elif a.kind == "deduction" and a.target == "fixed":
            new_id = _insert_row(session, "deductions", _deduction_values(a.name, now))
            target_tbl = "deductions"
        else:  # special allowance/deduction
            type_int = SPECIAL_TYPE_DEDUCTION if a.kind == "deduction" else SPECIAL_TYPE_ALLOWANCE
            new_id = _insert_row(session, "special_cat_names", _special_cat_values(a.name, type_int, now))
            target_tbl = "special_cat_names"

        if a.action == "conflict":
            # Reclassify: soft-delete the old special category.
            old_id = (ex["spec_allow"] if a.kind == "allowance" else ex["spec_ded"]).get(norm)
            if old_id and has_spec_softdelete:
                session.execute(
                    text("UPDATE special_cat_names SET deleted_at = :d, updated_at = :d WHERE id = :id"),
                    {"d": now, "id": old_id},
                )
            reclassified.append({"name": a.name, "new_table": target_tbl, "new_id": new_id, "old_special_cat_id": old_id})
        else:
            created.append({"name": a.name, "table": target_tbl, "id": new_id})

    return {
        "created": created,
        "reclassified": reclassified,
        "skipped": skipped,
        "counts": {
            "created": len(created), "reclassified": len(reclassified), "skipped": len(skipped),
        },
    }
