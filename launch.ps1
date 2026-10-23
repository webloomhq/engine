# Launch a Chrome session for the Chrome MCP
# Usage: .\launch.ps1 -Session main
#        .\launch.ps1 -Session farm
#        .\launch.ps1 -Session all

param(
    [string]$Session = "main"
)

$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$sessionsFile = Join-Path $PSScriptRoot "sessions.json"
$sessions = Get-Content $sessionsFile | ConvertFrom-Json

function Launch-Session($name, $cfg) {
    $port = $cfg.port
    $dir = $cfg.user_data_dir

    # Check if already running
    $existing = try { Invoke-RestMethod "http://127.0.0.1:$port/json/version" -TimeoutSec 1 } catch { $null }
    if ($existing) {
        # ASCII only. PowerShell 5.1 reads this file as ANSI, so a UTF-8 dash arrives as three
        # bytes, one of them a quote, and the whole script dies with "string is missing the
        # terminator" pointing 30 lines away from the real character.
        Write-Host "[$name] Already running on port $port, skipping" -ForegroundColor Yellow
        return
    }

    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Write-Host "[$name] Launching Chrome on port $port..." -ForegroundColor Cyan

    Start-Process $chromePath -ArgumentList @(
        "--remote-debugging-port=$port",
        "--user-data-dir=$dir",
        "--no-first-run",
        "--no-default-browser-check"
    )

    Start-Sleep -Milliseconds 1500
    $check = try { Invoke-RestMethod "http://127.0.0.1:$port/json/version" -TimeoutSec 2 } catch { $null }
    if ($check) {
        Write-Host "[$name] Ready on port $port" -ForegroundColor Green
    } else {
        Write-Host "[$name] May still be starting..." -ForegroundColor Yellow
    }
}

if ($Session -eq "all") {
    foreach ($name in ($sessions | Get-Member -MemberType NoteProperty).Name) {
        Launch-Session $name $sessions.$name
    }
} else {
    if (-not ($sessions | Get-Member -MemberType NoteProperty -Name $Session)) {
        Write-Host "Unknown session: $Session" -ForegroundColor Red
        # PowerShell 5.1 no parsea comillas simples dentro de $() dentro de "": se arma antes
        $disponibles = ($sessions | Get-Member -MemberType NoteProperty).Name -join ', '
        Write-Host "Available: $disponibles"
        exit 1
    }
    Launch-Session $Session $sessions.$Session
}
