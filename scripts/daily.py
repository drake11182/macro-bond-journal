"""매일 일기 생성: journal/YYYY/MM/YYYY-MM-DD.md

평일(오전 10시): 지표 + 일정 + 핵심 포인트 10개 내외 + 코멘트 칸 + 나의 뷰 칸
주말(오후 4시): 다음 주 이벤트 위주 + 주간 이슈 + 코멘트 칸

python scripts/daily.py                # 오늘 (평일/주말 자동)
python scripts/daily.py --mode weekend
python scripts/daily.py --force        # 이미 있는 파일을 덮어씀 (코멘트 사라짐 주의)
"""
from __future__ import annotations

import argparse
import datetime as dt

import market
from collect import inbox_since
from common import (claude_json, config, fmt_date, has_claude, journal_path, notify, now_kst,
                    read_state, repo_url, write_state)
from sources import all_channel_posts, events_table, news_items

ICON = {"채권시장": "🏦", "금리": "📈", "크레딧": "💳", "한국": "🇰🇷", "미국": "🇺🇸",
        "FX": "💱", "이벤트": "📅"}
CIRCLE = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
COMMENT = "> 💬 **내 코멘트:** "

VIEW_BLOCK = """<!-- VIEW-START : 아래 값만 채우면 주간/월간 리뷰에서 자동 채점됩니다 -->
<!-- 방향: 강세 / 약세 / 중립   ·   커브: 스티프닝 / 플래트닝 / 중립   ·   기간: 1주 / 1개월 -->
- 국고 3Y 방향:
- 국고 10Y 방향:
- 커브(10Y-3Y):
- 기간: 1주
- 확신도(1~5):
- 근거:
<!-- VIEW-END -->"""


def schema(cats: list[str], max_points: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "one_liner": {"type": "string", "description": "오늘 시장을 한 문장으로"},
            "market_read": {"type": "string", "description": "제공된 지표 숫자에 근거한 금리·스프레드 흐름 해설 2~4문장"},
            "points": {
                "type": "array",
                "description": f"핵심 포인트 최대 {max_points}개, 중요도 순",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": cats},
                        "title": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                        "implication": {"type": "string", "description": "채권 투자자 관점 시사점 1문장"},
                        "sources": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}, "url": {"type": "string"}},
                                "required": ["name", "url"],
                                "additionalProperties": False,
                            },
                        },
                        "starred": {"type": "boolean", "description": "⭐ 내가 고른 글에서 나온 포인트면 true"},
                    },
                    "required": ["category", "title", "bullets", "implication", "sources", "starred"],
                    "additionalProperties": False,
                },
            },
            "watch": {"type": "array", "items": {"type": "string"}, "description": "오늘/이번 주 체크포인트 2~4개"},
        },
        "required": ["one_liner", "market_read", "points", "watch"],
        "additionalProperties": False,
    }


SYSTEM = """당신은 한국 채권시장을 주로 보는 매크로·채권 애널리스트의 리서치 보조입니다.
매일 아침 사용자가 읽고 코멘트를 달 '매크로·채권 일기'의 핵심 포인트를 정리합니다.

원칙
- 국내 채권 금리(국고채 커브, 크레딧, 금통위/수급)를 가장 비중 있게, 그다음 미국 금리·매크로, FX, 원자재 순.
- 숫자는 반드시 입력에 있는 것만 사용합니다. 지어내거나 추정한 숫자를 쓰지 않습니다.
- 각 포인트의 sources에는 입력에 실제로 있는 링크만 넣습니다 (없으면 빈 배열).
- ⭐ 표시된 '내가 고른 글'은 빠짐없이 포인트에 반영하고 starred=true로 표시합니다.
- 비슷한 내용은 하나의 포인트로 합치고, 채권과 무관한 개별 종목·생활 기사는 제외합니다.
- 문장은 간결한 한국어 개조식(~함, ~음 체 또는 명사형)으로 씁니다."""


def build_prompt(mode, today, snap, events_txt, picks, posts, news, max_points) -> str:
    parts = [f"날짜: {fmt_date(today)}  모드: {'평일 장중' if mode == 'weekday' else '주말 (다음 주 이벤트 위주)'}",
             f"핵심 포인트 개수: 최대 {max_points}개"]
    if mode == "weekend":
        parts.append("주말 모드: 다음 주 일정이 채권시장에 미칠 영향과 이번 주 주요 이슈 정리에 집중하세요.")
    parts.append(f"\n## 시장 지표 (전일 종가 기준, 실제 값)\n{snap}")
    parts.append(f"\n## 경제지표 일정\n{events_txt}")
    if picks:
        parts.append("\n## ⭐ 내가 고른 글")
        for p in picks:
            line = f"- [{p['source']}] {p['title']} ({p.get('link') or '링크 없음'})"
            for b in p.get("bullets", []):
                line += f"\n  · {b}"
            if p.get("memo"):
                line += f"\n  · 내 메모: {p['memo']}"
            parts.append(line)
    parts.append("\n## 텔레그램 채널 글")
    for i, p in enumerate(posts, 1):
        parts.append(f"[T{i}] {p['source']} | {p['ts'][:16]} | {p['link']}\n{p['text']}\n")
    parts.append("\n## 한국경제 기사 (제목·요약문)")
    for i, n in enumerate(news, 1):
        parts.append(f"[N{i}] {n['title']} — {n['desc']} ({n['link']})")
    return "\n".join(parts)


def fallback_result(picks, posts, news) -> dict:
    """API 키가 없을 때: 요약 없이 링크 목록만으로 일기 틀을 만든다."""
    pts = [{"category": "채권시장", "title": p["title"], "bullets": p.get("bullets", []),
            "implication": "-", "sources": [{"name": p["source"], "url": p.get("link", "")}], "starred": True}
           for p in picks]
    for p in posts[:5]:
        pts.append({"category": "채권시장", "title": p["text"].splitlines()[0][:60], "bullets": [],
                    "implication": "-", "sources": [{"name": p["source"], "url": p["link"]}], "starred": False})
    return {"one_liner": "(API 키가 없어 AI 요약 없이 생성됨)", "market_read": "-", "points": pts, "watch": []}


def render(mode, today, data, events_md, result, picks, counts) -> str:
    cats = config()["categories"]
    title = "매크로·채권 일기" if mode == "weekday" else "주말 이벤트 체크"
    md = [f"# 📒 {fmt_date(today)} {title}", "",
          f"> **한 줄 요약** — {result['one_liner']}", "",
          f"<sub>자동 생성 {now_kst():%Y-%m-%d %H:%M} KST · 입력: 텔레그램 {counts['posts']}건 · "
          f"한경 {counts['news']}건 · ⭐내가 고른 글 {counts['picks']}건 · 지표는 전일 종가 기준</sub>", ""]

    md += ["## 1. 시장 지표", "", "### 국내 금리", market.table(data, market.KR_TABLE), "",
           "### 크레딧", market.table(data, market.CREDIT_TABLE), "",
           "### 해외 금리 · FX · 원자재", market.table(data, market.GLOBAL_TABLE), "",
           "### 주요 스프레드", market.spread_table(data), "",
           f"**🤖 AI 시장 해설** — {result['market_read']}", "", COMMENT, ""]

    md += [f"## 2. 경제지표 일정 ({'오늘~7일' if mode == 'weekday' else '다음 주'})", "", events_md, ""]
    if result.get("watch"):
        md += ["**👀 체크포인트**"] + [f"- {w}" for w in result["watch"]] + [""]

    md += ["## 3. 핵심 포인트", ""]
    pts = sorted(result["points"], key=lambda p: cats.index(p["category"]) if p["category"] in cats else 99)
    n = 0
    for cat in cats + ["기타"]:
        group = [p for p in pts if (p["category"] if p["category"] in cats else "기타") == cat]
        if not group:
            continue
        md += [f"### {ICON.get(cat, '📌')} {cat}", ""]
        for p in group:
            mark = CIRCLE[n] if n < len(CIRCLE) else f"{n + 1}."
            n += 1
            md.append(f"#### {mark} {p['title']}{' ⭐' if p['starred'] else ''}")
            md += [f"- {b}" for b in p["bullets"]]
            if p.get("implication") and p["implication"] != "-":
                md.append(f"- **채권 시사점:** {p['implication']}")
            srcs = [f"[{s['name']}]({s['url']})" if s.get("url") else s["name"] for s in p["sources"]]
            if srcs:
                md.append(f"- <sub>출처: {', '.join(srcs)}</sub>")
            md += ["", COMMENT, ""]

    memos = [p for p in picks if p.get("type") == "memo"]
    if memos:
        md += ["## 4. 📝 내 메모 (텔레그램으로 남긴 것)", ""]
        md += [f"- {dt.datetime.fromisoformat(m['ts']):%m/%d %H:%M} — {m['memo']}" for m in memos] + [""]

    md += [f"## {'5' if memos else '4'}. 오늘의 총평 & 나의 뷰", "", "### 🧭 나의 뷰", "", VIEW_BLOCK, "",
           "### ✍️ 총평", "", "> ", ""]
    return "\n".join(md)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["weekday", "weekend"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    now = now_kst()
    today = now.date()
    mode = args.mode or ("weekend" if today.weekday() >= 5 else "weekday")
    path = journal_path(today)
    if path.exists() and not args.force:
        print(f"[daily] 이미 있음: {path} (덮어쓰려면 --force)")
        return

    cfg = config()
    state = read_state()
    since = dt.datetime.fromisoformat(state["last_daily"]) if state.get("last_daily") else now - dt.timedelta(hours=24)
    since = max(since, now - dt.timedelta(days=4))

    data = market.update(days=20)
    events_md, events_txt = events_table(now, 7)
    picks = inbox_since(since)
    posts = all_channel_posts(since)
    news = news_items(since)
    max_points = cfg["max_points"] if mode == "weekday" else cfg["max_points_weekend"]

    if has_claude():
        result = claude_json(SYSTEM, build_prompt(mode, today, market.snapshot_text(data), events_txt,
                                                  picks, posts, news, max_points),
                             schema(cfg["categories"], max_points), max_tokens=16000, effort="medium")
    else:
        print("[daily] ANTHROPIC_API_KEY 없음 → 요약 없이 생성")
        result = fallback_result(picks, posts, news)

    counts = {"posts": len(posts), "news": len(news), "picks": len(picks)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(mode, today, data, events_md, result, picks, counts), encoding="utf-8")
    print(f"[daily] 작성 완료: {path}")

    state["last_daily"] = now.isoformat()
    write_state(state)
    notify(f"📒 {fmt_date(today)} 일기 준비 완료\n{result['one_liner']}\n\n✍️ 코멘트 달기: {repo_url(path)}")


if __name__ == "__main__":
    main()
