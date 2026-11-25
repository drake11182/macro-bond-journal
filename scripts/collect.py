"""'내가 고른 글' 수집함.

텔레그램 봇에게 보낸 것(채널 글 포워딩, 기사 링크, 직접 쓴 메모)을 가져와서
AI로 요약한 뒤 inbox/YYYY-MM-DD.jsonl 에 저장합니다. 다음 일기에 ⭐로 표시됩니다.

봇 메시지 규칙
- 채널 글 포워딩 / 링크 / 텍스트 붙여넣기 → AI 요약해서 저장
- "메모:" 로 시작 → 내 생각 메모로 그대로 저장 (요약 안 함)
- 링크 + 한 줄 덧붙이기 → 덧붙인 말은 내 메모로 같이 저장

python scripts/collect.py                 # 봇 수집함 가져오기
python scripts/add.py 파일.txt             # PC에서 직접 넣기 (add.py 참고)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os

from common import INBOX, claude_json, config, has_claude, notify, now_kst, read_state, tg_api, write_state
from sources import extract_urls, fetch_article

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "한 줄 제목 (40자 이내)"},
        "category": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}, "description": "핵심 요약 2~4개"},
    },
    "required": ["title", "category", "bullets"],
    "additionalProperties": False,
}


def summarize(text: str, source: str) -> dict:
    cats = ", ".join(config()["categories"])
    if not has_claude():
        first = text.strip().splitlines()[0][:60] if text.strip() else "(내용 없음)"
        return {"title": first, "category": "기타", "bullets": ["(API 키가 없어 요약 생략)"]}
    return claude_json(
        system=("당신은 채권 애널리스트의 리서치 보조입니다. 입력된 글을 채권·매크로 투자자 관점에서 "
                "한국어로 짧게 요약합니다. 숫자(금리, bp, 지표)는 원문에 있는 것만 쓰고 지어내지 않습니다."),
        user=(f"출처: {source}\n분류 후보: {cats} (가장 맞는 것 하나)\n\n"
              f"<글>\n{text[:12000]}\n</글>"),
        schema=SUMMARY_SCHEMA, max_tokens=4000, effort="low",
    )


def save_item(item: dict) -> None:
    day = dt.datetime.fromisoformat(item["ts"]).date()
    INBOX.mkdir(exist_ok=True)
    with (INBOX / f"{day.isoformat()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def make_item(text: str, source: str, link: str = "", ts: dt.datetime | None = None) -> dict:
    """텍스트/링크 하나를 요약해서 inbox 아이템으로 만든다."""
    ts = ts or now_kst()
    text = (text or "").strip()
    if text.startswith(("메모:", "메모 :", "/memo")):
        memo = text.split(":", 1)[-1].strip() if ":" in text else text[5:].strip()
        return {"id": hashlib.md5(f"{ts}{memo}".encode()).hexdigest()[:10], "ts": ts.isoformat(),
                "type": "memo", "source": "내 메모", "link": "", "title": memo[:40],
                "category": "메모", "bullets": [], "memo": memo, "starred": True}

    urls = extract_urls(text)
    memo = ""
    body = text
    if urls and not link:
        link = urls[0]
    # 링크만 왔거나 링크 + 짧은 코멘트 → 기사 본문을 가져와서 요약
    stripped = text
    for u in urls:
        stripped = stripped.replace(u, "")
    stripped = stripped.strip()
    if urls and len(stripped) < 200 and "t.me/" not in urls[0]:
        title, article = fetch_article(urls[0])
        if article:
            body = f"{title}\n\n{article}"
            memo = stripped
            if "hankyung" in urls[0]:
                source = "한국경제"
    s = summarize(body, source)
    return {"id": hashlib.md5(f"{link}{text[:200]}".encode()).hexdigest()[:10], "ts": ts.isoformat(),
            "type": "pick", "source": source, "link": link, "title": s["title"],
            "category": s["category"], "bullets": s["bullets"], "memo": memo, "starred": True}


def collect_bot() -> int:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("[collect] TELEGRAM_BOT_TOKEN 없음 → 건너뜀")
        return 0
    state = read_state()
    offset = state.get("tg_offset", 0)
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "")
    updates = tg_api("getUpdates", offset=offset, timeout=0, allowed_updates=["message"]) or []
    n = 0
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        m = u.get("message")
        if not m:
            continue
        chat_id = str(m["chat"]["id"])
        if allowed and chat_id != allowed:
            print(f"[collect] 허용되지 않은 채팅 {chat_id} 무시")
            continue
        text = m.get("text") or m.get("caption") or ""
        if text.strip() in ("/start", "/help"):
            tg_api("sendMessage", chat_id=chat_id, text=(
                f"내 chat id: {chat_id}\n\n"
                "• 채널 글 포워딩 / 기사 링크 / 글 붙여넣기 → 요약해서 다음 일기에 ⭐로 넣어요\n"
                "• '메모: ...' → 내 생각 메모로 저장"))
            continue
        if not text.strip():
            continue
        is_forward = bool(m.get("forward_origin") or m.get("forward_from_chat"))
        if text.strip().startswith("/") or (not is_forward and len(text.strip()) < 15
                                             and not extract_urls(text) and not text.startswith("메모")):
            tg_api("sendMessage", chat_id=chat_id, reply_to_message_id=m["message_id"],
                   text="ℹ️ 짧은 글·명령어는 저장하지 않아요. 생각을 남기려면 '메모: ...' 로 보내주세요.")
            continue
        source, link = "직접 입력", ""
        origin = m.get("forward_origin") or {}
        if origin.get("type") == "channel":
            ch = origin.get("chat", {})
            source = ch.get("title", "텔레그램 채널")
            if ch.get("username"):
                link = f"https://t.me/{ch['username']}/{origin.get('message_id')}"
        elif m.get("forward_from_chat"):
            ch = m["forward_from_chat"]
            source = ch.get("title", "텔레그램 채널")
            if ch.get("username") and m.get("forward_from_message_id"):
                link = f"https://t.me/{ch['username']}/{m['forward_from_message_id']}"
        ts = dt.datetime.fromtimestamp(m["date"], tz=dt.timezone.utc).astimezone(now_kst().tzinfo)
        try:
            item = make_item(text, source, link, ts)
            save_item(item)
            n += 1
            tg_api("sendMessage", chat_id=chat_id, reply_to_message_id=m["message_id"],
                   text=f"✅ 저장: [{item['category']}] {item['title']}")
        except Exception as e:
            print(f"[collect] 처리 실패: {e}")
            tg_api("sendMessage", chat_id=chat_id, reply_to_message_id=m["message_id"],
                   text=f"⚠️ 저장 실패: {e}")
    state["tg_offset"] = offset
    write_state(state)
    print(f"[collect] 봇 수집 {n}건")
    return n


def inbox_since(since: dt.datetime) -> list[dict]:
    items = []
    for f in sorted(INBOX.glob("*.jsonl")):
        try:
            if dt.date.fromisoformat(f.stem) < since.date() - dt.timedelta(days=1):
                continue
        except ValueError:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                it = json.loads(line)
                if dt.datetime.fromisoformat(it["ts"]) > since:
                    items.append(it)
    return items


if __name__ == "__main__":
    collect_bot()
