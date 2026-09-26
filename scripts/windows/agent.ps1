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
        $argList = @()
        if ($payload.args) { $argList = @($payload.args) }
        $cwd = $payload.cwd

        $logPath = Get-LogPath $name
        $errLogPath = Get-ErrLogPath $name
        $pidPath = Get-PidPath $name
        if (Test-Path $pidPath) { Remove-Item $pidPath -ErrorAction SilentlyContinue }

        function Quote-Literal($value) { "'" + ($value -replace "'", "''") + "'" }

        # Env vars go inside the generated launcher script below (applied to
        # *its* process before it starts the model), not here - anything set
        # in *this* process's environment would be useless, since the model
        # runs in a separate process tree entirely (see the WMI comment).
        $envLines = ""
        if ($payload.env) {
            $payload.env.PSObject.Properties | ForEach-Object {
                $quotedValue = Quote-Literal $_.Value
                if ($_.Name -ieq "PATH") {
                    # Prepended, never replaces the rest of PATH outright - wiping
                    # it can break the child's own DLL search for anything else.
                    $envLines += "`$env:PATH = $quotedValue + ';' + `$env:PATH`r`n"
                } else {
                    $envLines += "Set-Item -Path 'Env:$($_.Name)' -Value $quotedValue`r`n"
                }
            }
        }

        # Start-Process here (or anything else spawned directly in this SSH
        # session) stays inside the Job Object Windows' OpenSSH assigns to
        # the session, and gets killed the instant the session closes -
        # which happens right after this script returns, so the model would
        # die moments after "successfully" starting. Instead, write a small
        # launcher script that does the actual Start-Process call and hand
        # *that* to WMI (Win32_Process.Create) to run: WMI's own process
        # tree (WmiPrvSE as the parent) sits entirely outside the SSH
        # session's job, so the launcher - and the Start-Process child it
        # creates - both survive past the SSH session ending. The launcher
        # writes its own PID to $pidPath directly once Start-Process returns,
        # since it's the one that actually knows it; polling below just
        # waits for that file to show up. Args/paths are embedded as
        # PowerShell single-quoted literals (')''( doubling for escaping)
        # rather than passed inline on WMI's CommandLine, since cmd.exe's own
        # quoting rules get unreliable once several quoted arguments and
        # redirection operators are combined into one command-line string.
        $argListLiteral = "@(" + (($argList | ForEach-Object { Quote-Literal $_ }) -join ", ") + ")"
        $workingDirLiteral = ""
        if ($cwd) { $workingDirLiteral = "-WorkingDirectory $(Quote-Literal $cwd) " }
        $launcherDir = Join-Path $root "launchers"
        New-Item -ItemType Directory -Force -Path $launcherDir | Out-Null
        $launcherPath = Join-Path $launcherDir "$name.ps1"
        $launcherScript = @"
$envLines`$proc = Start-Process -FilePath $(Quote-Literal $binary) -ArgumentList $argListLiteral $workingDirLiteral-RedirectStandardOutput $(Quote-Literal $logPath) -RedirectStandardError $(Quote-Literal $errLogPath) -WindowStyle Hidden -PassThru
Set-Content -Path $(Quote-Literal $pidPath) -Value `$proc.Id
"@
        Set-Content -Path $launcherPath -Value $launcherScript -Encoding UTF8

        $wmiCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$launcherPath`""
        $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $wmiCommand }
        if ($created.ReturnValue -ne 0) {
            Write-Error "failed to launch $name via WMI (Win32_Process.Create ReturnValue=$($created.ReturnValue))"
            exit 1
        }

        $modelPid = $null
        for ($i = 0; $i -lt 100; $i++) {
            if (Test-Path $pidPath) { $modelPid = Get-Content $pidPath; break }
            Start-Sleep -Milliseconds 100
        }
        if (-not $modelPid) {
            Write-Error "$name did not report a PID within 10s of launching"
            exit 1
        }

        @{ pid = [int]$modelPid; log_path = $logPath } | ConvertTo-Json -Compress
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
