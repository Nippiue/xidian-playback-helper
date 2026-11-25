@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto check_dependency
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if errorlevel 1 goto python_error
:check_dependency
".venv\Scripts\python.exe" -c "import playwright" >nul 2>nul
if not errorlevel 1 goto run
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto dependency_error
:run
".venv\Scripts\python.exe" -u xidian_playback.py %*
if errorlevel 1 pause
exit /b
:python_error
echo 请先安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。
pause
exit /b 1
:dependency_error
echo 依赖安装失败，请检查网络后重新运行。
pause
exit /b 1
