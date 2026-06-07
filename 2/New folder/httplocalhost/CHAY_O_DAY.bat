@echo off
echo ============================================
echo   Hardware Info - CPU-Z / Speccy style
echo ============================================
echo.
echo Dang cai thu vien can thiet...
pip install psutil py-cpuinfo wmi -q
echo.
echo Khoi dong server...
python server.py
pause
