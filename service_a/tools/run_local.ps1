<#
.SYNOPSIS
Runs A, B, C, D and the frontend together on this machine, each from its own branch.

.DESCRIPTION
Every service is checked out from its teammate's branch into a git worktree under -Workspace, so
nothing is merged and nobody's code is changed. B runs in a Linux container, because it uses
Unix-only signal handling; PostgreSQL also runs in a container. A, C, D and the frontend run as
local processes, with their logs in <Workspace>\logs.

  Service      Branch                  Port
  A            feature/trading         8000
  B            main                    9999 (market data), 11659 (D's channel)
  C            main                    8081 (8080 is often taken on Windows), RabbitMQ off
  D            feature/risk_control    8400
  PostgreSQL   -                       5432
  Frontend     feature/frontend        5173

Run it from cmd or PowerShell:
  powershell -ExecutionPolicy Bypass -File service_a\tools\run_local.ps1            start or update
  powershell -ExecutionPolicy Bypass -File service_a\tools\run_local.ps1 -Status    show health
  powershell -ExecutionPolicy Bypass -File service_a\tools\run_local.ps1 -Check     end-to-end check
  powershell -ExecutionPolicy Bypass -File service_a\tools\run_local.ps1 -Stop      stop everything
  powershell -ExecutionPolicy Bypass -File service_a\tools\run_local.ps1 -Reset     stop and erase all data

The first start compiles B (about 6 minutes) and installs the Python and npm dependencies.
#>
param(
    [string]$Workspace = "",
    [switch]$Stop,
    [switch]$Status,
    [switch]$Check,
    [switch]$Reset,
    [switch]$NoUpdate
)

$ErrorActionPreference = "Stop"

$commonDir = (git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir).Trim()
$repo = Split-Path $commonDir -Parent
if (-not $Workspace) { $Workspace = Join-Path (Split-Path $repo -Parent) "LAME-local" }
$logs = Join-Path $Workspace "logs"
$pidFile = Join-Path $Workspace "pids.json"

$trees = [ordered]@{
    main = "origin/main"
    a    = "origin/feature/trading"
    d    = "origin/feature/risk_control"
    fe   = "origin/feature/frontend"
}
$ports = [ordered]@{ A = 8000; D = 8400; C = 8081; Frontend = 5173; B_md = 9999; B_ctrl = 11659; Postgres = 5432 }
$dbUrl = "postgresql+asyncpg://lame:lame@127.0.0.1:5432/lame"

# Native commands whose failure is expected (stopping what is not running, removing what is
# absent). With ErrorActionPreference Stop, Windows PowerShell turns their stderr into an error.
function Invoke-Quiet([scriptblock]$command) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $command 2>&1 | Out-Null } finally { $ErrorActionPreference = $saved }
}

function Say($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }

function Test-Http($url) {
    try { return (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { return $false }
}

function Wait-Http($name, $url, $seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        if (Test-Http $url) { Write-Host "    $name is up: $url" -ForegroundColor Green; return }
        Start-Sleep -Seconds 2
    }
    throw "$name did not answer at $url within $seconds s. See the log in $logs."
}

function Get-Listener($port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue }
    return $null
}

function Stop-All {
    Say "Stopping A, C, D and the frontend"
    if (Test-Path $pidFile) {
        foreach ($p in (Get-Content $pidFile | ConvertFrom-Json).PSObject.Properties) {
            Invoke-Quiet { taskkill /F /T /PID $p.Value }
        }
        Remove-Item $pidFile
    }
    # Processes left from a run without a pid file: only python or node on our own ports.
    foreach ($port in 8000, 8400, 8081, 5173) {
        $proc = Get-Listener $port
        if ($proc -and $proc.ProcessName -in "python", "node") { Invoke-Quiet { taskkill /F /T /PID $proc.Id } }
    }
    Say "Stopping the B and PostgreSQL containers"
    Invoke-Quiet { docker stop lame-b lame-pg }
}

function Show-Status {
    $checks = [ordered]@{
        A        = "http://127.0.0.1:8000/healthz"
        D        = "http://127.0.0.1:8400/healthz"
        B        = "http://127.0.0.1:11659/healthz"
        C        = "http://127.0.0.1:8081/healthz"
        Frontend = "http://localhost:5173/"
    }
    foreach ($name in $checks.Keys) {
        if (Test-Http $checks[$name]) { Write-Host ("    {0,-9} up    {1}" -f $name, $checks[$name]) -ForegroundColor Green }
        else { Write-Host ("    {0,-9} DOWN  {1}" -f $name, $checks[$name]) -ForegroundColor Red }
    }
    docker ps --filter "name=lame-" --format "    {{.Names}}: {{.Status}}"
}

function Update-Trees {
    if (-not $NoUpdate) {
        Say "Fetching the latest branches"
        git -C $repo fetch --quiet origin
    }
    git -C $repo worktree prune
    $changed = @()
    foreach ($name in $trees.Keys) {
        $path = Join-Path $Workspace $name
        $ref = $trees[$name]
        if (-not (Test-Path $path)) {
            git -C $repo worktree add --quiet --detach $path $ref
            $changed += $name
            continue
        }
        if ($NoUpdate) { continue }
        $branch = (git -C $path rev-parse --abbrev-ref HEAD).Trim()
        $dirty = git -C $path status --porcelain --untracked-files=no
        if ($branch -ne "HEAD" -or $dirty) {
            Warn "$name is on a branch or has local changes; left as it is"
            continue
        }
        $before = (git -C $path rev-parse HEAD).Trim()
        git -C $path checkout --quiet --detach $ref
        if ((git -C $path rev-parse HEAD).Trim() -ne $before) { $changed += $name }
    }
    foreach ($name in $trees.Keys) {
        $path = Join-Path $Workspace $name
        Write-Host ("    {0,-5} {1}" -f $name, (git -C $path log -1 --format="%h %ad %s" --date=short))
    }
    return $changed
}

function Ensure-Venv($dir, [string[]]$requirements, [string[]]$extra) {
    $python = Join-Path $dir "Scripts\python.exe"
    if (-not (Test-Path $python)) { python -m venv $dir }
    $stamp = Join-Path $dir "requirements.stamp"
    $hash = ($requirements | ForEach-Object { (Get-FileHash $_).Hash }) -join ","
    if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $hash) {
        Say "Installing Python dependencies into $dir"
        foreach ($r in $requirements) { & $python -m pip install --quiet --disable-pip-version-check -r $r }
        if ($extra) { & $python -m pip install --quiet --disable-pip-version-check @extra }
        Set-Content $stamp $hash
    }
    return $python
}

function Start-Service($name, $file, [string[]]$arguments, $dir, [hashtable]$envVars) {
    $saved = @{}
    foreach ($k in $envVars.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k)
        [Environment]::SetEnvironmentVariable($k, $envVars[$k])
    }
    try {
        $out = Join-Path $logs "$name.log"
        $err = Join-Path $logs "$name.err.log"
        $proc = Start-Process -FilePath $file -ArgumentList $arguments -WorkingDirectory $dir -WindowStyle Hidden `
            -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }
    return $proc.Id
}

function Start-Containers($rebuildB) {
    Say "Starting PostgreSQL"
    if (-not (docker ps -a --filter "name=^lame-pg$" --format "{{.Names}}")) {
        docker run -d --name lame-pg -e POSTGRES_USER=lame -e POSTGRES_PASSWORD=lame -e POSTGRES_DB=lame `
            -p 127.0.0.1:5432:5432 -v lame-pg-data:/var/lib/postgresql/data postgres:16-alpine | Out-Null
    } else { docker start lame-pg | Out-Null }

    Say "Starting B (the first start compiles it, about 6 minutes)"
    $src = Join-Path $Workspace "main\match_engine"
    # B reads its pairs from config\pairs.json since 18 Nov; a container made before that lacks the mount.
    $pairs = Join-Path $src "config\pairs.json"
    $exists = docker ps -a --filter "name=^lame-b$" --format "{{.Names}}"
    if ($exists -and (Test-Path $pairs) -and -not (docker inspect lame-b --format "{{range .Mounts}}{{.Destination}} {{end}}" | Select-String "/etc/lame/pairs.json")) {
        Warn "B now needs its pair file: recreating its container (data is kept)"
        Invoke-Quiet { docker rm -f lame-b }
        $exists = $null
    }
    if (-not $exists) {
        $pairsMount = if (Test-Path $pairs) { @("-v", "${pairs}:/etc/lame/pairs.json:ro", "-e", "PAIRS_FILE=/etc/lame/pairs.json") } else { @() }
        docker run -d --name lame-b -v "${src}:/src" -v lame-b-target:/src/target -v lame-b-cargo:/usr/local/cargo/registry `
            -v lame-b-data:/data -w /src -p 9999:9999 -p 11659:11659 @pairsMount `
            -e "SYMBOLS=BTC-USDT:1:1,ETH-USDT:1:1" -e EVENT_DB_DIR=/data -e RUST_LOG=info `
            rust:1.90 cargo run --release | Out-Null
    } elseif ($rebuildB) {
        Warn "B's code changed: restarting it to rebuild"
        docker restart lame-b | Out-Null
    } else { docker start lame-b | Out-Null }

    $end = (Get-Date).AddMinutes(2)
    while ((& { $ErrorActionPreference = "Continue"; docker exec lame-pg pg_isready -U lame 2>&1 }) -notmatch "accepting") {
        if ((Get-Date) -gt $end) { throw "PostgreSQL did not start" }
        Start-Sleep -Seconds 1
    }
    Wait-Http "B" "http://127.0.0.1:11659/healthz" 900
}

function Start-All {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker is required for B and PostgreSQL." }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw "The Docker engine is not running. Start Docker Desktop and try again." }

    foreach ($name in $ports.Keys) {
        $proc = Get-Listener $ports[$name]
        if ($proc -and $proc.ProcessName -notin "python", "node", "com.docker.backend", "wslrelay", "vpnkit") {
            throw "Port $($ports[$name]) for $name is taken by $($proc.ProcessName) (PID $($proc.Id))."
        }
    }

    New-Item -ItemType Directory -Force -Path $Workspace, $logs, (Join-Path $Workspace "d-data") | Out-Null
    $changed = Update-Trees

    $aDir = Join-Path $Workspace "a\service_a"
    $dDir = Join-Path $Workspace "d\service_d"
    $mainDir = Join-Path $Workspace "main"
    $feDir = Join-Path $Workspace "fe\frontend"
    $pyAD = Ensure-Venv (Join-Path $Workspace "venv-ad") @((Join-Path $aDir "requirements.txt"), (Join-Path $dDir "requirements.txt")) @("tzdata")
    $pyC = Ensure-Venv (Join-Path $Workspace "venv-c") @((Join-Path $mainDir "service_c\requirements.txt")) @()
    if (-not (Test-Path (Join-Path $feDir "node_modules")) -or $changed -contains "fe") {
        Say "Installing the frontend's npm dependencies"
        Push-Location $feDir; npm install --no-audit --no-fund --loglevel=error; Pop-Location
    }

    Stop-All
    Start-Containers ($changed -contains "main")

    $secretFile = Join-Path $Workspace "jwt_secret.txt"
    if (-not (Test-Path $secretFile)) { Set-Content $secretFile ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")) }
    $secret = (Get-Content $secretFile).Trim()

    Say "Migrating A's database"
    $env:DATABASE_URL = $dbUrl
    Push-Location $aDir; & $pyAD -m alembic upgrade head; Pop-Location
    Remove-Item Env:DATABASE_URL

    Say "Starting D, A, C and the frontend"
    $pids = [ordered]@{}
    $pids.D = Start-Service "d" $pyAD @("-m", "d.main") $dDir @{
        B_CTRL_URL = "ws://127.0.0.1:11659/d"; A_NOTIFY_URL = "http://127.0.0.1:8000/internal/order-events"
        DATA_DIR = (Join-Path $Workspace "d-data"); PYTHONUTF8 = "1"
    }
    $pids.A = Start-Service "a" $pyAD @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1") $aDir @{
        DATABASE_URL = $dbUrl; D_URL = "http://127.0.0.1:8400"; B_MD_URL = "ws://127.0.0.1:9999"; JWT_SECRET = $secret; PYTHONUTF8 = "1"
    }
    $pids.C = Start-Service "c" $pyC @("-m", "uvicorn", "service_c.main:app", "--host", "127.0.0.1", "--port", "8081") $mainDir @{
        SERVICE_C_PORT = "8081"; RABBITMQ_ENABLED = "false"; SERVICE_B_WS_URL = "ws://127.0.0.1:9999/md"
        SERVICE_C_DB_PATH = (Join-Path $Workspace "c-data.db"); PYTHONUTF8 = "1"
    }
    $pids.Frontend = Start-Service "frontend" "cmd.exe" @("/c", "npx", "vite", "--port", "5173", "--strictPort") $feDir @{
        VITE_C_URL = "http://localhost:8081"
    }
    $pids | ConvertTo-Json | Set-Content $pidFile

    Wait-Http "D" "http://127.0.0.1:8400/healthz" 90
    Wait-Http "A" "http://127.0.0.1:8000/healthz" 90
    Wait-Http "C" "http://127.0.0.1:8081/healthz" 90
    Wait-Http "Frontend" "http://localhost:5173/" 120

    Write-Host ""
    Write-Host "Open http://localhost:5173  (seeded users: user1001 ... with password lame-demo-password)" -ForegroundColor Green
    Write-Host "Logs: $logs.   Stop with -Stop.   B accepts orders 00:00-23:00 Dublin time."
}

function Reset-All {
    Stop-All
    Say "Erasing A's, B's, C's and D's data together"
    Invoke-Quiet { docker rm -f lame-b lame-pg }
    foreach ($volume in "lame-pg-data", "lame-b-data") { Invoke-Quiet { docker volume rm $volume } }
    Remove-Item -Recurse -Force (Join-Path $Workspace "d-data"), (Join-Path $Workspace "c-data.db") -ErrorAction SilentlyContinue
}

if ($Stop) { Stop-All }
elseif ($Reset) { Reset-All }
elseif ($Status) { Show-Status }
elseif ($Check) {
    $python = Join-Path $Workspace "venv-ad\Scripts\python.exe"
    & $python (Join-Path $PSScriptRoot "local_e2e.py")
    exit $LASTEXITCODE
}
else { Start-All }
