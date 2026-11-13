# DEV ONLY: start the LOS backend on 8010 with authentication OFF, demo MIFIN / LMS and a throwaway identity key.
# The backend refuses AUTH_ENABLED=false outside ENVIRONMENT=development, so this cannot run as production.
# Run from the LOS-agentic-ai folder:   .\scripts\start_backend_auth_off.ps1   (optional: -Port 8030)
param([int]$Port = 8010)

$env:ENVIRONMENT = "development"
$env:AUTH_ENABLED = "false"
$env:AUTH_LOCAL_SUBJECT = "local-developer"
# every scope the Dedupe Agent page AND the chatbot need: read, create a case, upload documents, correct a field from
# chat, dedupe check, confirm a possible match (app/config/applicant_agent.yaml permissions)
$env:AUTH_LOCAL_SCOPES = "los.read los.write los.dedupe.read los.dedupe.confirm los.credit.underwrite agents:execute read_applicant read_application read_documents read_verification read_pending_items read_next_action create_applicant create_application update_applicant upload_document"
$env:MIFIN_PROVIDER = "demo"
# the LMS is Leviosa (owner 2026-10-23): synthetic Leviosa replies read by the real Leviosa mapping (dev only)
$env:LMS_PROVIDER = "leviosa-demo"
# key_id:secret (a new random secret each start; dev only)
$env:LOS_IDENTITY_HMAC_KEY = "dev1:" + [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")

Write-Host "Backend: http://127.0.0.1:$Port  (auth OFF, demo MIFIN + demo Leviosa LMS)  -- Ctrl+C to stop"
python -m uvicorn main:app --host 127.0.0.1 --port $Port
