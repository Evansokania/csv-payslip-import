"""Compare CSV import vs native payroll row shapes (readonly)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

host = os.getenv("MYSQL_HOST", "127.0.0.1")
port = os.getenv("MYSQL_PORT", "3306")
user = os.getenv("MYSQL_USER", "root")
password = os.getenv("MYSQL_PASSWORD", "")
db = os.getenv("MYSQL_DATABASE", "")
url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}?charset=utf8mb4"

JAN = "2026-01-31"
JUN = "2026-06-30"


def _parse_json(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return val


def _shape(obj, depth=0, max_depth=3):
    if depth > max_depth:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1, max_depth) for k, v in list(obj.items())[:20]}
    if isinstance(obj, list):
        if not obj:
            return []
        return [_shape(obj[0], depth + 1, max_depth), f"...({len(obj)} items)"]
    if isinstance(obj, str) and len(obj) > 80:
        return obj[:77] + "..."
    return obj


def main() -> int:
    eng = create_engine(url)
    insp = inspect(eng)
    cols = [c["name"] for c in insp.get_columns("payrolls")]

    with eng.connect() as conn:
        print("=== payroll_date / filter counts ===")
        rows = conn.execute(
            text(
                """
                SELECT payroll_date, filter, COUNT(*) AS cnt,
                       SUM(finalized) AS finalized_cnt
                FROM payrolls
                WHERE payroll_date IN (:jan, :jun)
                GROUP BY payroll_date, filter
                ORDER BY payroll_date, filter
                """
            ),
            {"jan": JAN, "jun": JUN},
        ).mappings().all()
        for r in rows:
            print(dict(r))

        # Pick one employee present in both months if possible
        pair = conn.execute(
            text(
                """
                SELECT j.employee_id, e.payroll_number
                FROM payrolls j
                INNER JOIN payrolls g ON g.employee_id = j.employee_id AND g.payroll_date = :jun
                INNER JOIN employees e ON e.id = j.employee_id
                WHERE j.payroll_date = :jan
                ORDER BY j.employee_id
                LIMIT 1
                """
            ),
            {"jan": JAN, "jun": JUN},
        ).mappings().first()

        if not pair:
            print("No employee with both Jan and Jun payroll")
            return 1

        eid = int(pair["employee_id"])
        print(f"\n=== Sample employee {pair['payroll_number']} (id={eid}) ===")

        for label, pd in (("JAN_CSV", JAN), ("JUN_NATIVE", JUN)):
            row = conn.execute(
                text("SELECT * FROM payrolls WHERE employee_id = :eid AND payroll_date = :pd ORDER BY id LIMIT 1"),
                {"eid": eid, "pd": pd},
            ).mappings().first()
            if not row:
                print(f"{label}: missing")
                continue
            d = dict(row)
            print(f"\n--- {label} filter={d.get('filter')} finalized={d.get('finalized')} ---")
            scalars = [
                "basic_pay", "gross_pay", "net_pay", "paye_due", "third_rule", "for_rate",
                "total_deductions", "house_allowance", "transport_allowance", "overtime",
                "other_allowances", "other_earning", "personal_relief", "total_relief",
                "nssf", "nhif", "affordable_housing_levy", "ahl_gross", "ahl_relief", "shif",
                "department_id", "beneficiary_name",
            ]
            for k in scalars:
                if k in d:
                    print(f"  {k}: {d[k]}")

            for json_col in ("allowances", "deductions", "advances", "loans", "kra", "payslip", "paye_data", "nssf_data", "nhif_data"):
                if json_col not in d or d[json_col] is None:
                    continue
                parsed = _parse_json(d[json_col])
                print(f"\n  [{json_col}] shape:")
                print(json.dumps(_shape(parsed), indent=2, default=str)[:4000])

        # Column presence diff on sample rows
        print("\n=== Non-null column keys (sample Jan vs Jun same employee) ===")
        samples = {}
        for label, pd in (("jan", JAN), ("jun", JUN)):
            row = conn.execute(
                text("SELECT * FROM payrolls WHERE employee_id = :eid AND payroll_date = :pd LIMIT 1"),
                {"eid": eid, "pd": pd},
            ).mappings().first()
            samples[label] = {k for k, v in dict(row).items() if v is not None and v != "" and v != 0}
        only_jan = sorted(samples["jan"] - samples["jun"])
        only_jun = sorted(samples["jun"] - samples["jan"])
        print("only_jan:", only_jan[:30])
        print("only_jun:", only_jun[:30])

    return 0


if __name__ == "__main__":
    sys.exit(main())
