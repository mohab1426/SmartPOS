SmartPOS - Windows portable release
===================================
Requirements: Windows 10/11 64-bit. Python is NOT required.

1. Extract the whole SmartPOS folder anywhere (e.g. C:\SmartPOS or your Desktop). Do not run from inside the zip.
2. Double-click SmartPOS.exe.
3. First launch: create the administrator account when asked, then log in.

Your data is NOT stored in this folder. It lives in:
  %LOCALAPPDATA%\SmartPOS\data      (database, backups, logs)
  %APPDATA%\SmartPOS                (settings)
Updating = extract the new version over/beside the old one; your data is untouched.
Back up %LOCALAPPDATA%\SmartPOS\data\smartpos.db regularly (the app has a backup button for admins).

If Windows SmartScreen warns about an unknown publisher, choose "More info" > "Run anyway" (the EXE is not code-signed).
If the app fails to start, check %LOCALAPPDATA%\SmartPOS\data\logs\smartpos.log.
