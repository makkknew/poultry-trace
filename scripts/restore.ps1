param([Parameter(Mandatory=$true)][string]$BackupFile)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path $BackupFile)) { throw "Файл резервной копии не найден: $BackupFile" }
Write-Host "Восстанавливается резервная копия базы Poultry Trace."
$containerId = (docker compose ps -q db).Trim()
if (-not $containerId) { throw "Контейнер базы не запущен." }
$containerPath = "/tmp/poultry_trace_restore.dump"
docker cp (Resolve-Path $BackupFile).Path "${containerId}:$containerPath"
docker compose exec -T db pg_restore -U poultry -d poultry_trace --clean --if-exists $containerPath
docker compose exec -T db rm -f $containerPath
