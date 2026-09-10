#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .env/bin/python ]]; then
    echo "Fehler: Python-Umgebung \".env\" nicht gefunden."
    echo "Bitte zuerst anlegen und Abhängigkeiten installieren:"
    echo "  python -m venv .env"
    echo "  .env/bin/pip install -r requirements.txt"
    exit 1
fi

exec .env/bin/python app.py
