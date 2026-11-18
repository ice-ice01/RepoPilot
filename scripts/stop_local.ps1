$ErrorActionPreference = "Stop"
$statePath = Join-Path (Split-Path -Parent $PSScriptRoot) ".data\local-processes.json"
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) { throw "No local startup state found." }
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
foreach ($service in @("frontend", "backend")) {
    $servicePid = $state."${service}_pid"
    $expectedStart = $state."${service}_started_at"
    $process = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    if (-not $expectedStart) { throw "Missing startup identity for $service PID $servicePid." }
    if ($expectedStart -is [datetime]) { $recordedUtc = $expectedStart.ToUniversalTime() }
    else { $recordedUtc = [datetimeoffset]::Parse($expectedStart).UtcDateTime }
    if ($process.StartTime.ToUniversalTime().Ticks -ne $recordedUtc.Ticks) {
        throw "Refusing to stop $service PID ${servicePid}: process identity does not match startup state."
    }
    & taskkill.exe /PID $servicePid /T /F
    if ($LASTEXITCODE -ne 0) { throw "Could not stop $service PID $servicePid." }
}
Write-Host "Frontend and backend stopped. Docker services and data were retained."
