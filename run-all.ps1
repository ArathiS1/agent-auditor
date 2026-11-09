# One command to run everything: the Agent Auditor server AND the chatbot, already connected.
#
#   powershell -ExecutionPolicy Bypass -File .\run-all.ps1            start both and open the browser
#   powershell -ExecutionPolicy Bypass -File .\run-all.ps1 -Check     start both, then run the 23-point self-check
#   powershell -ExecutionPolicy Bypass -File .\run-all.ps1 -Test      run the unit tests only (needs: pip install -e ".[dev]")
#   powershell -ExecutionPolicy Bypass -File .\run-all.ps1 -Bench     start the Auditor, then benchmark every model whose key is in .env
#
# The first run asks for your Gemini key once and saves it, with a generated Auditor key, in .env
# (which is never committed). After that you are never asked again.

param([switch]$Check, [switch]$Test, [switch]$Bench)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$envFile = Join-Path $root ".env"

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

function Get-Saved([string]$name) {
    if (-not (Test-Path $envFile)) { return "" }
    $m = Select-String -Path $envFile -Pattern ("^\s*" + $name + "\s*=") | Select-Object -First 1
    if (-not $m) { return "" }
    return ($m.Line -split "=", 2)[1].Trim().Trim('"').Trim("'")
}

function Save-Value([string]$name, [string]$value) {
    $lines = @()
    if (Test-Path $envFile) { $lines = @(Get-Content $envFile) }
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match ("^\s*" + $name + "\s*=")) { $lines[$i] = "$name=$value"; $found = $true; break }
    }
    if (-not $found) { $lines += "$name=$value" }
    Set-Content -Path $envFile -Value $lines -Encoding UTF8
}

function Use-Setting([string]$name, [string]$default) {
    # window value wins, then .env, then the default (which is saved so it sticks)
    $v = [Environment]::GetEnvironmentVariable($name)
    if (-not $v) { $v = Get-Saved $name }
    if (-not $v -and $default) { $v = $default; Save-Value $name $v }
    if ($v) { [Environment]::SetEnvironmentVariable($name, $v) }
    return $v
}

if ($Test) {
    # tests must not see your real keys
    foreach ($n in "AUDITOR_API_KEY", "AUDITOR_URL", "ANTHROPIC_API_KEY") { [Environment]::SetEnvironmentVariable($n, $null) }
    & $py -m pytest -q
    exit $LASTEXITCODE
}

# ---- settings ----
$provider = Use-Setting "CHAT_PROVIDER" "gemini"
$null = Use-Setting "CHAT_MODEL" "gemini-flash-latest"
$null = Use-Setting "AUDITOR_URL" "http://127.0.0.1:8000"

$keyName = @{ gemini = "GEMINI_API_KEY"; anthropic = "ANTHROPIC_API_KEY"; groq = "GROQ_API_KEY"; openrouter = "OPENROUTER_API_KEY"; openai = "OPENAI_API_KEY"; perplexity = "PERPLEXITY_API_KEY"; deepseek = "DEEPSEEK_API_KEY"; mistral = "MISTRAL_API_KEY"; xai = "XAI_API_KEY"; cerebras = "CEREBRAS_API_KEY"; airouter = "AIROUTER_API_KEY"; nabh = "NABH_API_KEY" }[$provider]
if ($keyName) {
    $k = Use-Setting $keyName ""
    if (-not $k) {
        $secure = Read-Host "Paste your $provider API key (saved in .env, asked only once)" -AsSecureString
        $k = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim().Trim('"').Trim("'")
        if (-not $k) { throw "No key entered." }
        Save-Value $keyName $k
        [Environment]::SetEnvironmentVariable($keyName, $k)
    }
}

$auditKey = Use-Setting "AUDITOR_API_KEY" ""
if (-not $auditKey -or $auditKey -eq "change-me-too") {
    $auditKey = -join ((48..57) + (97..102) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
    Save-Value "AUDITOR_API_KEY" $auditKey
    [Environment]::SetEnvironmentVariable("AUDITOR_API_KEY", $auditKey)
    Write-Host "Made an Auditor key and saved it in .env"
}

$auditUrl = $env:AUDITOR_URL.TrimEnd("/")

# ---- 1. the Auditor server (own window; it inherits the key from here) ----
function Test-Auditor {
    try { $r = Invoke-RestMethod -Uri "$auditUrl/healthz" -TimeoutSec 2; return ($r.status -eq "ok") } catch { return $false }
}
# A server left running from an older unzip serves the old code, which cannot build the new dashboard (blank page).
$wantVersion = ""
$m = Select-String -Path (Join-Path $root "src\agent_auditor\__init__.py") -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($m) { $wantVersion = $m.Matches[0].Groups[1].Value }
function Get-AuditorVersion { try { return (Invoke-RestMethod -Uri "$auditUrl/healthz" -TimeoutSec 2).version } catch { return "" } }
$port = ([Uri]$auditUrl).Port
if ((Test-Auditor) -and $wantVersion -and ((Get-AuditorVersion) -ne $wantVersion)) {
    Write-Host "An older Auditor server (version $(Get-AuditorVersion)) is still running. Stopping it so version $wantVersion can start..." -ForegroundColor Yellow
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20 -and (Test-Auditor); $i++) { Start-Sleep -Milliseconds 500 }
}
if (Test-Auditor) {
    Write-Host "Auditor server is already running at $auditUrl (version $(Get-AuditorVersion))"
} else {
    Write-Host "Starting the Auditor server..."
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root'; & '$py' -m uvicorn agent_auditor.api.main:app --port $port"
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) { Start-Sleep -Milliseconds 750; if (Test-Auditor) { $ok = $true; break } }
    if (-not $ok) { throw "The Auditor server did not start. Look at the new window for the reason (usually: run 'pip install -e .' in the venv)." }
}
Write-Host "Auditor dashboard:  $auditUrl/dashboard   (opens already signed in; your key is in .env as AUDITOR_API_KEY)"

# ---- 2. optional self-check ----
if ($Check) {
    & $py (Join-Path $root "validation\selfcheck.py")
    if ($LASTEXITCODE -ne 0) { Write-Host "Self-check reported a problem (see above)." -ForegroundColor Yellow }
}

# ---- 2b. optional benchmark ----
if ($Bench) {
    & $py (Join-Path $root "bench\run.py")
    Start-Process "$auditUrl/dashboard#key=$auditKey"
    exit $LASTEXITCODE
}

# ---- 3. the chatbot (this window), tracing on ----
$chatPort = if ($env:CHAT_PORT) { $env:CHAT_PORT } else { "8001" }
Start-Process "$auditUrl/dashboard#key=$auditKey"
Start-Process "http://127.0.0.1:$chatPort"
Write-Host "Chat:               http://127.0.0.1:$chatPort   (Ctrl+C here stops the chat; close the other window to stop the Auditor)"
& $py (Join-Path $root "chatbot\server.py")
