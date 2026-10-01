@echo off
rem Loads KEY=VALUE lines from .env (if present), then starts the interactive bot.
rem Use "run.bat once" to send a single report instead.
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
)
if "%1"=="once" (python shop_notifier.py) else (python bot_polling.py)
