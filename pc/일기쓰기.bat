@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo [1/2] 최신 일기 가져오는 중...
git pull --rebase --autostash
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
set F=journal\%TODAY:~0,4%\%TODAY:~5,2%\%TODAY%.md
if not exist "%F%" (
  echo.
  echo 오늘 일기가 아직 없습니다. 평일 10시 / 주말 16시쯤 생성됩니다.
  echo 지금 만들려면 GitHub 저장소 - Actions - "매일 일기" - Run workflow
  pause
  exit /b
)
echo [2/2] 오늘 일기 열기: %F%
echo  - "내 코멘트:" 뒤에 코멘트, "나의 뷰"에 방향을 채우세요.
echo  - 다 쓰면 저장(Ctrl+S) 후 "저장하기.bat" 실행
where code >nul 2>nul && (code "%CD%" "%F%") || (start "" notepad "%F%")
