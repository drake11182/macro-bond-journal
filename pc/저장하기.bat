@echo off
chcp 65001 >nul
cd /d "%~dp0.."
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
git add -A
git commit -m "💬 %TODAY% 코멘트"
git pull --rebase
git push
if errorlevel 1 (
  echo.
  echo [!] 업로드 실패. 인터넷 연결을 확인하거나 이 창 내용을 캡처해서 물어보세요.
) else (
  echo.
  echo 저장 완료! GitHub에 올라갔습니다.
)
pause
