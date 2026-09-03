@echo off
cd /d "%~dp0"

if not exist ".env\Scripts\pythonw.exe" (
    echo Fehler: Python-Umgebung ".env" nicht gefunden.
    echo Bitte zuerst anlegen und Abhaengigkeiten installieren:
    echo   python -m venv .env
    echo   .env\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem pythonw = kein Konsolenfenster; start schließt die BAT-CMD sofort
start "" "%~dp0.env\Scripts\pythonw.exe" "%~dp0app.py"
exit /b 0
