@echo off
REM Update these values if your Python or project path differs.
set "PROJECT_DIR=C:\Users\HP\OneDrive\Documents\BestLogMarketPlaceProject"
set "PYTHON=C:\Python313\python.exe"
set "LOG_DIR=%PROJECT_DIR%\logs"
set "LOG_FILE=%LOG_DIR%\sync_supplier_products.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
cd /d "%PROJECT_DIR%"
"%PYTHON%" manage.py sync_supplier_products >> "%LOG_FILE%" 2>&1
exit /b %ERRORLEVEL%
