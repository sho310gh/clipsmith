@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat python -m venv .venv
call .venv\Scripts\activate.bat
if not exist .venv\.deps_ok (
  echo Installing dependencies - first run only, this can take a few minutes...
  python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
  if errorlevel 1 (
    echo Install failed - see the messages above.
    pause
    exit /b 1
  )
  echo ok> .venv\.deps_ok
)
echo ClipSmith: open http://127.0.0.1:5000 in your browser
python app.py
pause
