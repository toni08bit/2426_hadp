@echo off
cd /d "%~dp0"

if not exist ".env\Scripts\python.exe" (
    echo Fehler: Python-Umgebung ".env" nicht gefunden.
    echo Bitte zuerst anlegen und Abhaengigkeiten installieren:
    echo   python -m venv .env
    echo   .env\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

".env\Scripts\python.exe" app.py
if errorlevel 1 pause
