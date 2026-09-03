@echo off
cd /d "%~dp0"

if not exist ".env" (
    echo.
    echo  [.env fehlt] Bitte legen Sie eine .env-Datei an ^(siehe .env.example^).
    echo.
    pause
    exit /b 1
)

rem KEY=VALUE aus .env in die Umgebung laden (ohne Anführungszeichen um die Werte).
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
)

python app.py
if errorlevel 1 pause
