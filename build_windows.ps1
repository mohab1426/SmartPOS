# SmartPOS - Windows build, smoke test and packaging. Run from the project root on Windows 10/11 x64.
# Output: release\SmartPOS_Windows_Release.zip  (+ release\smoke\*.png, release\SHA256SUMS.txt)
$ErrorActionPreference = "Stop"
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv; Check "venv" }
$Py = (Resolve-Path ".venv\Scripts\python.exe").Path
& $Py -m pip install --upgrade pip; Check "pip upgrade"
& $Py -m pip install -r requirements-dev.txt; Check "pip install"
& $Py -m ruff check .; Check "ruff"
& $Py -m pytest -q; Check "pytest"

# 1) GUI smoke test from source (isolated temp data dir; never touches a real database)
& $Py scripts\smoke_test.py --out release\smoke\source; Check "source smoke test"

# 2) Build the app and the CI-only smoke executable
& $Py -m PyInstaller --clean --noconfirm SmartPOS.spec; Check "PyInstaller (app)"
& $Py -m PyInstaller --noconfirm SmartPOS_Smoke.spec; Check "PyInstaller (smoke)"

# 3) Run the FROZEN smoke test from a different directory
$other = Join-Path $env:TEMP "smartpos_elsewhere"; New-Item -ItemType Directory -Force $other | Out-Null
Push-Location $other
& (Join-Path $PSScriptRoot "dist\SmartPOS_SmokeTest\SmartPOS_SmokeTest.exe") --out (Join-Path $PSScriptRoot "release\smoke\frozen")
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { throw "Frozen smoke test failed (exit $code)" }

# 4) Launch the real SmartPOS.exe with an isolated data dir; it must stay alive at the login screen
$env:SMARTPOS_DATA_DIR = Join-Path $env:TEMP "smartpos_launch_check"
Remove-Item -Recurse -Force $env:SMARTPOS_DATA_DIR -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath "dist\SmartPOS\SmartPOS.exe" -PassThru
Start-Sleep -Seconds 12
$alive = -not $proc.HasExited
if ($alive) { Stop-Process -Id $proc.Id -Force }
Remove-Item Env:\SMARTPOS_DATA_DIR
if (-not $alive) { throw "SmartPOS.exe exited during startup (exit $($proc.ExitCode))" }

# 5) Make sure no database / secret was bundled
$bad = Get-ChildItem -Recurse dist\SmartPOS -Include *.db,*.sqlite,*.sqlite3,*.db-wal,.env
if ($bad) { throw "Database/secret files found in distribution: $($bad.FullName -join ', ')" }

# 6) Package
Copy-Item docs\RELEASE_README.txt dist\SmartPOS\README_FIRST.txt -Force
New-Item -ItemType Directory -Force release | Out-Null
$zip = "release\SmartPOS_Windows_Release.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path dist\SmartPOS -DestinationPath $zip
Get-FileHash $zip -Algorithm SHA256 | ForEach-Object { "$($_.Hash.ToLower())  SmartPOS_Windows_Release.zip" } | Set-Content release\SHA256SUMS.txt -Encoding ascii
& $Py --version | Out-File release\build_env.txt; & $Py -m PyInstaller --version | Out-File -Append release\build_env.txt
[System.Environment]::OSVersion.VersionString | Out-File -Append release\build_env.txt
Write-Host "DONE: $zip"
