"""Load tenant allowance/deduction master metadata for native-shaped payroll rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.header_utils import normalize_header


@dataclass
class AllowanceMeta:
    id: int
    name: str
    in_basic: int = 0
    non_cash: int = 0
    taxable: int = 1
    in_common_paye: int = 1
    tax_rate: float = 100.0


@dataclass
class DeductionMeta:
    id: int
    name: str
    included_in_costing_report: int = 0


@dataclass
class MasterContext:
    allowances_by_id: dict[int, AllowanceMeta] = field(default_factory=dict)
    deductions_by_id: dict[int, DeductionMeta] = field(default_factory=dict)
    allowances_by_norm: dict[str, AllowanceMeta] = field(default_factory=dict)
    deductions_by_norm: dict[str, DeductionMeta] = field(default_factory=dict)


def _table_columns(session: Session, table: str) -> set[str]:
    try:
        bind = session.get_bind()
        return {c["name"].lower() for c in inspect(bind).get_columns(table)}
    except Exception:
        return set()


def load_master_context(session: Session) -> MasterContext:
    """Read ``allowances`` / ``deductions`` masters; tolerate missing optional columns."""
    ctx = MasterContext()
    cols_a = _table_columns(session, "allowances")
    if cols_a:
        sel = ["id", "name"]
        if "in_basic" in cols_a:
            sel.append("in_basic")
        if "non_cash" in cols_a:
            sel.append("non_cash")
        if "taxable" in cols_a:
            sel.append("taxable")
        if "in_common_paye" in cols_a:
            sel.append("in_common_paye")
        if "tax_rate" in cols_a:
            sel.append("tax_rate")
        rows = session.execute(text(f"SELECT {', '.join(sel)} FROM allowances")).mappings().all()
        for r in rows:
            aid = int(r["id"])
            meta = AllowanceMeta(
                id=aid,
                name=str(r.get("name") or ""),
                in_basic=int(r.get("in_basic") or 0) if "in_basic" in cols_a else 0,
                non_cash=int(r.get("non_cash") or 0) if "non_cash" in cols_a else 0,
                taxable=int(r.get("taxable") if r.get("taxable") is not None else 1)
                if "taxable" in cols_a
                else 1,
                in_common_paye=int(r.get("in_common_paye") or 1) if "in_common_paye" in cols_a else 1,
                tax_rate=float(r.get("tax_rate") or 100) if "tax_rate" in cols_a else 100.0,
            )
            ctx.allowances_by_id[aid] = meta
            ctx.allowances_by_norm[normalize_header(meta.name)] = meta

    cols_d = _table_columns(session, "deductions")
    if cols_d:
        sel = ["id", "name"]
        if "included_in_costing_report" in cols_d:
            sel.append("included_in_costing_report")
        rows = session.execute(text(f"SELECT {', '.join(sel)} FROM deductions")).mappings().all()
        for r in rows:
            did = int(r["id"])
            meta = DeductionMeta(
                id=did,
                name=str(r.get("name") or ""),
                included_in_costing_report=int(r.get("included_in_costing_report") or 0)
                if "included_in_costing_report" in cols_d
                else 0,
            )
            ctx.deductions_by_id[did] = meta
            ctx.deductions_by_norm[normalize_header(meta.name)] = meta
    return ctx


def enrich_earning_line(line: dict[str, Any], masters: MasterContext) -> dict[str, Any]:
    """Add Laravel ``allowances`` JSON fields from master when ``allowance_id`` is set."""
    out = dict(line)
    label = str(out.get("label") or out.get("name") or "Earning").strip()
    out["label"] = label
    out["name"] = label
    amt = float(out.get("amount") or 0)
    aid = out.get("allowance_id")
    if aid is not None:
        try:
            meta = masters.allowances_by_id.get(int(aid))
        except (TypeError, ValueError):
            meta = None
        if meta:
            out["allowance_id"] = meta.id
            out["in_basic"] = meta.in_basic
            out["non_cash"] = bool(meta.non_cash)
            out["taxable"] = meta.taxable
            out["in_common_paye"] = meta.in_common_paye
            out["tax_rate"] = meta.tax_rate
            out["tax_amount"] = amt if meta.taxable else 0.0
            if not out.get("name"):
                out["name"] = meta.name
                out["label"] = meta.name
        else:
            out.setdefault("in_basic", 0)
            out.setdefault("non_cash", False)
            out.setdefault("taxable", 1)
            out.setdefault("tax_amount", amt)
    else:
        norm = normalize_header(label)
        meta = masters.allowances_by_norm.get(norm)
        if meta:
            out["allowance_id"] = meta.id
            out["in_basic"] = meta.in_basic
            out["non_cash"] = bool(meta.non_cash)
            out["taxable"] = meta.taxable
            out["in_common_paye"] = meta.in_common_paye
            out["tax_rate"] = meta.tax_rate
            out["tax_amount"] = amt if meta.taxable else 0.0
            out["name"] = meta.name
            out["label"] = meta.name
        else:
            out.setdefault("in_basic", 0)
            out.setdefault("non_cash", False)
            out.setdefault("taxable", 1)
            out.setdefault("in_common_paye", 1)
            out.setdefault("tax_rate", 100.0)
            out.setdefault("tax_amount", amt)
    return out


def enrich_deduction_line(line: dict[str, Any], masters: MasterContext) -> dict[str, Any]:
    """Add ``included_in_costing_report`` from deduction master when linked."""
    out = dict(line)
    label = str(out.get("label") or out.get("name") or "Deduction").strip()
    out["label"] = label
    out["name"] = label
    did = out.get("deduction_id")
    if did is not None:
        try:
            meta = masters.deductions_by_id.get(int(did))
        except (TypeError, ValueError):
            meta = None
        if meta:
            out["deduction_id"] = meta.id
            out["included_in_costing_report"] = meta.included_in_costing_report
            if not out.get("name"):
                out["name"] = meta.name
                out["label"] = meta.name
        else:
            out.setdefault("included_in_costing_report", 0)
    else:
        norm = normalize_header(label)
        meta = masters.deductions_by_norm.get(norm)
        if meta:
            out["deduction_id"] = meta.id
            out["included_in_costing_report"] = meta.included_in_costing_report
            out["name"] = meta.name
            out["label"] = meta.name
        else:
            out.setdefault("included_in_costing_report", 0)
    return out
