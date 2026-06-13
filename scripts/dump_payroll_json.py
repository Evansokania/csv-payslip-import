"""Dump full JSON fields for Jan vs Jun comparison."""
import json, os, sys
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
db = os.getenv("MYSQL_DATABASE")
url = f"mysql+pymysql://{os.getenv('MYSQL_USER','root')}:{os.getenv('MYSQL_PASSWORD','')}@{os.getenv('MYSQL_HOST')}:{os.getenv('MYSQL_PORT')}/{db}?charset=utf8mb4"
eng = create_engine(url)
eid = 1

with eng.connect() as c:
    for pd, label in [("2026-01-31", "JAN"), ("2026-06-30", "JUN")]:
        r = c.execute(text("SELECT * FROM payrolls WHERE employee_id=:e AND payroll_date=:pd LIMIT 1"), {"e": eid, "pd": pd}).mappings().first()
        d = dict(r)
        print(f"\n======== {label} ========")
        for col in ("allowances", "deductions", "payslip", "paye_data"):
            v = d.get(col)
            if v:
                try:
                    obj = json.loads(v) if isinstance(v, str) else v
                    print(f"\n{col}:")
                    print(json.dumps(obj, indent=2)[:6000])
                except Exception as ex:
                    print(col, ex)

    snap = c.execute(text("""
        SELECT earnings_lines, deduction_lines, computed
        FROM payroll_import_snapshots
        WHERE employee_id=:e AND import_run_id=1
    """), {"e": eid}).mappings().first()
    if snap:
        print("\n======== JAN SNAPSHOT ========")
        for k in ("earnings_lines", "deduction_lines", "computed"):
            v = snap[k]
            if isinstance(v, str):
                v = json.loads(v)
            print(f"\n{k}:")
            print(json.dumps(v, indent=2)[:5000])
