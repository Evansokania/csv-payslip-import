# CSV/Excel → Payslip Import (developer tool)

A FastAPI tool that ingests a monthly payroll spreadsheet (**`.csv` or `.xlsx`**), maps its columns
to the tenant's allowance/deduction masters, and pushes the result into the **same MySQL tenant
database** used by [Hr-Genie-Payroll](https://github.com/) (`C:\laragon\www\Hr-Genie-Payroll`) — so
the imported month appears on HR Genie's **Payroll**, **payslips**, **P9 (krap9)**, and the
allowance/deduction/costing/scalar (`/payroll-report`) reports.

On startup, if the `payroll_import_*` tables are missing, **this app creates them** (SQLAlchemy DDL).
You do **not** need to change Laravel solely to get those tables.

**Requirements:** the database must already contain the normal tenant payroll tables this UI reads
(`employees`, `allowances`, `deductions`, `special_cat_names`, `company_profiles`, `payrolls`,
`krap9`, `salary_arrears`, etc.). Only the `payroll_import_*` extension tables are auto-created.

Built for a real BIDCO workload: **~900 employees/month**. The push path is batch-loaded and bulk-inserted
to keep a full month import at a few seconds.

---

## What it does (feature overview)

- **CSV *and* Excel input.** `.xlsx`/`.xlsm` are detected by magic bytes or extension and parsed with
  `openpyxl`; `.csv` is parsed as text. Banner/section rows (e.g. *Earnings*, *Statutory Deductions*)
  and summary/total rows are detected and handled, with the header row anchored on `PAYROLL NO`.
  See [app/tabular_parse.py](app/tabular_parse.py).
- **Header canonicalization.** Vendor headers are normalized to the tenant's vocabulary via a synonyms
  map — e.g. *Personnel Number → PAYROLL NO*, *Social Health Ins ACT → SHIF*,
  *EE NSSF Tier I/II → NSSF TIER 1/2*, *NSSF Vol Contribution → VOLUNTARY NSSF*,
  *EE NSSF Tier III → RETIREMENT CONTRIBUTION*. See [app/header_utils.py](app/header_utils.py).
- **NSSF tier handling.** Tier I/II are treated as statutory NSSF; **Tier III is a retirement
  (pension) contribution**, and **Voluntary NSSF is voluntary** — both routed as deductions and carried
  through to the payslip and P9.
- **Pension relief with cap.** Statutory + voluntary + pension contributions are allocated to relief
  with a **KES 30,000 pension deduction cap** (`allocate_pension_relief`), and `taxable_pay = gross −
  total_non_tax`. See [app/payroll_format.py](app/payroll_format.py).
- **Classification-driven routing.** A stored wage-type classification (per header: *Earning /
  Statutory Deduction / Other Deduction / Memo / Calculated*) decides whether a money column is an
  earning, a deduction, a computed statutory value, or an informational/memo column that must be
  excluded. This future-proofs routing against banner-layout changes and fixes ambiguous columns
  (e.g. *Variance*, *Check*). See [app/classification.py](app/classification.py) +
  [app/import_service.py](app/import_service.py).
- **Master matching.** Named allowances/deductions are matched to existing `allowances`/`deductions`
  masters (subset + Jaccard fuzzy match), excluding statutory masters, so their amounts land in the
  right report buckets. Overtime is split (OT1/OT2). See [app/master_match.py](app/master_match.py).
- **Master sync (optional).** From a classification sheet, missing fixed/special allowance & deduction
  masters can be **created**, and existing ones **reclassified** (`plan_master_sync` is a dry-run;
  `apply_master_sync` writes). See [app/master_sync.py](app/master_sync.py).
- **Payslip + P9 generation.** Renders a native-style computed payslip per employee and writes P9
  (`krap9`) rows on push. See [templates/payslip.html](templates/payslip.html).
- **Integrity-safe push.** Skips **finalized** payrolls, preserves `salary_arrears` `paid_amount` when
  payments exist, removes only *unfinalized* duplicates, and requires `employee_id` to exist in
  `employees`. See [app/laravel_payroll_push.py](app/laravel_payroll_push.py) +
  [app/payroll_integrity.py](app/payroll_integrity.py).

### Report coverage notes

- **Allowance / deduction reports** (name-matched from the `payrolls` JSON): complete for imported
  months — every named earning/deduction is itemized through its matched master.
- **`/payroll-report`** (scalar-column summary; one column per `payrolls` field): money **totals** are
  correct (basic, gross, house, other allowances, NSSF, voluntary NSSF, PAYE, total deductions, net).
  By design it cannot itemize individual named lines — those all appear under `other_allowances` /
  `other_deductions`. A few scalar columns (e.g. `airtime_allowance`, `director_fees`) and
  `costing_report_deductions_total` may read blank/0 for imports unless explicitly mapped/flagged.

---

## Laragon environments

| Path | Role |
|------|------|
| `C:\laragon\www\Hr-Genie-Payroll` | Laravel app (optional: run the same migration there to keep schema in sync) |
| `C:\laragon\www\csv-payslip-import` | This FastAPI UI |

MySQL is typically **Laragon** `127.0.0.1:3306`, user `root`, empty password (local). Tenant DB name
matches the Stancl prefix `tenant_prl_*` from the central `tenants` table (e.g. `tenant_prl_bidco`),
or any database that has `employees` and related payroll data.

**Windows environment variables:** If `MYSQL_DATABASE` or `DATABASE_URL` is set in User/System
environment variables (even to an empty value), it can block values from `.env`. This app loads `.env`
with `override=True` so the project file wins; if anything still looks wrong, remove stray `MYSQL_*` /
`DATABASE_URL` entries from Windows.

---

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
   python -m pip install -r requirements.txt
   ```

3. Run:

   ```bash
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8890 --reload
   ```

4. Open **http://127.0.0.1:8890**

---

## Flow

1. **Upload** CSV/XLSX + payroll month → creates a `payroll_import_runs` row and parses rows
   (`POST /runs/upload`).
2. **Parse** validates every `PAYROLL NO` against `employees`. Missing employees are **skipped and
   reported** (the run only fails if *zero* rows match).
3. **Map columns** — auto-suggested from synonyms + classification + master match; review and adjust
   (`POST /runs/{id}/map/rebuild`, `POST /runs/{id}/map/save`, `POST /runs/{id}/map/ai-suggest`).
   Unclassified money columns are flagged for attention.
4. **Build snapshot** → CSV amounts frozen in `payroll_import_snapshots`
   (`POST /runs/{id}/snapshot`).
5. **Preview payslip** per employee, CSV as source of truth
   (`GET /runs/{id}/preview/{employee_id}`).
6. **Push to Laravel `/payroll`** → upserts one `payrolls` row per employee
   (`filter = CSV import · run {id}`), plus `salary_arrears` and P9 (`krap9`) rows, so the month
   appears on HR Genie **Payroll** (`/payroll/0?sub=mm-yyyy`). Re-push deletes only rows for that same
   filter + `payroll_date`, then re-inserts (`POST /runs/{id}/push-to-laravel-payroll`).

Diagnostics: `GET /health`, `GET /diag` (no DB), `GET /api/home-state` (JSON).

---

## Reference data

- **Sample upload:** `C:\Users\Admin\Documents\BIDCO\JAN sample upload.xlsx` (~933 rows).
- **Wage-type / master classification:** `C:\Users\Admin\Downloads\BIDCO_Allowance_Deduction_Classification.xlsx`
  drives both the stored classification (`seed_classifications`) and optional master creation/reclassification
  (`plan_master_sync` / `apply_master_sync`).

---

## Shareable Windows build (.exe)

You can ship a **folder** (onedir) so teammates do not need Python installed.

1. From the project root, install build deps (once):

   ```powershell
   cd C:\laragon\www\csv-payslip-import
   .\.venv\Scripts\activate
   python -m pip install -r requirements.txt -r requirements-build.txt
   ```

2. Build:

   ```powershell
   .\scripts\build_exe.ps1
   ```

   Or manually: `python -m PyInstaller --noconfirm csv-payslip-import.spec`

3. **Share** the whole `dist\csv-payslip-import\` directory (zip it). The runnable file is
   `csv-payslip-import.exe`.

4. **On each machine:** copy `.env.example` to `.env` in that **same folder as the .exe**, edit
   `MYSQL_*` / `DATABASE_URL`, then double-click the exe (or run it from a terminal). A console window
   stays open while the server runs; open **http://127.0.0.1:8890** (or whatever `PORT` is in `.env`).

**Note:** Antivirus may flag or slow first launch of PyInstaller bundles; the exe is not code-signed.
MySQL must be reachable from that PC (same rules as the Python app).

---

## Security

- Bind **127.0.0.1** only.
- Use a **read/write MySQL user** scoped to the tenant DB; never commit `.env`.
- The push path never edits **finalized** payrolls and preserves existing arrears/loan payments.

---

## Troubleshooting

- **`/` returns 500** — after pulling the latest `main.py`, open `/` again: the response is an HTML
  error page with a **full traceback** (not a blank “Internal Server Error”). Try
  `http://127.0.0.1:8890/api/home-state` (JSON, no Jinja): if `/health` is OK but this is not, MySQL
  cannot read `payroll_import_runs` (missing table or permissions). Also use `/diag` (no DB). If
  `/diag` is 404, you are not running this repo’s `app.main:app` from `csv-payslip-import` (check
  `/docs` for routes).
- **`uvicorn.exe` / `pip.exe` “blocked by your organization's Device Guard policy”** — the
  launcher shims pip writes into `.venv\Scripts\` are unsigned, so WDAC refuses them. `python.exe`
  itself is allowed, so call the same tools as modules: `python -m uvicorn app.main:app ...`,
  `python -m pip install ...`, `python -m PyInstaller ...`. `python -m app` also starts the server
  on the `.env` host/port. No policy exemption is needed.
- **`.xlsx` won't parse** — ensure `openpyxl>=3.1.0` is installed (`python -m pip install -r requirements.txt`).
- **A money column is flagged “unclassified”** — add/adjust its row in the classification sheet and
  re-seed, or fix the mapping on the run's map page before building the snapshot.
- **Statutory shows 0 on P9/payslip** — confirm the header canonicalizes correctly (synonyms map) so
  the computed lookup keys match; NSSF Tier III routes to *retirement contribution*, Voluntary NSSF to
  *voluntary*.
- **Stale config** — `get_settings()` is cached; after editing `.env`, restart the process (or bump
  `APP_TITLE` to confirm reload picked up changes).
