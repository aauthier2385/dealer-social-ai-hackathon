$ErrorActionPreference = "Stop"
Write-Host "=== Dealer Social AI - local run ===" -ForegroundColor Cyan
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env. Open it, set SERVICE_TOKEN and MODEL_API_KEY, then run this again." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment (.venv)..." -ForegroundColor DarkGray
    py -3.11 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
Write-Host "Open http://127.0.0.1:8090/playground" -ForegroundColor Green
Write-Host "Local only. Press Ctrl+C to stop." -ForegroundColor DarkGray
& .\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8090 --env-file .env
