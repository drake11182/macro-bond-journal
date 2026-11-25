@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo 필요한 파이썬 패키지 설치 중...
python -m pip install -q -r requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "pc\setup.ps1"
pause
