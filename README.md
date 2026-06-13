# CSV → Payslip Import (developer tool)

Connects to the **same MySQL tenant database** as [Hr-Genie-Payroll](https://github.com/) (`C:\laragon\www\Hr-Genie-Payroll`). On startup, if the `payroll_import_*` tables are missing, **this app creates them** (SQLAlchemy DDL). You do **not** need to change Laravel solely to get those tables.

**Requirements:** the database must already contain normal tenant payroll tables this UI reads (`employees`, `allowances`, `deductions`, `company_profiles`, etc.). Only the import extension tables are auto-created.

## Laragon environments

| Path | Role |
|------|------|
| `C:\laragon\www\Hr-Genie-Payroll` | Laravel app (optional: run same migration there to keep schema in sync) |
| `C:\laragon\www\csv-payslip-import` | This FastAPI UI |

MySQL is typically **Laragon** `127.0.0.1:3306`, user `root`, empty password (local). Tenant DB name matches Stancl prefix `tenant_prl_*` from central `tenants` table, or any database that has `employees` and related payroll data.

**Windows environment variables:** If `MYSQL_DATABASE` or `DATABASE_URL` is set in User/System environment variables (even to an empty value), it can block values from `.env`. This app loads `.env` with `override=True` so the project file wins; if anything still looks wrong, remove stray `MYSQL_*` / `DATABASE_URL` entries from Windows.

## Setup

1. **Optional — Laravel:** migrate a tenant DB if you want the import tables created by Artisan as well:

   ```bash
   cd C:\laragon\www\Hr-Genie-Payroll
   php artisan tenants:migrate --tenants=<tenant_id>
   ```

2. Copy env and install Python deps:
   ```bash
   cd C:\laragon\www\csv-payslip-import
   copy .env.example .env
   # edit MYSQL_DATABASE to your tenant database name
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Run:

   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8890 --reload
   ```

4. Open **http://127.0.0.1:8890**

## Shareable Windows build (.exe)

You can ship a **folder** (onedir) so teammates do not need Python installed.

1. From the project root, install build deps (once):

   ```powershell
   cd C:\laragon\www\csv-payslip-import
   .\.venv\Scripts\activate
   pip install -r requirements.txt -r requirements-build.txt
   ```

2. Build:

   ```powershell
   .\scripts\build_exe.ps1
   ```

   Or manually: `pyinstaller --noconfirm csv-payslip-import.spec`

3. **Share** the whole `dist\csv-payslip-import\` directory (zip it). The runnable file is `csv-payslip-import.exe`.

4. **On each machine:** copy `.env.example` to `.env` in that **same folder as the .exe**, edit `MYSQL_*` / `DATABASE_URL`, then double-click the exe (or run it from a terminal). A console window stays open while the server runs; open **http://127.0.0.1:8890** (or whatever `PORT` is in `.env`).

**Note:** Antivirus may flag or slow first launch of PyInstaller bundles; the exe is not code-signed. MySQL must be reachable from that PC (same rules as the Python app).

## Flow

1. **Upload** CSV + payroll month → creates `payroll_import_runs` + parses rows.  
2. **Parse** validates every `PAYROLL NO` exists in `employees`.  
3. **Map columns** (auto-suggested; edit POST to adjust).  
4. **Build snapshot** → CSV amounts frozen in `payroll_import_snapshots`.  
5. **Preview payslip** per employee (CSV as source of truth).  
6. **Push to Laravel /payroll** → inserts one row per employee into Laravel’s `payrolls` table (`filter` = `CSV import · run {id}`) so they appear on HR Genie **Payroll** for that month (`/payroll/0?sub=mm-yyyy`). Re-push deletes only rows for that same filter + `payroll_date`, then re-inserts.

## Security

- Bind **127.0.0.1** only.  
- Use a **read/write MySQL user** scoped to the tenant DB; never commit `.env`.

## Troubleshooting

- **`/` returns 500** — after pulling the latest `main.py`, open `/` again: the response is an HTML error page with a **full traceback** (not a blank “Internal Server Error”). Try `http://127.0.0.1:8890/api/home-state` (JSON, no Jinja): if `/health` is OK but this is not, MySQL cannot read `payroll_import_runs` (missing table or permissions). Also use `/diag` (no DB). If `/diag` is 404, you are not running this repo’s `app.main:app` from `csv-payslip-import` (check `/docs` for routes).
- **Stale config** — `get_settings()` is cached; after editing `.env`, restart the process (or bump `APP_TITLE` to confirm reload picked up changes).
