param([int]$Port = 7897)

$ErrorActionPreference = 'Stop'
$settingsPath = Join-Path $env:APPDATA 'Docker\settings-store.json'
if (-not (Test-Path -LiteralPath $settingsPath)) {
    throw 'Docker Desktop settings file not found.'
}
$backupPath = "$settingsPath.backup-$(Get-Date -Format yyyyMMddHHmmss)"
Copy-Item -LiteralPath $settingsPath -Destination $backupPath
$config = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
$proxy = "http://127.0.0.1:$Port"
$config | Add-Member -NotePropertyName proxyHttpMode -NotePropertyValue 'manual' -Force
$config | Add-Member -NotePropertyName overrideProxyHTTP -NotePropertyValue $proxy -Force
$config | Add-Member -NotePropertyName overrideProxyHTTPS -NotePropertyValue $proxy -Force
$config | Add-Member -NotePropertyName overrideProxyExclude -NotePropertyValue 'localhost,127.0.0.1' -Force
$json = $config | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText($settingsPath, $json, [Text.UTF8Encoding]::new($false))
Write-Output "Docker proxy configured: $proxy"
Write-Output "Settings backup: $backupPath"
