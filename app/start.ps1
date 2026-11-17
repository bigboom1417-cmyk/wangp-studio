# Starts the Studio server hidden on port 4500 with WanGP's python. Safe to run twice.
if (Get-NetTCPConnection -LocalPort 4500 -State Listen -ErrorAction SilentlyContinue) { "Studio already running: http://localhost:4500"; exit 0 }
Start-Process -WindowStyle Hidden -WorkingDirectory $PSScriptRoot -FilePath "$(if ($env:WANGP_DIR) { $env:WANGP_DIR } else { 'C:\AI\WanGP' })\env_uv\Scripts\python.exe" `
  -ArgumentList "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "4500"
"Studio started: http://localhost:4500"
