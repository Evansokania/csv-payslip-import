"""Full JSON dump Jan vs Jun for one employee."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
db = os.getenv("MYSQL_DATABASE")
url = (
    f"mysql+pymysql://{os.getenv('MYSQL_USER', 'root')}:{os.getenv('MYSQL_PASSWORD', '')}"
    f"@{os.getenv('MYSQL_HOST', '127.0.0.1')}:{os.getenv('MYSQL_PORT', '3306')}/{db}?charset=utf8mb4"
)
eng = create_engine(url)
eid = int(sys.argv[1]) if len(sys.argv) > 1 else 1

with eng.connect() as c:
    for pd, label in [("2026-01-31", "JAN"), ("2026-06-30", "JUN")]:
        r = c.execute(
            text(
                "SELECT filter, allowances, deductions, payslip, paye_data, kra, nssf_data "
                "FROM payrolls WHERE employee_id=:e AND payroll_date=:pd LIMIT 1"
            ),
            {"e": eid, "pd": pd},
        ).mappings().first()
        print("====", label, r["filter"], "====")
        for col in ("allowances", "deductions", "paye_data", "kra", "nssf_data"):
            v = r[col]
            if v is None:
                print(f"{col}: null\n")
                continue
            obj = json.loads(v) if isinstance(v, str) else v
            print(f"{col}:")
            print(json.dumps(obj, indent=2))
            print()
        ps = json.loads(r["payslip"]) if isinstance(r["payslip"], str) else r["payslip"]
        for nested in ("allowances", "deductions", "paye", "nssf", "untaxable"):
            if nested not in ps:
                continue
            print(f"payslip.{nested}:")
            inner = ps[nested]
            if isinstance(inner, str):
                inner = json.loads(inner)
            print(json.dumps(inner, indent=2))
            print()
