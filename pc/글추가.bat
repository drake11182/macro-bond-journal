@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set F=pc\_new_post.txt
> "%F%" echo 출처: 
echo.
echo 메모장이 열리면:
echo   1) 첫 줄 "출처:" 뒤에 출처를 적고 (예: KB 채권, 한국경제)
echo   2) 아래에 글 내용이나 기사 링크를 붙여넣고
echo   3) 저장(Ctrl+S) 후 메모장을 닫으세요.
echo   * 내 생각만 적으려면 둘째 줄을 "메모:" 로 시작하세요.
start /wait notepad "%F%"
python scripts\add.py "%F%"
del "%F%" >nul 2>nul
git add inbox
git commit -m "📥 직접 입력" >nul
git pull --rebase --autostash
git push
echo.
echo 다음 일기에 ⭐로 반영됩니다.
pause
