@echo off
title Deal Finder
cd /d "%~dp0"

:: Controlla se Python e' installato
python --version >nul 2>&1
if errorlevel 1 (
    echo Python non trovato! Installa Python 3.10+ da python.org
    pause
    exit /b 1
)

:: Crea venv se non esiste
if not exist "venv" (
    echo Creazione ambiente virtuale...
    python -m venv venv
)

:: Attiva venv e installa dipendenze
call venv\Scripts\activate.bat
pip install -r requirements.txt -q

:: Avvia l'applicazione
python run.py

pause
