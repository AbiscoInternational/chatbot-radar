@echo off
REM Quick start script for Windows

echo.
echo ========================================
echo ChatbotRadar - Local Development Setup
echo ========================================
echo.

echo Checking Python version...
python --version
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.11+
    pause
    exit /b 1
)

echo.
echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Generating secret key...
for /f %%i in ('python -c "import secrets; print(secrets.token_hex(32))"') do set SECRET_KEY=%%i
echo Secret key generated

echo.
echo Starting Flask development server...
echo.
echo Your app will be available at: http://localhost:5001
echo Press CTRL+C to stop the server
echo.
echo ========================================
echo.

python app.py
pause
