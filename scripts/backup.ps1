param([string]$OutputDirectory = "backups")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path ".env")) { throw "Файл .env не найден. Сначала запустите проект." }
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$name = "poultry_trace_{0}.dump" -f (Get-Date -Format "yyyyMMdd_HHmmss")
$containerPath = "/tmp/$name"
docker compose exec -T db pg_dump -U poultry -Fc -f $containerPath poultry_trace
$containerId = (docker compose ps -q db).Trim()
if (-not $containerId) { throw "Контейнер базы не запущен." }
docker cp "${containerId}:$containerPath" (Join-Path $OutputDirectory $name)
docker compose exec -T db rm -f $containerPath
Write-Host "Резервная копия создана: $(Join-Path $OutputDirectory $name)"
