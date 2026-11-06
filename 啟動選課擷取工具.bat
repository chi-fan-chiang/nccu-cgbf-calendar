@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "C:\Users\Anderson\.gemini\antigravity\scratch\nccu-cgbf-calendar"
"C:\Users\Anderson\AppData\Local\Programs\Python\Python312\python.exe" -u cgbf_calendar.py --fresh
echo.
pause
