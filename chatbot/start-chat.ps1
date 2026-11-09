# Starts the chat with Gemini and (if the Auditor server is running) tracing on.
# Run from the project folder:   .\chatbot\start-chat.ps1
# The Gemini key is asked for once per window and is not saved anywhere.

if (-not $env:GEMINI_API_KEY) {
    $secure = Read-Host "Paste your Gemini API key" -AsSecureString
    $env:GEMINI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
$env:CHAT_PROVIDER = "gemini"
if (-not $env:CHAT_MODEL) { $env:CHAT_MODEL = "gemini-3.8-flash" }

# Tracing: send every chat to Agent Auditor. The API key is read from the project's .env if it is not already set.
if (-not $env:AUDITOR_URL) { $env:AUDITOR_URL = "http://127.0.0.1:8000" }
$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
if (-not $env:AUDITOR_API_KEY -and (Test-Path $envFile)) {
    $line = Select-String -Path $envFile -Pattern '^\s*AUDITOR_API_KEY\s*=' | Select-Object -First 1
    if ($line) { $env:AUDITOR_API_KEY = ($line.Line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
}

python (Join-Path $PSScriptRoot "server.py")
