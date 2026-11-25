<#
.SYNOPSIS
    Create a Desktop shortcut for O-Cleaner with its custom icon.
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root 'O-Cleaner.exe'
$bat = Join-Path $root '启动 O-Cleaner.bat'
$target = if (Test-Path $exe) { $exe } else { $bat }
$ico = Join-Path $root 'web\icon.ico'
$desk = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desk 'O-Cleaner.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath = $target
$sc.WorkingDirectory = $root
$sc.Description = 'O-Cleaner Disk Space Analyzer & Safe Cleaner'
if (Test-Path $ico) { $sc.IconLocation = $ico }
if ($target -eq $bat) { $sc.WindowStyle = 7 }  # Minimized console when launching via .bat
$sc.Save()
Write-Host "Created Desktop shortcut: $lnkPath"
