"""Re-push a CSV import run to Laravel payrolls (for testing after format fixes)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

from app.laravel_payroll_push import push_snapshots_to_laravel_payrolls  # noqa: E402

run_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
db = os.getenv("MYSQL_DATABASE")
url = f"mysql+pymysql://root:@127.0.0.1:3306/{db}?charset=utf8mb4"
engine = create_engine(url)

with Session(engine) as session:
    written, flt, report = push_snapshots_to_laravel_payrolls(session, run_id)
    session.commit()
    print(f"Pushed {written} rows with filter={flt!r}")
    print(report)
