# DEV ONLY, one command: the AI model (Ollama, for the AI summaries) + the LOS backend on 8010 with authentication OFF.
# Bank Verification:  http://127.0.0.1:8010/bank-verification/
# Crime Verification: http://127.0.0.1:8010/crime-verification/
# Run from the LOS-agentic-ai folder:   powershell -ExecutionPolicy Bypass -File .\scripts\start_all_dev.ps1
param([int]$Port = 8010)

Set-Location (Split-Path -Parent $PSScriptRoot)

# 1. Ollama in its own minimised window, unless it is already running
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
$ollamaExe = if ($ollama) { $ollama.Source } else { Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
if (Get-Process -Name ollama -ErrorAction SilentlyContinue) {
    Write-Host "Ollama: already running"
} elseif (Test-Path $ollamaExe) {
    $env:OLLAMA_NUM_PARALLEL = "4"
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Minimized
    Write-Host "Ollama: started (minimised window)"
} else {
    Write-Host "Ollama: not installed -- the screens work, AI summaries fall back to the rules-written text"
}

# 2. Port check: an old backend still on the port would make this start fail
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Port $Port is still in use: stop the old backend first (Ctrl+C in its window), then run this again." -ForegroundColor Yellow
    exit 1
}

# 3. The backend, authentication off (the backend refuses this outside ENVIRONMENT=development)
$env:ENVIRONMENT = "development"
$env:AUTH_ENABLED = "false"
$env:AUTH_LOCAL_SUBJECT = "local-developer"
$env:AUTH_LOCAL_SCOPES = "los.read los.write los.dedupe.read los.dedupe.confirm los.credit.underwrite create_applicant create_application update_applicant upload_document"
$env:MIFIN_PROVIDER = "demo"
$env:LMS_PROVIDER = "leviosa-demo"
$env:LOS_IDENTITY_HMAC_KEY = "dev1:" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")

Write-Host ""
Write-Host "Backend: http://127.0.0.1:$Port  (auth OFF)  -- Ctrl+C to stop"
Write-Host "  Bank Verification:  http://127.0.0.1:$Port/bank-verification/"
Write-Host "  Crime Verification: http://127.0.0.1:$Port/crime-verification/"
Write-Host ""

# the embedded database may still be recovering after an unclean stop: try again a few times before giving up
for ($try = 1; $try -le 3; $try++) {
    python -m uvicorn main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -eq 0) { break }
    Write-Host "Backend stopped (exit $LASTEXITCODE). Retrying in 30 s ($try of 3) -- Ctrl+C to give up." -ForegroundColor Yellow
    Start-Sleep -Seconds 30
}
