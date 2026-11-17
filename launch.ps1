param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$VideoPaths = @()
)

$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskLogs = Join-Path $taskRoot 'logs'
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$taskLaunchLog = Join-Path $taskLogs "launcher-$taskStamp-$PID.log"

function Show-LaunchError([string]$Message) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($Message, 'Upscale could not start', 'OK', 'Error') | Out-Null
    } catch {
        Write-Error $Message -ErrorAction Continue
    }
}

function Quote-ProcessArgument([string]$Value) {
    # Windows CommandLineToArgvW quoting, including trailing backslashes.
    return '"' + ([regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1')) + '"'
}

try {
    New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
    $taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
    $taskPythonW = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
    $taskMain = Join-Path $taskRoot 'main.py'
    foreach ($taskRequired in @($taskPython, $taskPythonW, $taskMain)) {
        if (-not (Test-Path -LiteralPath $taskRequired -PathType Leaf)) {
            throw "Missing $taskRequired`nRun Setup.ps1 from this folder first."
        }
    }

    $taskCheckOutputLog = Join-Path $taskLogs "check-$taskStamp-$PID.stdout.log"
    $taskCheckErrorLog = Join-Path $taskLogs "check-$taskStamp-$PID.stderr.log"
    $taskCheckArgs = @('-c', 'import PySide6.QtWidgets, numpy, cv2, av')
    $taskCheckCommandLine = ($taskCheckArgs | ForEach-Object { Quote-ProcessArgument $_ }) -join ' '
    $taskCheckProcess = Start-Process -FilePath $taskPythonW -ArgumentList $taskCheckCommandLine -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput $taskCheckOutputLog -RedirectStandardError $taskCheckErrorLog -Wait -PassThru
    if ($taskCheckProcess.ExitCode -ne 0) {
        $taskCheckDetail = Get-Content -LiteralPath $taskCheckErrorLog -Tail 16 -ErrorAction SilentlyContinue
        throw "Python packages could not load. Run Setup.ps1 to repair them.`n$($taskCheckDetail -join [Environment]::NewLine)`nLog: $taskCheckErrorLog"
    }
    foreach ($taskTool in @('ffmpeg', 'ffprobe')) {
        $taskOverride = [Environment]::GetEnvironmentVariable("DLSS_$($taskTool.ToUpperInvariant())_PATH")
        if ($taskOverride) {
            if (-not (Test-Path -LiteralPath $taskOverride -PathType Leaf)) {
                throw "DLSS_$($taskTool.ToUpperInvariant())_PATH points to a missing file: $taskOverride"
            }
        } elseif (-not (Get-Command "$taskTool.exe" -CommandType Application -ErrorAction SilentlyContinue)) {
            throw "$taskTool.exe is missing from PATH. Set DLSS_$($taskTool.ToUpperInvariant())_PATH to its full path, then relaunch Upscale. See README.md."
        }
    }

    $taskOutputLog = Join-Path $taskLogs "app-$taskStamp-$PID.stdout.log"
    $taskErrorLog = Join-Path $taskLogs "app-$taskStamp-$PID.stderr.log"
    $taskArgs = @('-u', $taskMain) + @($VideoPaths)
    $taskCommandLine = ($taskArgs | ForEach-Object { Quote-ProcessArgument $_ }) -join ' '
    $taskProcess = Start-Process -FilePath $taskPythonW -ArgumentList $taskCommandLine -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput $taskOutputLog -RedirectStandardError $taskErrorLog -PassThru
    $taskProcess.Handle | Out-Null
    $taskProcess.WaitForExit()
    if ($taskProcess.ExitCode -ne 0) {
        $taskDetail = Get-Content -LiteralPath $taskErrorLog -Tail 16 -ErrorAction SilentlyContinue
        throw "Upscale exited with code $($taskProcess.ExitCode).`n$($taskDetail -join [Environment]::NewLine)`nLog: $taskErrorLog"
    }
} catch {
    $taskMessage = $_.Exception.Message
    try { $taskMessage | Set-Content -LiteralPath $taskLaunchLog -Encoding UTF8 } catch { }
    Show-LaunchError "$taskMessage`n`nLauncher log: $taskLaunchLog"
    exit 1
}
