"""README의 '최근 기록'·'뷰 적중률' 영역과 journal/INDEX.md 를 다시 만든다."""
from __future__ import annotations

import re

from common import JOURNAL, REVIEWS, ROOT, fmt_date
from review import hit_rate, journals_between, load_scorecard

import datetime as dt


def main() -> None:
    js = journals_between(dt.date(2000, 1, 1), dt.date(2100, 1, 1))
    js.sort(key=lambda j: j["date"], reverse=True)

    def mark(j):
        has_c = "💬" if j["comments"] or j["wrapup"] else "·"
        has_v = "🧭" if any(v and v != "1주" for k, v in j["views"].items() if k != "기간") else "·"
        return f"{has_c}{has_v}"

    # journal/INDEX.md — 전체 목록 (월별)
    lines = ["# 📚 전체 일기 목록", "", "💬 = 총평 작성 · 🧭 = 뷰 기록", ""]
    month = None
    for j in js:
        m = j["date"].strftime("%Y-%m")
        if m != month:
            lines += ["", f"## {m}", ""]
            month = m
        rel = j["path"].relative_to(JOURNAL).as_posix()
        lines.append(f"- {mark(j)} [{fmt_date(j['date'])}]({rel}) — {j['summary']}")
    (JOURNAL / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # README 자동 영역
    recent = [f"- {mark(j)} [{fmt_date(j['date'])}]({j['path'].relative_to(ROOT).as_posix()}) — {j['summary']}"
              for j in js[:10]] or ["- 아직 기록이 없습니다."]
    reviews = sorted(REVIEWS.glob("*/*.md"), key=lambda p: p.stem, reverse=True)[:6]
    rev = [f"- [{p.stem}]({p.relative_to(ROOT).as_posix()})" for p in reviews] or ["- 아직 리뷰가 없습니다."]
    total = len(js)
    commented = sum(1 for j in js if j["comments"] or j["wrapup"])
    block = "\n".join([
        "<!-- AUTO-START -->",
        f"**기록 {total}일 · 총평 작성 {commented}일 · 뷰 적중률 {hit_rate(load_scorecard())}**",
        "", "### 🗓️ 최근 일기", *recent, "", f"[→ 전체 목록](journal/INDEX.md)",
        "", "### 🔍 최근 리뷰", *rev,
        "<!-- AUTO-END -->",
    ])
    readme = ROOT / "README.md"
    txt = readme.read_text(encoding="utf-8")
    txt = re.sub(r"<!-- AUTO-START -->.*?<!-- AUTO-END -->", lambda _: block, txt, flags=re.S)
    readme.write_text(txt, encoding="utf-8")
    print("[index] 갱신 완료")


if __name__ == "__main__":
    main()
