@echo off
rem Запуск бота с автоперезапуском, если он упал. Лог: data\run.log
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
:loop
".venv\Scripts\python.exe" -m app.main >> data\run.log 2>&1
echo %date% %time% bot exited, restart in 30 s >> data\run.log
timeout /t 30 /nobreak >nul
goto loop
