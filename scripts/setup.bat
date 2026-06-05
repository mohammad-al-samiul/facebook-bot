@echo off
REM One-time setup for Windows (run from project root)
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
echo.
echo Setup done. Activate with: .venv\Scripts\activate
echo Then run: python scripts\check_ollama.py
echo           python scripts\run_agent_brain.py
