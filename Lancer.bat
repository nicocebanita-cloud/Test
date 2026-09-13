@echo off
REM Double-cliquez sur ce fichier pour tout lancer (Windows).
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 lancer.py %*
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python lancer.py %*
    ) else (
        echo Python est introuvable. Installez-le depuis https://www.python.org/downloads/
        echo en cochant "Add Python to PATH", puis relancez ce fichier.
    )
)
echo.
pause
