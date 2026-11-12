# Legt eine Desktop-Verknuepfung "FritzConfigurator" mit eigenem Symbol an.
# Ziel: FritzConfigurator.exe (falls gebaut), sonst pythonw.exe + FritzConfigurator.pyw (ohne Konsolenfenster).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$ico  = Join-Path $root 'FritzConfigurator.ico'
$exe  = Join-Path $root 'FritzConfigurator.exe'
$pyw  = Join-Path $root 'FritzConfigurator.pyw'

if (Test-Path $exe) {
    $target = $exe; $arguments = ''
} else {
    $pythonw = $null
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $exePath = (& $py.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
        if ($exePath) {
            $cand = Join-Path (Split-Path -Parent $exePath.Trim()) 'pythonw.exe'
            if (Test-Path $cand) { $pythonw = $cand }
        }
    }
    if (-not $pythonw) {
        $cmd = Get-Command pythonw -ErrorAction SilentlyContinue
        if ($cmd) { $pythonw = $cmd.Source }
    }
    if (-not $pythonw) {
        $cmd = Get-Command pyw -ErrorAction SilentlyContinue
        if ($cmd) { $pythonw = $cmd.Source }
    }
    if (-not $pythonw) { throw 'Python (pythonw.exe) nicht gefunden - bitte Python 3 installieren oder build_exe.bat ausfuehren.' }
    $target = $pythonw; $arguments = '"' + $pyw + '"'
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'FritzConfigurator.lnk'
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $target
$lnk.Arguments = $arguments
$lnk.WorkingDirectory = $root
$lnk.Description = 'FRITZ!Box Vorkonfiguration'
if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
$lnk.Save()
Write-Output "Verknuepfung erstellt: $lnkPath"
Write-Output "Ziel: $target $arguments"
