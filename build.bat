@echo off
REM Builds dist\AndroidKitchen.exe (single file, no console window).
setlocal
cd /d "%~dp0"
python -m pip install --upgrade -r requirements.txt pyinstaller || goto :err
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name AndroidKitchen ^
  --hidden-import lz4.frame --hidden-import lz4.block ^
  --hidden-import brotli --hidden-import zstandard ^
  AndroidKitchen.py || goto :err
echo.
echo Built: %~dp0dist\AndroidKitchen.exe
exit /b 0
:err
echo Build failed.
exit /b 1
