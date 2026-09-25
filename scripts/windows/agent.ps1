# ModelManager remote control helper for native Windows llama.cpp processes.
# Invoked over SSH by modelmanager.adapters.llamacpp.LlamaCppAdapter.
# Deployed to <your home folder>\.ModelManager\agent.ps1 on each Windows GPU
# box (see modelmanager/agent_deploy.py - deployment is automatic).

param(
    [Parameter(Mandatory = $true)][string]$Action,
    [Parameter(Mandatory = $true)][string]$PayloadB64
)

$ErrorActionPreference = "Stop"

$root = Join-Path $env:USERPROFILE ".ModelManager"
$pidDir = Join-Path $root "pids"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $pidDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($PayloadB64))
$payload = $json | ConvertFrom-Json

function Get-PidPath($name) { Join-Path $pidDir "$name.pid" }
function Get-LogPath($name) { Join-Path $logDir "$name.log" }
function Get-ErrLogPath($name) { Join-Path $logDir "$name.err.log" }

switch ($Action) {
    "start" {
        $name = $payload.name
        $binary = $payload.binary
        $modelPath = $payload.model_path
        $port = $payload.port
        $extraArgs = @()
        if ($payload.args) { $extraArgs = @($payload.args) }

        $logPath = Get-LogPath $name
        $errLogPath = Get-ErrLogPath $name
        $argList = @("--model", $modelPath, "--port", "$port") + $extraArgs

        $proc = Start-Process -FilePath $binary -ArgumentList $argList `
            -RedirectStandardOutput $logPath -RedirectStandardError $errLogPath `
            -WindowStyle Hidden -PassThru

        Set-Content -Path (Get-PidPath $name) -Value $proc.Id
        @{ pid = $proc.Id; log_path = $logPath } | ConvertTo-Json -Compress
    }
    "stop" {
        $name = $payload.name
        $pidPath = Get-PidPath $name
        if (Test-Path $pidPath) {
            $procId = Get-Content $pidPath
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Remove-Item $pidPath -ErrorAction SilentlyContinue
        }
        @{ status = "stopped" } | ConvertTo-Json -Compress
    }
    "status" {
        $name = $payload.name
        $pidPath = Get-PidPath $name
        if (-not (Test-Path $pidPath)) {
            @{ running = $false } | ConvertTo-Json -Compress
        }
        else {
            $procId = Get-Content $pidPath
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc) {
                @{ running = $true; pid = [int]$procId } | ConvertTo-Json -Compress
            }
            else {
                Remove-Item $pidPath -ErrorAction SilentlyContinue
                @{ running = $false } | ConvertTo-Json -Compress
            }
        }
    }
    "logs" {
        $name = $payload.name
        $lines = 200
        if ($payload.lines) { $lines = [int]$payload.lines }
        $logPath = Get-LogPath $name
        $errLogPath = Get-ErrLogPath $name
        if (Test-Path $logPath) { Get-Content -Path $logPath -Tail $lines }
        if (Test-Path $errLogPath) {
            Write-Output "--- stderr ---"
            Get-Content -Path $errLogPath -Tail $lines
        }
    }
    default {
        Write-Error "unknown action: $Action"
        exit 1
    }
}
