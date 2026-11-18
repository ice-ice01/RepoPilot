param(
    [ValidateRange(1, 65535)][int]$BackendPort = 8000,
    [ValidateRange(1, 65535)][int]$FrontendPort = 3000,
    [string]$ProxyUrl,
    [string]$DockerPath
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$pythonPath = Join-Path $backendDirectory ".venv\Scripts\python.exe"
$dataDirectory = Join-Path $projectRoot ".data"

foreach ($name in @("AI_API_KEY", "AI_BASE_URL", "AI_MODEL")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
        foreach ($scope in @("User", "Machine")) {
            $value = [Environment]::GetEnvironmentVariable($name, $scope)
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                [Environment]::SetEnvironmentVariable($name, $value, "Process")
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
        throw "Required environment variable is missing: $name"
    }
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "Install backend/.venv first." }
if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory "node_modules") -PathType Container)) {
    throw "Install frontend dependencies first."
}
foreach ($port in @($BackendPort, $FrontendPort)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. Check the running service before starting another instance."
    }
}
if (-not $DockerPath) {
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($dockerCommand) { $DockerPath = $dockerCommand.Source }
    else { $DockerPath = "D:\BrowserDownload\DockerInstall\DockerDesktop\resources\bin\docker.exe" }
}
if (-not (Test-Path -LiteralPath $DockerPath -PathType Leaf)) { throw "Docker CLI not found. Supply -DockerPath." }
& $DockerPath compose --project-directory $projectRoot up -d --no-build --wait --wait-timeout 180 postgres etcd minio milvus
if ($LASTEXITCODE -ne 0) { throw "Docker dependencies failed. Open Docker Desktop and check the containers." }

New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
$env:DATABASE_URL = "postgresql+psycopg://devflow:devflow@127.0.0.1:5432/devflow"
$env:MILVUS_ENABLED = "true"
$env:MILVUS_URI = "http://127.0.0.1:19530"
$env:EMBEDDING_PROVIDER = "qwen_local"
$env:EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
$env:EMBEDDING_DIMENSIONS = "1024"
$env:LOCAL_EMBEDDING_DEVICE = "cpu"
$env:LOCAL_EMBEDDING_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
$env:HF_HOME = Join-Path $dataDirectory "huggingface"
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$env:TOKENIZERS_PARALLELISM = "false"
$env:DEMO_MODE = "false"
$env:AUTO_SYNC_ENABLED = "false"
$env:RAG_LLM_ENABLED = "true"
$env:RAGAS_ENABLED = "true"
$env:REPO_CHECKOUT_DIR = Join-Path $dataDirectory "repos"
$env:UPLOAD_DIR = Join-Path $dataDirectory "uploads"
$env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:$BackendPort"
$env:PYTHONPATH = $backendDirectory
$env:NO_PROXY = "localhost,127.0.0.1,::1"
if ($ProxyUrl) { $env:HTTP_PROXY = $ProxyUrl; $env:HTTPS_PROXY = $ProxyUrl }

$keyPath = Join-Path $dataDirectory "token-encryption.key"
if ([string]::IsNullOrWhiteSpace($env:TOKEN_ENCRYPTION_KEY)) {
    if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
        $generatedKey = & $pythonPath -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        if ($LASTEXITCODE -ne 0) { throw "Cannot generate local token encryption key." }
        [IO.File]::WriteAllText($keyPath, $generatedKey.Trim())
    }
    $env:TOKEN_ENCRYPTION_KEY = [IO.File]::ReadAllText($keyPath).Trim()
}

# Load the cached model before launching, so missing downloads fail visibly.
Push-Location $backendDirectory
try {
    & $pythonPath -c "from app.services.rag.embeddings import embed_text; v=embed_text('startup validation'); assert len(v)==1024; print('Local Qwen embedding ready: 1024 dimensions')"
    if ($LASTEXITCODE -ne 0) { throw "Qwen cache is incomplete. Download the pinned model into HF_HOME first." }
}
finally { Pop-Location }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backendErrorLog = Join-Path $dataDirectory "backend-$stamp.err.log"
$frontendErrorLog = Join-Path $dataDirectory "frontend-$stamp.err.log"
$backendProcess = $null
$frontendProcess = $null
function Wait-LocalService($process, $url, $errorLog) {
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        $process.Refresh()
        if ($process.HasExited) { throw "Service exited. Check $errorLog" }
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2 | Out-Null
            return
        }
        catch { Start-Sleep -Milliseconds 500 }
    }
    throw "Service did not become ready: $url. Check $errorLog"
}
try {
    $backendProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $backendDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dataDirectory "backend-$stamp.out.log") -RedirectStandardError $backendErrorLog
    Wait-LocalService $backendProcess "http://127.0.0.1:$BackendPort/health" $backendErrorLog
    $frontendProcess = Start-Process -FilePath "npm.cmd" `
        -ArgumentList @("run", "dev", "--", "--hostname", "127.0.0.1", "-p", "$FrontendPort") `
        -WorkingDirectory $frontendDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $dataDirectory "frontend-$stamp.out.log") -RedirectStandardError $frontendErrorLog
    Wait-LocalService $frontendProcess "http://127.0.0.1:$FrontendPort" $frontendErrorLog
    $processInfo = @{
        backend_pid=$backendProcess.Id; frontend_pid=$frontendProcess.Id
        backend_started_at=$backendProcess.StartTime.ToUniversalTime().ToString("o")
        frontend_started_at=$frontendProcess.StartTime.ToUniversalTime().ToString("o")
        started_at=$stamp; backend_port=$BackendPort; frontend_port=$FrontendPort
    }
    $processInfo | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dataDirectory "local-processes.json") -Encoding UTF8
    Write-Host "Frontend: http://127.0.0.1:$FrontendPort"
    Write-Host "API docs: http://127.0.0.1:$BackendPort/docs"
    Write-Host "AI configuration: AI_API_KEY / AI_BASE_URL / AI_MODEL (values are not logged)"
    Write-Host "Backend PID: $($backendProcess.Id); frontend launcher PID: $($frontendProcess.Id)"
    Write-Host "Logs: $dataDirectory"
}
catch {
    if ($frontendProcess -and -not $frontendProcess.HasExited) {
        & taskkill.exe /PID $frontendProcess.Id /T /F | Out-Null
    }
    if ($backendProcess -and -not $backendProcess.HasExited) {
        & taskkill.exe /PID $backendProcess.Id /T /F | Out-Null
    }
    throw
}
