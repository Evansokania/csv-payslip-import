import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
db = os.getenv("MYSQL_DATABASE")
url = f"mysql+pymysql://root:@127.0.0.1:3306/{db}?charset=utf8mb4"
eng = create_engine(url)
with eng.connect() as c:
    print("allowances:")
    for r in c.execute(text("SELECT id,name,in_common_paye,tax_rate,taxable,in_basic FROM allowances")).mappings():
        print(dict(r))
    print("deductions:")
    for r in c.execute(text("SELECT id,name,included_in_costing_report FROM deductions")).mappings():
        print(dict(r))
    print("employees relief:", c.execute(text("SELECT id,relief,retirement_contribution,mortgage_relief,kra_pin FROM employees WHERE id=1")).mappings().first())
