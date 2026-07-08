"""FastAPI entry — CSV payslip import UI (developer tool)."""

from __future__ import annotations

import html
import logging
import traceback
from contextlib import asynccontextmanager

# `.env` is loaded in `app.config` (correct path for PyInstaller: next to `.exe`).
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from app.config import BASE_DIR, azure_openai_configured, get_settings
from app.column_map_ai import apply_azure_classifications_to_maps, suggest_column_maps_with_azure
from app.database import dispose_engine, get_db, read_session
from app.import_service import (
    DEFAULT_PERSONAL_RELIEF_KES,
    apply_column_map_overrides,
    build_snapshots,
    ingest_csv,
    payroll_month_last_day,
    rebuild_column_maps,
    replace_run_csv,
)
from app.laravel_payroll_push import push_snapshots_to_laravel_payrolls
from app.models import (
    Allowance,
    Deduction,
    PayrollImportColumnMap,
    PayrollImportRun,
    PayrollImportSnapshot,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB is lazy (first request): if MySQL is not up yet, Uvicorn still binds so
    # /diag and /health can respond instead of failing startup entirely.
    yield
    dispose_engine()


app = FastAPI(title=get_settings().app_title, lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/health")
def health():
    """Avoid Depends(get_db): resolving the dependency calls get_engine() outside the try block."""
    try:
        with read_session() as db:
            db.execute(text("SELECT 1"))
        return {"ok": True, "database": True}
    except Exception as e:
        return {"ok": False, "database": False, "error": str(e)[:200]}


@app.get("/diag", response_class=HTMLResponse)
@app.get("/_diag", response_class=HTMLResponse)
def diag():
    """No DB — if this works but / fails, the problem is in the home route or DB queries."""
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>diag</title></head>"
        "<body><p>diag ok</p><p><a href='/'>home</a></p></body></html>",
        media_type="text/html; charset=utf-8",
    )


@app.get("/api/home-state")
def api_home_state():
    """Minimal DB check (no Jinja). Compare with GET / if home still errors."""
    try:
        with read_session() as db:
            c = db.execute(text("SELECT COUNT(*) AS c FROM payroll_import_runs")).scalar()
        return {"ok": True, "payroll_import_runs_count": int(c or 0)}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_type": type(e).__name__}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    try:
        return _home_page_response(request)
    except Exception:
        logger.exception("GET / failed")
        tb = html.escape(traceback.format_exc())
        return HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Home error</title>"
            "<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:1.5rem}"
            "pre{white-space:pre-wrap;background:#020617;border:1px solid #334155;padding:1rem;border-radius:8px;font-size:12px}"
            "a{color:#34d399}</style></head><body>"
            "<h1>Home page crashed</h1>"
            "<p>Copy everything below if you need help. Also try "
            "<a href='/api/home-state'>/api/home-state</a>, <a href='/health'>/health</a>, "
            "<a href='/diag'>/diag</a>.</p>"
            f"<pre>{tb}</pre></body></html>",
            status_code=500,
            media_type="text/html; charset=utf-8",
        )


def _home_page_response(request: Request) -> HTMLResponse:
    """Build the normal dashboard HTML (may raise)."""
    runs_out: list[dict[str, object]] = []
    snapshot_counts: dict[int, int] = {}

    def _rows_to_out(rows: list) -> None:
        nonlocal runs_out, snapshot_counts
        runs_out = []
        snapshot_counts = {}
        for row in rows:
            rid = int(row["id"])
            pm = row["payroll_month"]
            if pm is not None and hasattr(pm, "isoformat"):
                pm = pm.isoformat()
            elif pm is not None:
                pm = str(pm)
            else:
                pm = ""
            try:
                sc = row["snapshot_count"]
            except KeyError:
                sc = 0
            n = int(sc or 0)
            snapshot_counts[rid] = n
            runs_out.append(
                {
                    "id": rid,
                    "payroll_month": pm,
                    "status": str(row["status"] or ""),
                    "row_count": int(row["row_count"] or 0),
                    "original_filename": row["original_filename"],
                    "csv_sha256": row["csv_sha256"],
                }
            )

    with read_session() as db:
        try:
            rows = db.execute(
                text(
                    """
                    SELECT
                        r.id,
                        r.payroll_month,
                        r.status,
                        r.row_count,
                        r.original_filename,
                        r.csv_sha256,
                        (SELECT COUNT(*) FROM payroll_import_snapshots s WHERE s.import_run_id = r.id) AS snapshot_count
                    FROM payroll_import_runs r
                    ORDER BY r.id DESC
                    LIMIT 25
                    """
                )
            ).mappings().all()
            _rows_to_out(rows)
        except Exception:
            db.rollback()
            try:
                rows = db.execute(
                    text(
                        """
                        SELECT id, payroll_month, status, row_count, original_filename, csv_sha256
                        FROM payroll_import_runs
                        ORDER BY id DESC
                        LIMIT 25
                        """
                    )
                ).mappings().all()
                _rows_to_out([{**dict(r), "snapshot_count": 0} for r in rows])
                run_ids = [int(x["id"]) for x in runs_out]
                if run_ids:
                    try:
                        agg = db.execute(
                            text(
                                """
                                SELECT import_run_id, COUNT(*) AS c
                                FROM payroll_import_snapshots
                                WHERE import_run_id IN :ids
                                GROUP BY import_run_id
                                """
                            ).bindparams(bindparam("ids", expanding=True)),
                            {"ids": run_ids},
                        ).all()
                        snapshot_counts = {int(rid): int(c) for rid, c in agg}
                    except Exception:
                        db.rollback()
                        snapshot_counts = {rid: 0 for rid in run_ids}
            except Exception:
                db.rollback()
                runs_out = []
                snapshot_counts = {}

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "runs": runs_out,
            "snapshot_counts": snapshot_counts,
            "settings": settings,
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(PayrollImportRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    maps = db.scalars(select(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id)).all()
    snap_count = db.execute(
        text("SELECT COUNT(*) FROM payroll_import_snapshots WHERE import_run_id = :rid"),
        {"rid": run_id},
    ).scalar()
    allowances = db.scalars(select(Allowance).order_by(Allowance.name)).all()
    deductions = db.scalars(select(Deduction).order_by(Deduction.name)).all()
    preview_rows = db.execute(
        text(
            """
            SELECT s.employee_id, e.payroll_number, e.first_name, e.last_name
            FROM payroll_import_snapshots s
            JOIN employees e ON e.id = s.employee_id
            WHERE s.import_run_id = :rid
            ORDER BY e.payroll_number
            """
        ),
        {"rid": run_id},
    ).mappings().all()
    previews = [
        {
            "employee_id": int(r["employee_id"]),
            "payroll_number": r["payroll_number"] or "",
            "first_name": r["first_name"] or "",
            "last_name": r["last_name"] or "",
        }
        for r in preview_rows
    ]
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "settings": get_settings(),
            "run": run,
            "maps": maps,
            "snap_count": int(snap_count or 0),
            "allowances": allowances,
            "deductions": deductions,
            "previews": previews,
            "ai_suggest_enabled": azure_openai_configured(get_settings()),
            "personal_relief_default": DEFAULT_PERSONAL_RELIEF_KES,
        },
    )


@app.post("/runs/upload")
async def run_upload(
    request: Request,
    payroll_month: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        pm = payroll_month_last_day(payroll_month)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    try:
        run = ingest_csv(session=db, payroll_month=pm, file_content=content, original_filename=file.filename or "upload.csv")
    except UnicodeDecodeError as e:
        raise HTTPException(400, "File must be UTF-8") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return RedirectResponse(url=f"/runs/{run.id}", status_code=303)


@app.post("/runs/{run_id}/map/ai-suggest")
def map_ai_suggest(run_id: int, db: Session = Depends(get_db)):
    """Use Azure OpenAI to classify CSV headers (earning vs deduction vs computed, master links)."""
    run = db.get(PayrollImportRun, run_id)
    if not run or run.status == "failed":
        raise HTTPException(400, "Invalid run")
    settings = get_settings()
    if not azure_openai_configured(settings):
        raise HTTPException(
            503,
            "Configure AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, and AZURE_OPENAI_DEPLOYMENT_NAME in .env",
        )
    try:
        out = suggest_column_maps_with_azure(db, settings, run_id)
        n = apply_azure_classifications_to_maps(db, run_id, out["classifications"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        logger.exception("AI column suggest failed")
        raise HTTPException(500, str(e)) from e
    return RedirectResponse(url=f"/runs/{run_id}?ai_suggest={n}", status_code=303)


@app.post("/runs/{run_id}/map/rebuild")
def map_rebuild(run_id: int, db: Session = Depends(get_db)):
    """Re-run header heuristics / auto-classification (same raw CSV rows)."""
    run = db.get(PayrollImportRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if not run.row_count:
        raise HTTPException(400, "Run has no CSV rows — upload or replace the CSV first.")
    n = rebuild_column_maps(db, run_id)
    return RedirectResponse(url=f"/runs/{run_id}?mapped={n}", status_code=303)


@app.post("/runs/{run_id}/replace-csv")
async def replace_csv(
    run_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Replace stored CSV for this run; clears snapshots and rebuilds column map."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    try:
        run = replace_run_csv(
            session=db,
            run_id=run_id,
            file_content=content,
            original_filename=file.filename or "upload.csv",
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except UnicodeDecodeError as e:
        raise HTTPException(400, "File must be UTF-8") from e
    return RedirectResponse(url=f"/runs/{run_id}?replaced={run.row_count}", status_code=303)


@app.post("/runs/{run_id}/map/save")
async def map_save(request: Request, run_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    overrides: dict[str, dict] = {}
    maps = db.scalars(select(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id)).all()
    for m in maps:
        prefix = f"m_{m.id}_"
        role = form.get(prefix + "role")
        if role is None:
            continue
        aid = form.get(prefix + "allowance_id")
        did = form.get(prefix + "deduction_id")
        overrides[m.csv_header_normalized] = {
            "role": str(role),
            "allowance_id": aid if aid not in (None, "") else None,
            "deduction_id": did if did not in (None, "") else None,
        }
    apply_column_map_overrides(db, run_id, overrides)
    return RedirectResponse(url=f"/runs/{run_id}?saved=1", status_code=303)


@app.get("/runs/{run_id}/snapshot")
def snapshot_get_redirect(run_id: int):
    """Opening this URL in a browser sends GET; snapshot build is POST-only from the run page."""
    return RedirectResponse(url=f"/runs/{run_id}?snapshot_post_only=1", status_code=303)


@app.post("/runs/{run_id}/snapshot")
def snapshot_build(
    run_id: int,
    db: Session = Depends(get_db),
    personal_relief_kes: float = Form(DEFAULT_PERSONAL_RELIEF_KES, ge=0),
):
    run = db.get(PayrollImportRun, run_id)
    if not run or run.status == "failed":
        raise HTTPException(400, "Invalid run")
    n = build_snapshots(db, run_id, personal_relief_kes=personal_relief_kes)
    return RedirectResponse(url=f"/runs/{run_id}?snapshots={n}#payslips", status_code=303)


@app.post("/runs/{run_id}/push-to-laravel-payroll")
def push_to_laravel_payroll(run_id: int, db: Session = Depends(get_db)):
    """Insert rows into Laravel `payrolls` for this import run (same MySQL DB)."""
    try:
        n, flt, integrity = push_snapshots_to_laravel_payrolls(db, run_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"Push failed: {e!s}") from e
    from urllib.parse import quote
    import json

    extra = ""
    skipped_fin = len(integrity.get("skipped_finalized") or [])
    skipped_emp = len(integrity.get("skipped_missing_employee") or [])
    if skipped_fin or skipped_emp:
        extra = "&integrity=" + quote(json.dumps(integrity, default=str), safe="")

    return RedirectResponse(
        url=f"/runs/{run_id}?pushed={n}&filter_used={quote(flt, safe='')}{extra}",
        status_code=303,
    )


@app.get("/runs/{run_id}/preview/{employee_id}", response_class=HTMLResponse)
def payslip_preview(request: Request, run_id: int, employee_id: int, db: Session = Depends(get_db)):
    run = db.get(PayrollImportRun, run_id)
    if not run:
        raise HTTPException(404)
    snap = db.scalars(
        select(PayrollImportSnapshot).where(
            PayrollImportSnapshot.import_run_id == run_id,
            PayrollImportSnapshot.employee_id == employee_id,
        )
    ).first()
    if not snap:
        raise HTTPException(404, "No snapshot for this employee — build snapshots first.")
    emp = db.execute(
        text(
            "SELECT id, payroll_number, first_name, last_name, kra_pin, identification_number, identification_type "
            "FROM employees WHERE id = :id"
        ),
        {"id": employee_id},
    ).mappings().first()
    company = db.execute(text("SELECT * FROM company_profiles ORDER BY id ASC LIMIT 1")).mappings().first()

    # Compute the same payslip object that Push writes to `payrolls.payslip` and that
    # HR Genie's native blade renders, so the preview reflects the *accurate* payslip.
    payslip = _computed_payslip_for_preview(db, run=run, snap=snap, employee_id=employee_id)

    return templates.TemplateResponse(
        request,
        "payslip.html",
        {
            "settings": get_settings(),
            "run": run,
            "snap": snap,
            "employee": emp,
            "company": company,
            "payslip": payslip,
        },
    )


def _computed_payslip_for_preview(db: Session, *, run, snap, employee_id: int) -> dict | None:
    """Build the payslip object (native shape) for a snapshot; None if it can't be built.

    ``col_names_lower`` is empty because only the payslip JSON is needed here (it does
    not depend on the `payrolls` scalar columns), so no table reflection is required.
    """
    import json
    from datetime import datetime

    from app.payroll_format import _filter_value
    from app.payroll_integrity import fetch_employee_for_payroll, has_table, reflect_payrolls_table
    from app.payroll_record_builder import build_payroll_record, fetch_prescribed_rate

    try:
        emp_full = fetch_employee_for_payroll(db, int(employee_id))
        if not emp_full:
            return None
        payroll_date = run.payroll_month
        if isinstance(payroll_date, datetime):
            payroll_date = payroll_date.date()
        # Reflect the real payrolls columns so the payslip matches Push exactly; always
        # include "payslip" so the object is emitted even on a minimal schema.
        col_names_lower = {"payslip"}
        try:
            if has_table(db, "payrolls"):
                _, col_map = reflect_payrolls_table(db)
                col_names_lower |= set(col_map.keys())
        except Exception:
            pass
        payload = build_payroll_record(
            db,
            snap=snap,
            employee=emp_full,
            payroll_date=payroll_date,
            filter_label=_filter_value(run.id),
            prescribed_rate=fetch_prescribed_rate(db),
            now=datetime.utcnow(),
            col_names_lower=col_names_lower,
        )
        ps = json.loads(payload["payslip"])
        for key in ("allowances", "untaxable", "deductions", "paye", "nssf"):
            val = ps.get(key)
            if isinstance(val, str):
                try:
                    ps[key] = json.loads(val)
                except json.JSONDecodeError:
                    ps[key] = []
        # Expand any per-master detailed breakdowns (e.g. overtime OT1/OT2).
        for a in ps.get("allowances") or []:
            det = a.get("detailed") if isinstance(a, dict) else None
            if isinstance(det, str):
                try:
                    a["detailed"] = json.loads(det)
                except json.JSONDecodeError:
                    a["detailed"] = None
        return ps
    except Exception:
        logger.exception("payslip preview computation failed")
        return None


def main():
    """`python -m app.main` for local dev."""
    import sys

    import uvicorn

    s = get_settings()
    reload = not getattr(sys, "frozen", False)
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=reload)


if __name__ == "__main__":
    main()


# Mount static files last so route paths like `/`, `/health`, `/diag` are never shadowed.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
