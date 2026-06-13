"""Inspect shif / AHL scalar column storage."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
db = os.getenv("MYSQL_DATABASE")
url = f"mysql+pymysql://root:@127.0.0.1:3306/{db}?charset=utf8mb4"
eng = create_engine(url)

with eng.connect() as conn:
    rows = conn.execute(
        text(
            """
            SELECT payroll_date, filter, shif, affordable_housing_levy, nhif,
                   CAST(shif AS CHAR) AS shif_raw,
                   CAST(affordable_housing_levy AS CHAR) AS ahl_raw
            FROM payrolls
            WHERE employee_id = 1
              AND payroll_date IN ('2026-01-31', '2026-06-30')
            """
        )
    ).mappings().all()
    for r in rows:
        print(dict(r))
