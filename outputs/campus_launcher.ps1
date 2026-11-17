# Shared by the VBS and diagnostic BAT. No machine-specific Python paths.
param(
    [switch]$Diagnose,
    [switch]$Autostart,
    [string]$PythonExecutable = $env:CAMPUS_PYTHON
)
$ErrorActionPreference = 'Stop'
$folder = $PSScriptRoot
$logFile = Join-Path $folder 'data\launcher.log'

function Write-LaunchLog([string]$message) {
    $line = ('{0:yyyy-MM-dd HH:mm:ss zzz} {1}' -f (Get-Date), $message)
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    if ($Diagnose) { Write-Host $line }
}

function Quote-Argument([string]$value) {
    # Windows CommandLineToArgvW/CRT escaping, including trailing backslashes.
    $value = [regex]::Replace($value, '(\\*)"', '$1$1\"')
    $value = [regex]::Replace($value, '(\\+)$', '$1$1')
    return '"' + $value + '"'
}

function Get-PythonCandidates {
    if ($PythonExecutable) {
        [pscustomobject]@{ File = $PythonExecutable; Prefix = @() }
        return
    }
    foreach ($base in @((Join-Path $folder '.venv\Scripts'), $env:VIRTUAL_ENV, $env:CONDA_PREFIX)) {
        if ($base) {
            foreach ($relative in @('python.exe', 'Scripts\python.exe')) {
                $file = Join-Path $base $relative
                if (Test-Path -LiteralPath $file -PathType Leaf) {
                    [pscustomobject]@{ File = $file; Prefix = @() }
                }
            }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
        foreach ($command in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            # Do not open the Microsoft Store for an uninstalled execution alias.
            if ($command.Source -like '*\Microsoft\WindowsApps\*') { continue }
            $prefix = @()
            if ($name -eq 'py.exe') { $prefix = @('-3') }
            [pscustomobject]@{ File = $command.Source; Prefix = $prefix }
        }
    }
    # PEP 514: per-user/all-user installs, including installs not added to PATH.
    foreach ($key in @('HKCU:\Software\Python', 'HKLM:\Software\Python',
                       'HKLM:\Software\WOW6432Node\Python')) {
        if (-not (Test-Path -LiteralPath $key)) { continue }
        foreach ($company in @(Get-ChildItem -LiteralPath $key -ErrorAction SilentlyContinue)) {
            foreach ($version in @(Get-ChildItem -LiteralPath $company.PSPath -ErrorAction SilentlyContinue)) {
                $install = Get-Item -LiteralPath ($version.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
                if ($install) {
                    $file = $install.GetValue('ExecutablePath')
                    if (-not $file -and $install.GetValue('')) {
                        $file = Join-Path $install.GetValue('') 'python.exe'
                    }
                    if ($file) { [pscustomobject]@{ File = $file; Prefix = @() } }
                }
            }
        }
    }
}

function Test-Python($candidate) {
    $probe = 'import sys,json; assert sys.version_info >= (3,10), "Python 3.10+ required"; import tkinter; from tkinter import ttk; r=tkinter.Tk(); r.withdraw(); r.update_idletasks(); r.destroy(); print(json.dumps({"executable":sys.executable,"version":sys.version.split()[0]}))'
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $candidate.File
    $info.Arguments = ((@($candidate.Prefix) + @('-I', '-c', $probe)) | ForEach-Object { Quote-Argument $_ }) -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(8000)) {
            $process.Kill()
            $process.WaitForExit()
            throw 'Python check timed out.'
        }
        if ($process.ExitCode -ne 0) { throw $errors.Result.Trim() }
        $result = $output.Result | ConvertFrom-Json
        if (-not (Test-Path -LiteralPath $result.executable -PathType Leaf)) {
            throw 'Python did not report a usable executable.'
        }
        return $result
    } catch {
        Write-LaunchLog ('Rejected Python: {0} - {1}' -f $candidate.File, $_.Exception.Message)
        return $null
    } finally {
        $process.Dispose()
    }
}

try {
    [void][System.IO.Directory]::CreateDirectory((Split-Path $logFile))
    # One bounded diagnostic file per launch, not an ever-growing probe log.
    Set-Content -LiteralPath $logFile -Value '' -Encoding UTF8
    foreach ($name in @('campus_keepalive.py', 'campus_core.py', 'campus_tray.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $folder $name) -PathType Leaf)) {
            throw "$name is missing. Extract the complete Windows package first."
        }
    }
    $python = $null
    $seen = @{}
    foreach ($candidate in @(Get-PythonCandidates)) {
        if ($seen.ContainsKey($candidate.File)) { continue }
        $seen[$candidate.File] = $true
        $python = Test-Python $candidate
        if ($python) { break }
    }
    if (-not $python) {
        throw 'No working Python 3.10+ with Tkinter was found. Install Python with Tcl/Tk, or set CAMPUS_PYTHON to the full path of python.exe. Anaconda and py.exe are not required.'
    }
    Write-LaunchLog ('Python {0}: {1}' -f $python.version, $python.executable)
    $windowed = Join-Path (Split-Path $python.executable) 'pythonw.exe'
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $python.executable
    if (Test-Path -LiteralPath $windowed -PathType Leaf) { $info.FileName = $windowed }
    # Match the probe's environment isolation, but retain imports beside the script.
    $info.Arguments = '-E -s ' + (Quote-Argument (Join-Path $folder 'campus_keepalive.py'))
    if ($Autostart) { $info.Arguments += ' --autostart' }
    $info.WorkingDirectory = $folder
    $info.UseShellExecute = $false
    # Also works when a Python distribution does not provide pythonw.exe.
    $info.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $childId = $process.Id
    $process.Dispose()
    Write-LaunchLog "Started independent GUI process: $childId. This launcher can be closed."
    exit 0
} catch {
    $message = $_.Exception.Message
    try { Write-LaunchLog ('ERROR: ' + $message) } catch { }
    [Console]::Error.WriteLine($message)
    exit 1
}
