$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot "venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Create it with: py -m venv venv"
}

& $python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
