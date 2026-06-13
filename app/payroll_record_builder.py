"""Build native-shaped Laravel ``payrolls`` row dicts from CSV import snapshots."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import PayrollImportSnapshot
from app.payroll_format import (
    _allowance_earnings_for_json,
    _basic_from_earnings,
    _computed_lookup,
    _costing_report_from_deductions,
    _earnings_bucket_totals,
    _fetch_prescribed_loan_rate,
    _laravel_deductions_column,
    _non_cash_int,
    _paye_slip_object,
    _paye_slip_object_minimal,
    _resolve_total_deductions,
    _statutory_amounts,
    apply_nssf_tier_derivation,
    build_kra_p9_json,
    build_laravel_payslip_json,
    normalize_snapshot_lines,
)


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


def build_native_allowances_json(
    earnings_lines: list[dict[str, Any]],
    *,
    insurance_relief: float = 0.0,
    mortgage_relief: float = 0.0,
    pension_contribution: float = 0.0,
) -> str:
    """
    ``payrolls.allowances`` shape aligned with ``PayrollCalculatorDynamic::calculate()``.
    Basic pay is stored on ``basic_pay`` — not repeated in this JSON array.
    """
    items: list[dict[str, Any]] = []
    for line in _allowance_earnings_for_json(earnings_lines):
        amt = float(line.get("amount") or 0)
        if amt == 0:
            continue
        name = str(line.get("name") or line.get("label") or "Earning").strip()
        taxable = line.get("taxable", 1)
        try:
            taxable_i = int(taxable)
        except (TypeError, ValueError):
            taxable_i = 1 if taxable else 0
        tax_rate = float(line.get("tax_rate") or 100)
        tax_amount = line.get("tax_amount")
        if tax_amount is None:
            tax_amount = amt if taxable_i else 0.0
        tax_amount = round(float(tax_amount), 2)
        net_amount = round(amt - tax_amount, 2)

        if line.get("allowance_id") is not None:
            item = {
                "allowance_id": int(line["allowance_id"]),
                "in_common_paye": int(line.get("in_common_paye") or 1),
                "in_basic": int(line.get("in_basic") or 0),
                "non_cash": _non_cash_int(line.get("non_cash")),
                "taxable": taxable_i,
                "tax_rate": tax_rate,
                "tax_amount": tax_amount,
                "name": name,
                "amount": net_amount,
                "allowance": round(amt, 2),
            }
        else:
            item = {
                "in_basic": int(line.get("in_basic") or 0),
                "non_cash": _non_cash_int(line.get("non_cash")),
                "taxable": taxable_i,
                "tax_rate": tax_rate,
                "tax_amount": tax_amount,
                "name": name,
                "amount": net_amount,
                "allowance": round(amt, 2),
            }
        items.append(item)

    items.append(
        {
            "name": "reliefs",
            "items": [
                {"non_cash": False, "name": "Insurance Relief", "amount": round(insurance_relief, 2)},
                {"non_cash": False, "name": "Mortgage Relief", "amount": round(mortgage_relief, 2)},
                {
                    "non_cash": False,
                    "name": "Other Pension Contribution",
                    "amount": round(pension_contribution, 2),
                },
            ],
        }
    )
    return json.dumps(items, default=str)


def build_native_deductions_json(
    deduction_lines: list[dict[str, Any]],
    stat: dict[str, Any],
    *,
    insurance_relief: float = 0.0,
) -> str:
    """``payrolls.deductions`` with native ordering and relief shapes."""
    rows = _laravel_deductions_column(stat, deduction_lines, insurance_relief=insurance_relief)
    return json.dumps(rows, default=str)


def build_payroll_record(
    session: Session,
    *,
    snap: PayrollImportSnapshot,
    employee: dict[str, Any],
    payroll_date: date,
    filter_label: str,
    prescribed_rate: float,
    now: datetime,
    col_names_lower: set[str],
) -> dict[str, Any]:
    """Build a complete ``payrolls`` insert/update payload from a snapshot."""
    earnings = _as_list(snap.earnings_lines)
    deductions = _as_list(snap.deduction_lines)
    computed = _as_dict(snap.computed)
    earnings, deductions, computed = normalize_snapshot_lines(earnings, deductions, computed)

    basic = _basic_from_earnings(earnings)
    gross = _computed_lookup(computed, "GROSS AMOUNT", "GROSS PAY", "GROSS")
    net = _computed_lookup(computed, "NETTPAY", "NET PAY", "NET SALARY", "NET", "NETPAY")
    third = _computed_lookup(computed, "A THIRD RULE", "THIRD RULE")
    stat = _statutory_amounts(computed, payroll_date)
    apply_nssf_tier_derivation(session, payroll_date, computed, earnings, stat)
    paye_due = float(stat["paye_due"] or 0)
    personal_relief = float(stat["personal_relief"] or 0)
    insurance_relief = float(stat.get("insurance_relief") or 0)
    tax_charged = float(stat["tax_charged"] or 0)

    if basic <= 0 and gross > 0:
        basic = gross
    if gross <= 0:
        gross = basic + sum(
            float(x.get("amount") or 0) for x in _allowance_earnings_for_json(earnings)
        )

    first_name = str(employee.get("first_name") or "")
    last_name = str(employee.get("last_name") or "")
    payslip_obj = build_laravel_payslip_json(
        statutory=stat,
        first_name=first_name,
        last_name=last_name,
        payroll_number=str(employee.get("payroll_number") or ""),
        identification_type=employee.get("identification_type"),
        identification_number=employee.get("identification_number"),
        kra_pin=employee.get("kra_pin"),
        payroll_month_end=payroll_date,
        earnings_lines=earnings,
        deduction_lines=deductions,
        computed=computed,
    )

    if net <= 0:
        net = float(payslip_obj.get("net_pay") or 0)

    nssf_amt = float(stat["nssf"] or 0)
    t1, t2 = float(stat["nssf_tier_1"] or 0), float(stat["nssf_tier_2"] or 0)
    nssf_data_obj: dict[str, Any] = {"name": "NSSF", "amount": round(nssf_amt, 2)}
    if nssf_amt > 0:
        nssf_data_obj["tier_1"] = round(t1, 2)
        nssf_data_obj["tier_2"] = round(t2, 2)

    nhif_amt = float(stat["nhif"] or 0)
    nhif_data_obj = {"name": "NHIF", "amount": round(nhif_amt, 2)} if nhif_amt > 0 else {}

    if personal_relief > 0 or insurance_relief > 0 or tax_charged > paye_due:
        paye_data_obj = _paye_slip_object(
            tax_charged=tax_charged,
            personal_relief=personal_relief,
            paye_due=paye_due,
            insurance_relief=insurance_relief,
        )
    else:
        paye_data_obj = _paye_slip_object_minimal(paye_due)

    total_deductions_val = _resolve_total_deductions(computed, gross, net, stat, deductions, paye_due)
    bucket = _earnings_bucket_totals(earnings)

    pension_contribution = float(employee.get("retirement_contribution") or 0)
    if pension_contribution <= 0:
        for line in deductions:
            if str(line.get("name") or line.get("label") or "").strip() == "Retirement Contribution":
                pension_contribution = float(line.get("amount") or 0)
                break

    mortgage_relief = float(employee.get("mortgage_relief") or 0)
    deductions_rows = json.loads(
        build_native_deductions_json(deductions, stat, insurance_relief=insurance_relief)
    )
    costing_json, costing_total = _costing_report_from_deductions(deductions_rows)

    kra_json = build_kra_p9_json(
        employee_id=int(snap.employee_id),
        for_month=payroll_date,
        basic_salary=float(basic),
        stat=stat,
        prescribed_rate=prescribed_rate,
    )

    payload: dict[str, Any] = {
        "employee_id": int(snap.employee_id),
        "payroll_date": payroll_date,
        "finalized": 0,
        "sage": 0,
        "kra": kra_json,
        "basic_pay": round(float(basic), 2),
        "net_pay": round(float(net), 2),
        "third_rule": round(float(third), 2),
        "beneficiary_name": str(employee.get("beneficiary_name") or "")[:255],
        "for_rate": "30 Days",
        "filter": filter_label[:255] if len(filter_label) > 255 else filter_label,
        "deductions": json.dumps(deductions_rows, default=str),
        "allowances": build_native_allowances_json(
            earnings,
            insurance_relief=insurance_relief,
            mortgage_relief=mortgage_relief,
            pension_contribution=pension_contribution,
        ),
        "advances": "[]",
        "loans": "[]",
        "is_terminated": 0,
        "created_at": now,
        "updated_at": now,
    }

    def _has(name: str) -> bool:
        return name.lower() in col_names_lower

    if _has("payslip"):
        payload["payslip"] = json.dumps(payslip_obj, default=str)
    if _has("gross_pay"):
        payload["gross_pay"] = round(float(gross), 2)
    if _has("paye_due"):
        payload["paye_due"] = round(paye_due, 2)
    if _has("department_id"):
        dept_id = employee.get("department_id")
        payload["department_id"] = int(dept_id) if dept_id is not None else None
    if _has("total_deductions"):
        payload["total_deductions"] = total_deductions_val
    if _has("paye_data"):
        payload["paye_data"] = json.dumps(paye_data_obj, default=str)
    if _has("nhif"):
        payload["nhif"] = round(nhif_amt, 2)
    if _has("nhif_data") and nhif_data_obj:
        payload["nhif_data"] = json.dumps(nhif_data_obj, default=str)
    elif _has("nhif_data"):
        payload["nhif_data"] = None
    if _has("nssf"):
        payload["nssf"] = round(nssf_amt, 2) if nssf_amt > 0 else None
    if _has("nssf_data") and nssf_amt > 0:
        payload["nssf_data"] = json.dumps(nssf_data_obj, default=str)
    if _has("personal_relief"):
        payload["personal_relief"] = round(personal_relief, 2) if personal_relief > 0 else None
    if _has("total_relief"):
        tr = float(stat.get("total_relief") or 0)
        payload["total_relief"] = round(tr, 2) if tr > 0 else None
    if _has("affordable_housing_levy"):
        # Native payroll stores 0 in this scalar (amount lives in deductions / payslip untaxable).
        payload["affordable_housing_levy"] = 0.0
    if _has("ahl_gross"):
        payload["ahl_gross"] = round(float(gross), 2) if gross > 0 else None
    if _has("ahl_relief"):
        ar = float(stat.get("ahl_relief") or 0)
        payload["ahl_relief"] = round(ar, 2)
    if _has("shif"):
        # ``payrolls.shif`` is TINYINT in tenant DB; native rows keep 0 (amount is in deductions JSON).
        payload["shif"] = 0
    if _has("deductions_in_costing_report"):
        payload["deductions_in_costing_report"] = costing_json
    if _has("costing_report_deductions_total"):
        payload["costing_report_deductions_total"] = costing_total

    for col_name, val in bucket.items():
        if _has(col_name):
            payload[col_name] = round(val, 2)

    other_earning = round(float(gross) - float(basic), 2)
    if _has("other_earning") and other_earning > 0:
        payload["other_earning"] = other_earning

    return payload


def fetch_prescribed_rate(session: Session) -> float:
    return _fetch_prescribed_loan_rate(session)
