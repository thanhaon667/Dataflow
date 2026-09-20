<#
.SYNOPSIS
  Creates the "ERP Desk" Desktop + Start Menu shortcuts (with the generated app icon).

.DESCRIPTION
  * (Re)generates desktop\static\app.ico with the project's Python (Pillow if available,
    otherwise a stdlib fallback) - the icon is code, not a hand-made binary.
  * Creates ERP Desk.lnk on the Desktop and in the Start Menu. The shortcut runs
    venv\Scripts\pythonw.exe -m desktop with the project folder as working directory, so
    no console window appears.
  * Safe to re-run (shortcuts are overwritten). -Uninstall removes them again.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File desktop\install_shortcut.ps1
  powershell -ExecutionPolicy Bypass -File desktop\install_shortcut.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [switch]$SkipDesktop,
    [switch]$SkipStartMenu,
    [string]$Name = "ERP Desk",
    # Overrides, mainly for testing without touching the real Desktop / Start Menu.
    [string]$DesktopDir = [Environment]::GetFolderPath("Desktop"),
    [string]$StartMenuDir = (Join-Path ([Environment]::GetFolderPath("Programs")) "")
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$pythonw = Join-Path $root "venv\Scripts\pythonw.exe"
$icon = Join-Path $root "desktop\static\app.ico"

$targets = @()
if (-not $SkipDesktop)   { $targets += Join-Path $DesktopDir "$Name.lnk" }
if (-not $SkipStartMenu) { $targets += Join-Path $StartMenuDir "$Name.lnk" }

if ($Uninstall) {
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "Removed $t" } else { Write-Host "Not found: $t" }
    }
    return
}

if (-not (Test-Path $pythonw)) {
    throw "Cannot find $pythonw - create the virtual environment first (see README: First-time setup)."
}

Push-Location $root
try { & $python -m desktop.make_icon | Out-Host } finally { Pop-Location }
if (-not (Test-Path $icon)) { throw "Icon was not generated: $icon" }

$shell = New-Object -ComObject WScript.Shell
foreach ($t in $targets) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $t) | Out-Null
    $lnk = $shell.CreateShortcut($t)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m desktop"
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$icon,0"
    $lnk.Description = "ERP Desk - live project reporting and management"
    $lnk.WindowStyle = 7   # minimized: pythonw has no window anyway
    $lnk.Save()
    Write-Host "Created $t"
}
Write-Host ""
Write-Host "Done. Launch '$Name' from the Desktop or the Start Menu (search for '$Name')."
