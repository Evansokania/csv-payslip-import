# Build Windows onedir bundle under dist\csv-payslip-import\
# Zip that folder and share; place .env next to csv-payslip-import.exe (copy from .env.example).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    Write-Error "Create .venv first: python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt -r requirements-build.txt"
}

.\.venv\Scripts\pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\pyinstaller.exe --noconfirm csv-payslip-import.spec

# PyInstaller puts `.env.example` inside `_internal`; duplicate beside the .exe for easy setup.
Copy-Item -Path .\.env.example -Destination .\dist\csv-payslip-import\.env.example -Force

$zip = Join-Path (Resolve-Path .\dist) "csv-payslip-import-Windows.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path ".\dist\csv-payslip-import\*" -DestinationPath $zip -CompressionLevel Optimal

Write-Host ""
Write-Host "Output: dist\csv-payslip-import\csv-payslip-import.exe"
Write-Host "Shareable zip: $zip"
Write-Host "Team: unzip, copy .env.example to .env (same folder as the .exe), edit MYSQL_* / DATABASE_URL, run the exe."
