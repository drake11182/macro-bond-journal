"""공통 유틸: 경로, 시간(KST), 설정, 상태 파일, Claude 호출, 텔레그램 알림."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INBOX = ROOT / "inbox"
JOURNAL = ROOT / "journal"
REVIEWS = ROOT / "reviews"
STATE_FILE = DATA / "state.json"
UA = {"User-Agent": "Mozilla/5.0 (macro-bond-journal)"}
WEEKDAYS = "월화수목금토일"


def load_env() -> None:
    """로컬 실행용: 프로젝트 루트의 .env 파일을 환경변수로 읽는다 (GitHub에서는 Secrets 사용)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# 복사·붙여넣기로 섞여 들어오는 보이지 않는 문자(BOM, 공백) 제거
for _k in ("ANTHROPIC_API_KEY", "ECOS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    if _k in os.environ:
        os.environ[_k] = os.environ[_k].replace("﻿", "").strip()


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def fmt_date(d: dt.date) -> str:
    return f"{d.isoformat()} ({WEEKDAYS[d.weekday()]})"


def config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def read_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def write_state(state: dict) -> None:
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def journal_path(d: dt.date) -> Path:
    return JOURNAL / f"{d.year}" / f"{d.month:02d}" / f"{d.isoformat()}.md"


def repo_url(path: Path | None = None) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return str(path) if path else ""
    base = f"https://github.com/{repo}"
    if path is None:
        return base
    return f"{base}/blob/main/{path.relative_to(ROOT).as_posix()}"


# ───────────────────────── Claude ─────────────────────────

def has_claude() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def claude_json(system: str, user: str, schema: dict, max_tokens: int = 16000,
                effort: str = "medium") -> dict:
    """Claude에게 JSON 스키마에 맞춘 답을 받는다 (structured outputs).

    거절(refusal) 시 서버 측 fallback 모델이 자동으로 이어받도록 설정.
    """
    import anthropic

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=config().get("model", "claude-opus-5-5"),
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude가 요청을 거절했습니다: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("응답이 max_tokens에서 잘렸습니다. max_tokens를 늘려주세요.")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ───────────────────────── Telegram ─────────────────────────

def tg_api(method: str, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    except requests.RequestException as e:   # 에러 메시지에 토큰이 든 URL이 찍히지 않도록
        raise RuntimeError(f"텔레그램 연결 실패 ({method}): {type(e).__name__}") from None
    if not r.ok:
        desc = r.json().get("description", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        raise RuntimeError(f"텔레그램 API 오류 ({method}): HTTP {r.status_code} {desc}")
    return r.json().get("result")


NOTIFY_QUEUE = ROOT / ".notify_queue"


def notify(text: str) -> None:
    """텔레그램으로 알림. GitHub Actions에서는 push가 끝난 뒤 보내도록 큐에 쌓아둔다."""
    if os.environ.get("DEFER_NOTIFY"):
        with NOTIFY_QUEUE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(text, ensure_ascii=False) + "\n")
        return
    send_now(text)


def send_now(text: str) -> None:
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        print("[notify] TELEGRAM_CHAT_ID 없음 → 알림 생략")
        return
    try:
        tg_api("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True)
    except Exception as e:  # 알림 실패가 전체 작업을 망치지 않도록
        print(f"[notify] 실패: {e}")


def flush_notify() -> None:
    if not NOTIFY_QUEUE.exists():
        return
    for line in NOTIFY_QUEUE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            send_now(json.loads(line))
    NOTIFY_QUEUE.unlink()


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["flush-notify"]:
        flush_notify()
