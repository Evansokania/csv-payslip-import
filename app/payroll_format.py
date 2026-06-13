"""Laravel payroll row JSON formatting (shared by builder and push)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.header_utils import normalize_header


def _as_list(val: Any) -> list[dict[str, Any]]:
    if val is None:
        return []
    if isinstance(val, list):
        return [x for x in val if isinstance(x, dict)]
    if isinstance(val, str):
        try:
            d = json.loads(val)
            return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _as_dict(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            d = json.loads(val)
            return d if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _computed_lookup(computed: dict[str, Any], *candidates: str) -> float:
    """Match CSV computed keys case/spacing insensitively."""
    if not computed:
        return 0.0
    norm_map = {normalize_header(k): float(v or 0) for k, v in computed.items()}
    for c in candidates:
        nk = normalize_header(c)
        if nk in norm_map:
            return float(norm_map[nk])
    return 0.0


def _line_label_norm(line: dict[str, Any]) -> str:
    return normalize_header(str(line.get("label") or line.get("name") or ""))


def _is_basic_line(line: dict[str, Any]) -> bool:
    label = _line_label_norm(line)
    return "BASIC" in label or label == "BASIC PAY"


def _is_net_total_line(line: dict[str, Any]) -> bool:
    label = _line_label_norm(line)
    return label in ("NETTPAY", "NET PAY", "NET SALARY", "NET", "NETPAY")


def _is_gross_total_line(line: dict[str, Any]) -> bool:
    label = _line_label_norm(line)
    return label in ("GROSS AMOUNT", "GROSS PAY", "GROSS")


def _is_pension_line(line: dict[str, Any]) -> bool:
    label = _line_label_norm(line)
    if "RELIEF" in label:
        return False
    return label in ("PENSION", "RETIREMENT CONTRIBUTION") or "PENSION" in label


def _is_statutory_deduction_line(line: dict[str, Any]) -> bool:
    label = _line_label_norm(line)
    if label in (
        "NSSF",
        "NHIF",
        "SHIF",
        "PAYE",
        "PAYE DUE",
        "AFFORDABLE HOUSING LEVY",
        "AHL",
        "HOUSING LEVY",
    ):
        return True
    if label in ("NSSF 1", "NSSF 2", "NSSF TIER 1", "NSSF TIER 2", "NSSF T1", "NSSF T2"):
        return True
    return "NHDFLEVY" in label


def normalize_snapshot_lines(
    earnings_lines: list[dict[str, Any]],
    deduction_lines: list[dict[str, Any]],
    computed: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    Reclassify common CSV mapping mistakes so push output matches native payroll shape.

    Moves net/gross totals from earnings into ``computed``, pension into deductions,
    and drops statutory duplicate deduction lines when totals exist in ``computed``.
    """
    earnings = list(earnings_lines)
    deductions = list(deduction_lines)
    comp = dict(computed or {})

    kept_earnings: list[dict[str, Any]] = []
    for line in earnings:
        amt = float(line.get("amount") or 0)
        if _is_net_total_line(line):
            if amt != 0 and _computed_lookup(comp, "NETTPAY", "NET PAY", "NET SALARY", "NET", "NETPAY") <= 0:
                comp["NET PAY"] = amt
            continue
        if _is_gross_total_line(line):
            if amt != 0 and _computed_lookup(comp, "GROSS AMOUNT", "GROSS PAY", "GROSS") <= 0:
                comp["GROSS PAY"] = amt
            continue
        if _is_pension_line(line):
            if amt != 0:
                deductions.append(
                    {
                        "label": "Retirement Contribution",
                        "name": "Retirement Contribution",
                        "amount": amt,
                        "included_in_costing_report": 0,
                    }
                )
            continue
        kept_earnings.append(line)
    earnings = kept_earnings

    nssf_t1 = nssf_t2 = 0.0
    kept_deductions: list[dict[str, Any]] = []
    for line in deductions:
        label = _line_label_norm(line)
        amt = float(line.get("amount") or 0)
        if label in ("NSSF 1", "NSSF TIER 1", "NSSF T1", "TIER 1 NSSF"):
            nssf_t1 += amt
            continue
        if label in ("NSSF 2", "NSSF TIER 2", "NSSF T2", "TIER 2 NSSF"):
            nssf_t2 += amt
            continue
        if _is_statutory_deduction_line(line):
            continue
        if label == "PENSION" or label == "RETIREMENT CONTRIBUTION":
            line = dict(line)
            line["label"] = "Retirement Contribution"
            line["name"] = "Retirement Contribution"
        kept_deductions.append(line)
    deductions = kept_deductions

    if nssf_t1 > 0 and _computed_lookup(comp, "NSSF TIER 1", "NSSF T1") <= 0:
        comp["NSSF Tier 1"] = nssf_t1
    if nssf_t2 > 0 and _computed_lookup(comp, "NSSF TIER 2", "NSSF T2") <= 0:
        comp["NSSF Tier 2"] = nssf_t2
    if nssf_t1 + nssf_t2 > 0 and _computed_lookup(comp, "NSSF") <= 0:
        comp["NSSF"] = nssf_t1 + nssf_t2

    return earnings, deductions, comp


def _allowance_earnings_for_json(earnings_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in earnings_lines:
        if _is_basic_line(line) or _is_net_total_line(line) or _is_gross_total_line(line) or _is_pension_line(line):
            continue
        if float(line.get("amount") or 0) == 0:
            continue
        out.append(line)
    return out


def _non_cash_int(val: Any) -> int:
    if isinstance(val, bool):
        return 1 if val else 0
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _basic_from_earnings(earnings: list[dict[str, Any]]) -> float:
    for line in earnings:
        label = normalize_header(line.get("label"))
        if not label:
            continue
        if "BASIC" in label or label == "BASIC PAY":
            return float(line.get("amount") or 0)
    return 0.0


def _filter_value(run_id: int) -> str:
    return f"CSV import · run {run_id}"


def _fetch_nssf_lel_uel(session: Session) -> tuple[float, float] | None:
    """Lower / upper earning limits from tenant `deduction_slabs` for NSSF (slab 1 / slab 2 max_amount)."""
    try:
        if not inspect(session.get_bind()).has_table("deduction_slabs"):
            return None
        rows = session.execute(
            text(
                """
                SELECT ds.slab_number, ds.max_amount
                FROM deduction_slabs ds
                INNER JOIN deductions d ON d.id = ds.deduction_id
                WHERE d.name = 'NSSF'
                ORDER BY ds.slab_number ASC
                LIMIT 2
                """
            )
        ).mappings().all()
    except Exception:
        return None
    if len(rows) < 2:
        return None
    lel = float(rows[0]["max_amount"] or 0)
    uel = float(rows[1]["max_amount"] or 0)
    if lel <= 0 or uel < lel:
        return None
    return lel, uel


def _pensionable_gross_for_nssf(computed: dict[str, Any], earnings_lines: list[dict[str, Any]]) -> float:
    """Pensionable base for NSSF — same fields as typical payroll; else sum of mapped earnings."""
    g = _computed_lookup(
        computed,
        "NSSF GROSS",
        "PENSIONABLE PAY",
        "PENSIONABLE",
        "PENSIONABLE INCOME",
        "TAXABLE PAY",
        "TAXABLE",
        "GROSS PAY",
        "GROSS AMOUNT",
        "GROSS",
    )
    if g > 0:
        return g
    return float(sum(float(x.get("amount") or 0) for x in earnings_lines))


def _blade_nssf_tier_split(nssf_total: float, payroll_month_end: date) -> tuple[float, float]:
    """Fallback when slabs are missing — matches payslip_content.blade.php tier display caps."""
    if nssf_total <= 0:
        return 0.0, 0.0
    february_2025 = date(2025, 2, 1)
    tier1_cap = 480.0 if payroll_month_end >= february_2025 else 420.0
    t1 = min(nssf_total, tier1_cap)
    return round(t1, 2), round(max(0.0, nssf_total - t1), 2)


def _split_nssf_employee_contribution_kra(
    pensionable: float,
    nssf_total: float,
    lel: float,
    uel: float,
    payroll_month_end: date,
) -> tuple[float, float]:
    """
    Tier I / II employee NSSF split consistent with PayrollCalculatorDynamic::calculateNSSF
    (6% up to LEL, 6% on (min(pensionable, UEL) - LEL)).
    If the CSV total differs from the recomputed amount (rounding), scale tier components to match the file total.
    """
    if nssf_total <= 0:
        return 0.0, 0.0
    rate = 0.06
    lel = max(float(lel), 0.01)
    uel = max(float(uel), lel)

    if pensionable <= 0:
        return _blade_nssf_tier_split(nssf_total, payroll_month_end)

    if pensionable <= lel + 1e-9:
        t_only = round(pensionable * rate, 2)
        if abs(t_only - nssf_total) <= 2.55:
            return t_only, 0.0
        return _blade_nssf_tier_split(nssf_total, payroll_month_end)

    t1 = round(lel * rate, 2)
    capped = min(max(pensionable, 0.0), uel)
    t2 = round((capped - lel) * rate, 2)
    recomputed = round(t1 + t2, 2)
    if recomputed <= 0:
        return _blade_nssf_tier_split(nssf_total, payroll_month_end)
    if abs(recomputed - nssf_total) <= 2.55:
        return t1, t2
    scale = nssf_total / recomputed
    st1 = round(t1 * scale, 2)
    st2 = round(max(0.0, nssf_total - st1), 2)
    return st1, st2


def apply_nssf_tier_derivation(
    session: Session | None,
    payroll_month_end: date,
    computed: dict[str, Any],
    earnings_lines: list[dict[str, Any]],
    stat: dict[str, Any],
) -> None:
    """
    Fill nssf_tier_1 / nssf_tier_2 when the CSV only has NSSF total: use tenant NSSF slabs + 6% KRA-style
    split, else the same payslip blade fallback used in HR Genie.
    """
    nssf = float(stat.get("nssf") or 0)
    t1_ex = float(stat.get("nssf_tier_1") or 0)
    t2_ex = float(stat.get("nssf_tier_2") or 0)
    if nssf <= 0:
        stat["nssf_tier_1"] = 0.0
        stat["nssf_tier_2"] = 0.0
        return
    if t1_ex > 0 or t2_ex > 0:
        s = t1_ex + t2_ex
        if s > 0 and abs(s - nssf) > 0.02:
            scale = nssf / s
            stat["nssf_tier_1"] = round(t1_ex * scale, 2)
            stat["nssf_tier_2"] = round(max(0.0, nssf - stat["nssf_tier_1"]), 2)
        else:
            stat["nssf_tier_1"] = round(t1_ex, 2)
            stat["nssf_tier_2"] = round(t2_ex, 2)
        return
    pensionable = _pensionable_gross_for_nssf(computed, earnings_lines)
    bounds = _fetch_nssf_lel_uel(session) if session else None
    if bounds:
        lel, uel = bounds
        nt1, nt2 = _split_nssf_employee_contribution_kra(pensionable, nssf, lel, uel, payroll_month_end)
    else:
        nt1, nt2 = _blade_nssf_tier_split(nssf, payroll_month_end)
    stat["nssf_tier_1"] = nt1
    stat["nssf_tier_2"] = nt2


def _fetch_prescribed_loan_rate(session: Session) -> float:
    """
    Same source as Payroll\\Parsers\\LoanCalculator::getPrescribedRate()
    (policies.module_id = 22, policy = 'PRESCRIBED LOAN RATE'); value in DB is whole percent, PHP divides by 100.
    """
    try:
        if not inspect(session.get_bind()).has_table("policies"):
            return 0.15
        row = session.execute(
            text(
                "SELECT `value` FROM policies WHERE module_id = 22 AND policy = :p LIMIT 1"
            ),
            {"p": "PRESCRIBED LOAN RATE"},
        ).scalar()
        if row is None or str(row).strip() == "":
            return 0.15
        return float(str(row).strip()) / 100.0
    except Exception:
        return 0.15


def build_kra_p9_json(
    *,
    employee_id: int,
    for_month: date,
    basic_salary: float,
    stat: dict[str, Any],
    prescribed_rate: float,
) -> str:
    """
    JSON for Laravel `payrolls.kra` — same keys as PayrollCalculatorDynamic P9 / KRAP9 fillable fields.
    """
    nssf = float(stat.get("nssf") or 0)
    paye_due = float(stat.get("paye_due") or 0)
    personal_relief = float(stat.get("personal_relief") or 0)
    tax_charged = float(stat.get("tax_charged") or 0)
    if tax_charged <= 0 and (paye_due > 0 or personal_relief > 0):
        tax_charged = paye_due + personal_relief
    relief = personal_relief
    paye = max(0.0, paye_due)
    p9: dict[str, Any] = {
        "employee_id": int(employee_id),
        "for_month": for_month.isoformat(),
        "basic_salary": float(basic_salary),
        "non_cash": "[]",
        "quarters": 0,
        "nssf": float(nssf),
        "tax_charged": float(tax_charged),
        "relief": float(relief),
        "paye": float(paye),
        "prescribed_rate": float(prescribed_rate),
    }
    return json.dumps(p9, default=str)


def _statutory_amounts(computed: dict[str, Any], _payroll_month_end: date) -> dict[str, Any]:
    nssf = _computed_lookup(computed, "NSSF")
    t1 = _computed_lookup(computed, "NSSF TIER 1", "NSSF T1", "TIER 1 NSSF")
    t2 = _computed_lookup(computed, "NSSF TIER 2", "NSSF T2", "TIER 2 NSSF")
    if nssf <= 0 and (t1 > 0 or t2 > 0):
        nssf = t1 + t2
    nhif = _computed_lookup(computed, "NHIF")
    shif = _computed_lookup(computed, "SHIF")
    ahl = _computed_lookup(
        computed,
        "AFFORDABLE HOUSING LEVY",
        "AHL",
        "HOUSING LEVY",
        "NHDFLEVYEMPLOYEE",
    )
    paye_due = _computed_lookup(computed, "PAYE")
    personal_relief = _computed_lookup(computed, "PERSONAL RELIEF")
    insurance_relief = _computed_lookup(computed, "INSURANCE RELIEF")
    # Native ``payrolls.total_relief`` is non-personal relief (insurance/NHIF), not personal relief.
    total_relief = insurance_relief
    tax_charged = _computed_lookup(computed, "TAX CHARGED", "GROSS TAX")
    if tax_charged <= 0 and (paye_due > 0 or personal_relief > 0):
        tax_charged = paye_due + personal_relief
    ahl_relief = _computed_lookup(computed, "AHL RELIEF")
    shif_relief = _computed_lookup(computed, "SHIF RELIEF")
    return {
        "nssf": nssf,
        "nssf_tier_1": t1,
        "nssf_tier_2": t2,
        "nhif": nhif,
        "shif": shif,
        "ahl": ahl,
        "paye_due": paye_due,
        "personal_relief": personal_relief,
        "insurance_relief": insurance_relief,
        "total_relief": total_relief,
        "tax_charged": tax_charged,
        "ahl_relief": ahl_relief,
        "shif_relief": shif_relief,
    }


def _earnings_bucket_totals(earnings_lines: list[dict[str, Any]]) -> dict[str, float]:
    """Map common CSV labels to Laravel payroll allowance columns when present."""
    out = {
        "house_allowance": 0.0,
        "transport_allowance": 0.0,
        "overtime": 0.0,
        "other_allowances": 0.0,
        "leave_encashment": 0.0,
        "other_earning": 0.0,
    }
    for line in earnings_lines:
        label = normalize_header(str(line.get("label") or ""))
        amt = float(line.get("amount") or 0)
        if amt == 0:
            continue
        if "BASIC" in label or _is_net_total_line(line) or _is_pension_line(line) or _is_gross_total_line(line):
            continue
        if any(x in label for x in ("HOUSE", "HOUSING", "HSE ", " HSE")):
            out["house_allowance"] += amt
        elif any(x in label for x in ("TRANSPORT", "TRAVEL")):
            out["transport_allowance"] += amt
        elif "OVERTIME" in label or label.startswith("OT") or " OT" in label:
            out["overtime"] += amt
        elif "LEAVE" in label and ("ENCASH" in label or "PAYOUT" in label):
            out["leave_encashment"] += amt
        else:
            out["other_allowances"] += amt
    return out


def _paye_slip_object(
    *,
    tax_charged: float,
    personal_relief: float,
    paye_due: float,
    insurance_relief: float = 0.0,
) -> dict[str, Any]:
    relief_list: list[dict[str, Any]] = []
    if personal_relief > 0:
        relief_list.append({"name": "Personal Relief", "amount": round(personal_relief, 2)})
    if insurance_relief > 0:
        relief_list.append({"name": "Insurance Relief", "amount": round(insurance_relief, 2)})
    total_reliefs = round(personal_relief + insurance_relief, 2)
    obj: dict[str, Any] = {
        "name": "PAYE",
        "raw_amount": round(tax_charged, 2),
        "total_reliefs": total_reliefs,
        "amount": round(paye_due, 2),
    }
    if relief_list:
        obj["relief"] = relief_list
    return obj


def _paye_slip_object_minimal(paye_due: float) -> dict[str, Any]:
    """Shape used inside payslip JSON string fields (matches older imports without name/total_reliefs)."""
    return {
        "name": "PAYE",
        "raw_amount": round(paye_due, 2),
        "total_reliefs": 0.0,
        "amount": round(paye_due, 2),
        "relief": [],
    }


def _untaxable_lines_from_statutory(stat: dict[str, Any]) -> list[dict[str, Any]]:
    """``payslip.untaxable`` — same names/order as ``PayrollCalculatorDynamic::calculatePayslip``."""
    items: list[dict[str, Any]] = []
    nssf = float(stat.get("nssf") or 0)
    if nssf > 0:
        items.append({"name": "NSSF", "amount": round(nssf, 2), "add": False})
    shif = float(stat.get("shif") or 0)
    if shif > 0:
        items.append({"name": "SHIF", "amount": round(shif, 2), "add": False})
    ahl = float(stat.get("ahl") or 0)
    if ahl > 0:
        items.append({"name": "Affordable Housing Levy", "amount": round(ahl, 2), "add": False})
    nhif = float(stat.get("nhif") or 0)
    if nhif > 0:
        items.append({"name": "NHIF", "amount": round(nhif, 2), "add": False})
    return items


def _laravel_deductions_column(
    stat: dict[str, Any],
    variable_deductions: list[dict[str, Any]],
    *,
    insurance_relief: float = 0.0,
) -> list[dict[str, Any]]:
    """``payrolls.deductions`` — statutory first (native order), then variable lines."""
    rows: list[dict[str, Any]] = []

    nssf_amt = float(stat["nssf"] or 0)
    if nssf_amt > 0:
        rows.append({"name": "NSSF", "amount": round(nssf_amt, 2), "included_in_costing_report": 0})

    tax_charged = float(stat["tax_charged"] or 0)
    personal_relief = float(stat["personal_relief"] or 0)
    paye_due = float(stat["paye_due"] or 0)
    if paye_due > 0 or personal_relief > 0 or tax_charged > 0:
        if personal_relief > 0:
            rows.append(
                {
                    "name": "PAYE",
                    "included_in_costing_report": 0,
                    "amount": {
                        "amount": round(tax_charged or (paye_due + personal_relief), 2),
                        "included_in_costing_report": 0,
                        "relief": {"name": "Personal Relief", "amount": round(personal_relief, 2)},
                    },
                }
            )
        else:
            rows.append(
                {
                    "name": "PAYE",
                    "included_in_costing_report": 0,
                    "amount": round(paye_due, 2),
                }
            )

    shif_amt = float(stat["shif"] or 0)
    if shif_amt > 0:
        row: dict[str, Any] = {"name": "SHIF", "included_in_costing_report": 0, "amount": round(shif_amt, 2)}
        sr = float(stat.get("shif_relief") or 0)
        if sr > 0:
            row["relief"] = {"name": "SHIF Relief", "amount": round(sr, 2)}
        rows.append(row)

    ahl_amt = float(stat["ahl"] or 0)
    if ahl_amt > 0:
        row_a: dict[str, Any] = {
            "name": "Affordable Housing Levy",
            "included_in_costing_report": 0,
            "amount": round(ahl_amt, 2),
        }
        ar = float(stat.get("ahl_relief") or 0)
        if ar > 0:
            row_a["relief"] = {"name": "AHL Relief", "amount": round(ar, 2)}
        rows.append(row_a)

    nhif_amt = float(stat["nhif"] or 0)
    if nhif_amt > 0:
        rows.append({"name": "NHIF", "included_in_costing_report": 0, "amount": round(nhif_amt, 2)})

    for line in variable_deductions:
        label = str(line.get("name") or line.get("label") or "Deduction").strip()
        amt = float(line.get("amount") or 0)
        if amt == 0 or not label:
            continue
        icr = int(line.get("included_in_costing_report") or 0)
        norm = normalize_header(label)
        if insurance_relief > 0 and "INSURANCE" in norm:
            rows.append(
                {
                    "name": label,
                    "included_in_costing_report": icr,
                    "amount": {
                        "amount": round(amt, 2),
                        "included_in_costing_report": icr,
                        "relief": {"name": "Insurance Relief", "amount": round(insurance_relief, 2)},
                    },
                }
            )
        else:
            rows.append(
                {
                    "name": label,
                    "included_in_costing_report": icr,
                    "amount": round(amt, 2),
                }
            )

    rows.append({"name": "Total Deduction Relief", "included_in_costing_report": 0, "amount": 0})
    return rows


def _payslip_variable_deductions(deduction_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in deduction_lines:
        if _is_statutory_deduction_line(line):
            continue
        label = str(line.get("name") or line.get("label") or "Deduction").strip()
        amt = float(line.get("amount") or 0)
        if amt == 0:
            continue
        row: dict[str, Any] = {"name": label, "amount": round(amt, 2)}
        out.append(row)
    return out


def _payslip_statutory_extra_lines(stat: dict[str, Any]) -> list[dict[str, Any]]:
    """Lines inside payslip JSON `deductions` (excludes NSSF — shown via `nssf` tiers) and PAYE (via `paye`)."""
    lines: list[dict[str, Any]] = []
    for name, key in (("SHIF", "shif"), ("NHIF", "nhif")):
        amt = float(stat.get(key) or 0)
        if amt <= 0:
            continue
        row: dict[str, Any] = {"name": name, "amount": round(amt, 2)}
        if name == "SHIF" and float(stat.get("shif_relief") or 0) > 0:
            row["relief"] = {"name": "SHIF Relief", "amount": round(float(stat["shif_relief"]), 2)}
        lines.append(row)
    ahl_amt = float(stat.get("ahl") or 0)
    if ahl_amt > 0:
        r: dict[str, Any] = {"name": "Affordable Housing Levy", "amount": round(ahl_amt, 2)}
        if float(stat.get("ahl_relief") or 0) > 0:
            r["relief"] = {"name": "AHL Relief", "amount": round(float(stat["ahl_relief"]), 2)}
        lines.append(r)
    return lines


def _resolve_total_deductions(
    computed: dict[str, Any],
    gross: float,
    net: float,
    stat: dict[str, Any],
    variable_deductions: list[dict[str, Any]],
    paye_due: float,
) -> float:
    td = _computed_lookup(computed, "TOTAL DEDUCTION", "TOTAL DEDUCTIONS")
    if td > 0:
        return round(td, 2)
    if gross > 0 and net > 0 and gross >= net:
        return round(gross - net, 2)
    partial = paye_due
    partial += float(stat.get("nssf") or 0)
    partial += float(stat.get("nhif") or 0)
    partial += float(stat.get("shif") or 0)
    partial += float(stat.get("ahl") or 0)
    partial += sum(float(x.get("amount") or 0) for x in variable_deductions)
    return round(partial, 2)


def _costing_report_from_deductions(rows: list[dict[str, Any]]) -> tuple[str, float]:
    """Build ``deductions_in_costing_report`` JSON and total from payroll deductions rows."""
    costing: list[dict[str, Any]] = []
    total = 0.0
    for row in rows:
        if int(row.get("included_in_costing_report") or 0) != 1:
            continue
        amt = row.get("amount")
        if isinstance(amt, dict):
            val = float(amt.get("amount") or 0)
        else:
            val = float(amt or 0)
        if val <= 0:
            continue
        costing.append(row)
        total += val
    return json.dumps(costing, default=str), round(total, 2)


def build_laravel_payslip_json(
    *,
    statutory: dict[str, Any],
    first_name: str,
    last_name: str,
    payroll_number: str,
    identification_type: str | None,
    identification_number: str | None,
    kra_pin: str | None,
    payroll_month_end: date,
    earnings_lines: list[dict[str, Any]],
    deduction_lines: list[dict[str, Any]],
    computed: dict[str, Any],
) -> dict[str, Any]:
    """Payslip JSON compatible with Laravel payslip blade (statutory + variable deductions)."""
    stat = statutory
    gross = _computed_lookup(computed, "GROSS AMOUNT", "GROSS PAY", "GROSS")
    net = _computed_lookup(computed, "NETTPAY", "NET PAY", "NET SALARY", "NET", "NETPAY")
    third = _computed_lookup(computed, "A THIRD RULE", "THIRD RULE")
    paye_due = float(stat["paye_due"] or 0)
    personal_relief = float(stat["personal_relief"] or 0)
    insurance_relief = float(stat.get("insurance_relief") or 0)
    tax_charged = float(stat["tax_charged"] or 0)

    basic_slip = _basic_from_earnings(earnings_lines)
    if basic_slip <= 0 and gross > 0:
        basic_slip = gross

    allowances_slip: list[dict[str, Any]] = []
    for line in _allowance_earnings_for_json(earnings_lines):
        label = str(line.get("name") or line.get("label") or "Earning").strip()
        amt = float(line.get("amount") or 0)
        tax_amount = float(line.get("tax_amount") if line.get("tax_amount") is not None else amt)
        row: dict[str, Any] = {
            "name": label,
            "amount": round(amt, 2),
            "included_in_basic_pay": bool(line.get("in_basic")),
            "tax_name": label,
            "tax_amount": round(tax_amount, 2),
            "show": True,
            "detailed": False,
        }
        allowances_slip.append(row)

    variable_slip: list[dict[str, Any]] = []
    for line in deduction_lines:
        if _is_statutory_deduction_line(line):
            continue
        label = str(line.get("name") or line.get("label") or "Deduction").strip()
        amt = float(line.get("amount") or 0)
        if amt == 0:
            continue
        row = {"name": label, "amount": round(amt, 2)}
        if insurance_relief > 0 and "INSURANCE" in normalize_header(label):
            row["relief"] = {
                "name": "Insurance Relief",
                "amount": round(insurance_relief, 2),
                "deducted": False,
            }
        variable_slip.append(row)
    deductions_slip = _payslip_statutory_extra_lines(stat) + variable_slip

    nssf_amt = float(stat["nssf"] or 0)
    t1, t2 = float(stat["nssf_tier_1"] or 0), float(stat["nssf_tier_2"] or 0)
    nssf_obj: dict[str, Any] = {"name": "NSSF", "amount": round(nssf_amt, 2)}
    if nssf_amt > 0:
        nssf_obj["tier_1"] = round(t1, 2)
        nssf_obj["tier_2"] = round(t2, 2)

    if personal_relief > 0 or insurance_relief > 0 or tax_charged > paye_due:
        paye_obj = _paye_slip_object(
            tax_charged=tax_charged,
            personal_relief=personal_relief,
            paye_due=paye_due,
            insurance_relief=insurance_relief,
        )
        paye_json = json.dumps(paye_obj)
    else:
        paye_json = json.dumps(_paye_slip_object_minimal(paye_due))

    start = payroll_month_end.replace(day=1)
    date_str = f"{start.strftime('%b %d, %Y')} - {payroll_month_end.strftime('%b %d, %Y')}"

    taxable = _computed_lookup(computed, "TAXABLE PAY", "TAXABLE")
    if taxable <= 0:
        taxable = gross if gross > 0 else basic_slip + sum(float(x.get("amount") or 0) for x in allowances_slip)

    total_allow = round(sum(float(x.get("amount") or 0) for x in allowances_slip), 2)
    total_ded = _resolve_total_deductions(computed, gross, net, stat, deduction_lines, paye_due)

    untaxable_items = _untaxable_lines_from_statutory(stat)

    net_rounded = round(net, 2) if net else round(
        gross - total_ded if gross else basic_slip + total_allow - total_ded,
        2,
    )

    return {
        "employee_name": f"{first_name} {last_name}".strip(),
        "payroll_number": payroll_number or "",
        "identification_type": identification_type or "",
        "identification_number": identification_number or "",
        "pin_number": (kra_pin or "").strip(),
        "date": date_str,
        "basic_pay": round(basic_slip, 2),
        "gross_pay": round(gross, 2)
        if gross
        else round(basic_slip + total_allow, 2),
        "net_pay": net_rounded,
        "taxable_pay": round(taxable, 2),
        "total_allowances": total_allow,
        "total_deductions": total_ded,
        "paye_due": round(paye_due, 2),
        "leave_days": 0,
        "total_non_tax": round(sum(float(x.get("amount") or 0) for x in untaxable_items), 2),
        "other_benefits": "TBD",
        "untaxable": json.dumps(untaxable_items),
        "allowances": json.dumps(allowances_slip),
        "deductions": json.dumps(deductions_slip),
        "paye": paye_json,
        "nssf": json.dumps(nssf_obj),
    }


def _krap9_row_from_payroll_kra_json(kra_json: str, *, created_at: datetime, krap9_table: Any) -> dict[str, Any]:
    """Insert row for Laravel `krap9` — same fields as ``KRAP9::create((array) $payroll->kra)`` on finalize."""
    p9 = json.loads(kra_json)
    fm_raw = p9.get("for_month")
    if isinstance(fm_raw, str):
        for_month = date.fromisoformat(fm_raw[:10])
    elif isinstance(fm_raw, date):
        for_month = fm_raw
    elif isinstance(fm_raw, datetime):
        for_month = fm_raw.date()
    else:
        raise ValueError("kra JSON missing for_month")

    nc = p9.get("non_cash", "[]")
    if not isinstance(nc, str):
        nc = json.dumps(nc, default=str)

    raw: dict[str, Any] = {
        "employee_id": int(p9["employee_id"]),
        "for_month": for_month,
        "basic_salary": float(p9.get("basic_salary") or 0),
        "non_cash": nc,
        "quarters": float(p9.get("quarters") or 0),
        "nssf": float(p9.get("nssf") or 0),
        "tax_charged": float(p9.get("tax_charged") or 0),
        "prescribed_rate": float(p9.get("prescribed_rate") or 0),
        "relief": float(p9.get("relief") or 0),
        "paye": float(p9.get("paye") or 0),
        "created_at": created_at,
        "updated_at": created_at,
    }
    return {k: v for k, v in raw.items() if k in krap9_table.c}


