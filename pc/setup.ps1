# 처음 한 번: API 키를 입력받아 (1) 내 PC의 .env 파일 (2) GitHub Secrets 에 저장
# 입력한 키는 화면에 표시되지 않고, 채팅이나 저장소에 올라가지 않습니다.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Read-Secret($label) {
    $s = Read-Host -AsSecureString $label
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b).Trim() }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

$keys = [ordered]@{
    "ANTHROPIC_API_KEY"  = "Claude API 키 (sk-ant-...)"
    "ECOS_API_KEY"       = "한국은행 ECOS 인증키"
    "TELEGRAM_BOT_TOKEN" = "텔레그램 봇 토큰 (123456:ABC...)"
    "TELEGRAM_CHAT_ID"   = "내 텔레그램 chat id (봇에게 /start 보내면 알려줌, 모르면 엔터)"
}

$existing = @{}
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match "^\s*([A-Z_]+)\s*=\s*(.*)$") { $existing[$matches[1]] = $matches[2] }
    }
}

Write-Host ""
Write-Host "이미 저장된 값은 엔터만 누르면 그대로 유지됩니다." -ForegroundColor Cyan
foreach ($k in $keys.Keys) {
    $v = Read-Secret ("{0}" -f $keys[$k])
    if ($v) { $existing[$k] = $v }
}

$lines = foreach ($k in $keys.Keys) { if ($existing[$k]) { "$k=$($existing[$k])" } }
[IO.File]::WriteAllLines((Join-Path $root ".env"), [string[]]$lines)
Write-Host ".env 저장 완료 (이 파일은 GitHub에 올라가지 않습니다)" -ForegroundColor Green

if (Get-Command gh -ErrorAction SilentlyContinue) {
    # .env 파일에서 바로 등록 (파이프로 넘기면 PowerShell이 값 앞에 BOM을 붙이는 문제 방지)
    gh secret set -f (Join-Path $root ".env")
    if ($LASTEXITCODE -eq 0) { Write-Host "GitHub Secrets 등록 완료" -ForegroundColor Green }
    else { Write-Host "GitHub Secrets 등록 실패 - 이 화면을 캡처해서 물어보세요" -ForegroundColor Red }
} else {
    Write-Host "gh(GitHub CLI)가 없어 Secrets 등록은 건너뜀 → SETUP.md의 웹 등록 방법 참고" -ForegroundColor Yellow
}
