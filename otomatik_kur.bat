@echo off
setlocal
cd /d "%~dp0"
schtasks /Create /TN "Streetwear Yeni Urun Takibi" /SC HOURLY /MO 6 /TR "\"%~dp0gorev.bat\"" /RL LIMITED /F
if errorlevel 1 (
  echo Gorev olusturulamadi. Bu dosyayi kendi Windows hesabinizla calistirin.
) else (
  echo Gorev olusturuldu: oturum acikken 6 saatte bir kontrol eder. Sonuclar takvim.log dosyasindadir.
)
pause
endlocal
