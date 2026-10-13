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
# Start-Process's -RedirectStandardOutput/-Error truncate $logPath/$errLogPath
# the instant the launched process opens, so a header written straight into
# either would just be wiped out - kept in its own sidecar instead, and the
# "logs" action below prepends it. Only the latest launch's line (not a
# history like the local-runner log gets), since each Windows launch already
# starts $logPath fresh the same way.
function Get-CmdlinePath($name) { Join-Path $logDir "$name.cmdline" }
function Get-PortPath($name) { Join-Path $pidDir "$name.port" }

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
        $cmdlinePath = Get-CmdlinePath $name
        if (Test-Path $pidPath) { Remove-Item $pidPath -ErrorAction SilentlyContinue }

        function Quote-Literal($value) { "'" + ($value -replace "'", "''") + "'" }

        function Quote-Display($value) { if ($value -match '\s') { '"' + $value + '"' } else { $value } }
        $displayArgs = ($argList | ForEach-Object { Quote-Display $_ }) -join ' '
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Set-Content -Path $cmdlinePath -Value "[$timestamp] launching: $(Quote-Display $binary) $displayArgs"

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

        # Start-Process -PassThru only confirms Windows created the process,
        # not that it stayed up - a bad launch (missing model file, port
        # already in use, a GPU/driver error, a bad CLI flag) typically
        # crashes within a second or two, well before a large model would
        # actually finish loading. Give it that long, then check again
        # before declaring success, so a caller can't be told a dead process
        # is running.
        Start-Sleep -Milliseconds 1500
        $proc = Get-Process -Id $modelPid -ErrorAction SilentlyContinue
        if (-not $proc) {
            Remove-Item $pidPath -ErrorAction SilentlyContinue
            $cmdlineText = ""
            if (Test-Path $cmdlinePath) { $cmdlineText = Get-Content -Path $cmdlinePath -Raw }
            $errTail = ""
            if (Test-Path $errLogPath) { $errTail = (Get-Content -Path $errLogPath -Tail 20) -join "`n" }
            Write-Error "$name exited immediately after launching (crashed on startup). $cmdlineText$(if ($errTail) { "`n$errTail" })"
            exit 1
        }

        if ($payload.port) {
            Set-Content -Path (Get-PortPath $name) -Value ([int]$payload.port)
        }
        @{ pid = [int]$modelPid; log_path = $logPath; port = $payload.port } | ConvertTo-Json -Compress
    }
    "stop" {
        $name = $payload.name
        $pidPath = Get-PidPath $name
        if (Test-Path $pidPath) {
            $procId = Get-Content $pidPath
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Remove-Item $pidPath -ErrorAction SilentlyContinue
        }
        Remove-Item (Get-PortPath $name) -ErrorAction SilentlyContinue
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
                $portPath = Get-PortPath $name
                $port = $null
                if (Test-Path $portPath) { $port = [int](Get-Content $portPath) }
                @{ running = $true; pid = [int]$procId; port = $port } | ConvertTo-Json -Compress
            }
            else {
                Remove-Item $pidPath -ErrorAction SilentlyContinue
                @{ running = $false } | ConvertTo-Json -Compress
            }
        }
    }
    "check-port" {
        $port = [int]$payload.port
        $conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
        if ($conn) {
            Write-Error "port $port is already in use"
            exit 1
        }
        @{ free = $true } | ConvertTo-Json -Compress
    }
    "logs" {
        $name = $payload.name
        $lines = 200
        if ($payload.lines) { $lines = [int]$payload.lines }
        $logPath = Get-LogPath $name
        $errLogPath = Get-ErrLogPath $name
        $cmdlinePath = Get-CmdlinePath $name
        if (Test-Path $cmdlinePath) { Get-Content -Path $cmdlinePath }
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
