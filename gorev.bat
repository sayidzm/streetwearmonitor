@echo off
setlocal
cd /d "%~dp0"
echo.>> takvim.log
echo [%date% %time%] Otomatik kontrol basladi>> takvim.log
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 monitor.py >> takvim.log 2>&1
) else (
  where python >nul 2>&1
  if %errorlevel%==0 (python monitor.py >> takvim.log 2>&1) else (echo Python bulunamadi. Python 3.10+ kurun ve PATH'e ekleyin.>> takvim.log)
)
echo [%date% %time%] Otomatik kontrol bitti, kod: %errorlevel%>> takvim.log
endlocal
