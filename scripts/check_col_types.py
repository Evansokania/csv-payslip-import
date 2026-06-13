"""Show payrolls column types for shif / AHL."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
db = os.getenv("MYSQL_DATABASE")
url = f"mysql+pymysql://root:@127.0.0.1:3306/{db}?charset=utf8mb4"
eng = create_engine(url)
insp = inspect(eng)
for c in insp.get_columns("payrolls"):
    if c["name"] in ("shif", "affordable_housing_levy", "nhif", "nssf"):
        print(c["name"], c["type"])
