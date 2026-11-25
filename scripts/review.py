"""주간·월간 리뷰 + 뷰 채점 + AI 검증 세션.

- 일기의 '나의 뷰' 블록을 읽어서, 기간(1주/1개월)이 지난 뷰를 실제 금리 변화로 자동 채점
  → data/view_scorecard.csv 에 누적
- AI가 당시 근거·코멘트와 실제 결과를 비교해 '무엇이 맞았고 무엇을 놓쳤는지' 검증
- 결과: reviews/weekly/2026-W48.md, reviews/monthly/2026-11.md (아래에 내 회고 칸)

python scripts/review.py weekly            # 오늘 기준 지난 7일
python scripts/review.py monthly           # 지난달
python scripts/review.py weekly --date 2026-11-29
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from pathlib import Path

import market
from common import (DATA, JOURNAL, REVIEWS, ROOT, claude_json, config, fmt_date, has_claude, notify,
                    now_kst, repo_url)

SCORECARD = DATA / "view_scorecard.csv"
SC_FIELDS = ["view_date", "target", "view", "horizon_days", "conviction", "base_date", "base",
             "end_date", "end", "change_bp", "result", "reason"]
TARGETS = {"국고 3Y 방향": ("ktb3y", "dir"), "국고 10Y 방향": ("ktb10y", "dir"),
           "커브(10Y-3Y)": ("ktb_10_3", "curve")}
COMMENT_RE = re.compile(r"^> 💬 \*\*내 코멘트:\*\*\s*(.*)$")


# ───────────────────────── 일기 파싱 ─────────────────────────

def parse_journal(path: Path) -> dict:
    txt = path.read_text(encoding="utf-8")
    day = dt.date.fromisoformat(path.stem)
    out = {"date": day, "path": path, "views": {}, "comments": [], "summary": "", "titles": [], "wrapup": ""}
    m = re.search(r"> \*\*한 줄 요약\*\* — (.*)", txt)
    out["summary"] = m.group(1).strip() if m else ""
    out["titles"] = [t.strip() for t in re.findall(r"^#### (.*)$", txt, re.M)]
    current = None
    for line in txt.splitlines():
        if line.startswith("#### "):
            current = line[5:].strip()
        cm = COMMENT_RE.match(line)
        if cm and cm.group(1).strip():
            out["comments"].append({"about": current or "시장 지표", "text": cm.group(1).strip()})
    vb = re.search(r"<!-- VIEW-START.*?-->(.*?)<!-- VIEW-END -->", txt, re.S)
    if vb:
        for line in vb.group(1).splitlines():
            mm = re.match(r"^- (.+?):\s*(.*)$", line.strip())
            if mm:
                out["views"][mm.group(1).strip()] = mm.group(2).strip()
    w = re.search(r"### ✍️ 총평\s*(.*)$", txt, re.S)
    if w:
        out["wrapup"] = "\n".join(l.lstrip("> ").rstrip() for l in w.group(1).splitlines()).strip()
    return out


def journals_between(start: dt.date, end: dt.date) -> list[dict]:
    out = []
    for p in sorted(JOURNAL.glob("*/*/*.md")):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if start <= d <= end:
            out.append(parse_journal(p))
    return out


def norm_dir(v: str) -> str | None:
    for k in ("강세", "약세", "중립", "스티프닝", "플래트닝"):
        if k in v:
            return k
    if "스팁" in v or "steep" in v.lower():
        return "스티프닝"
    if "플랫" in v or "flat" in v.lower():
        return "플래트닝"
    return None


def horizon_days(v: str) -> int:
    m = re.search(r"(\d+)\s*(주|개월|달|일)", v or "")
    if not m:
        return 7
    n, u = int(m.group(1)), m.group(2)
    return n * 7 if u == "주" else n * 30 if u in ("개월", "달") else n


# ───────────────────────── 채점 ─────────────────────────

def load_scorecard() -> list[dict]:
    if not SCORECARD.exists():
        return []
    with SCORECARD.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_scorecard(rows: list[dict]) -> None:
    rows.sort(key=lambda r: (r["view_date"], r["target"]))
    with SCORECARD.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SC_FIELDS)
        w.writeheader()
        w.writerows(rows)


def series_for(data, key: str) -> dict[str, float]:
    if key in market.SPREADS:
        _, a, b = market.SPREADS[key]
        return market.spread_series(data, a, b)
    return data.get(key, {})


def score_views(data, today: dt.date) -> list[dict]:
    """기간이 끝난 뷰 중 아직 채점 안 된 것을 채점해서 scorecard에 추가. 새로 채점된 행 반환."""
    band = config().get("view_neutral_band_bp", 5)
    rows = load_scorecard()
    done = {(r["view_date"], r["target"]) for r in rows}
    new = []
    for j in journals_between(dt.date(2000, 1, 1), today):
        hd = horizon_days(j["views"].get("기간", ""))
        maturity = j["date"] + dt.timedelta(days=hd)
        if maturity > today:
            continue
        for label, (key, kind) in TARGETS.items():
            view = norm_dir(j["views"].get(label, ""))
            if not view or (j["date"].isoformat(), label) in done:
                continue
            s = series_for(data, key)
            bd, bv = market.value_on_or_before(s, (j["date"] - dt.timedelta(days=1)).isoformat())
            ed, ev = market.value_on_or_before(s, maturity.isoformat())
            if bv is None or ev is None or ed <= bd:
                continue
            chg = (ev - bv) * 100
            if kind == "dir":
                hit = {"강세": chg < -band, "약세": chg > band, "중립": abs(chg) <= band}.get(view)
            else:
                hit = {"스티프닝": chg > band, "플래트닝": chg < -band, "중립": abs(chg) <= band}.get(view)
            if hit is None:
                continue
            row = {"view_date": j["date"].isoformat(), "target": label, "view": view,
                   "horizon_days": hd, "conviction": j["views"].get("확신도(1~5)", ""),
                   "base_date": bd, "base": f"{bv:.4f}", "end_date": ed, "end": f"{ev:.4f}",
                   "change_bp": f"{chg:+.1f}", "result": "적중" if hit else "빗나감",
                   "reason": j["views"].get("근거", "")}
            rows.append(row)
            new.append(row)
    save_scorecard(rows)
    return new


def hit_rate(rows: list[dict]) -> str:
    if not rows:
        return "아직 채점된 뷰가 없습니다."
    hits = sum(r["result"] == "적중" for r in rows)
    parts = [f"**전체 {hits}/{len(rows)} ({hits / len(rows):.0%})**"]
    for label in TARGETS:
        sub = [r for r in rows if r["target"] == label]
        if sub:
            h = sum(r["result"] == "적중" for r in sub)
            parts.append(f"{label.replace(' 방향', '')} {h}/{len(sub)}")
    return " · ".join(parts)


# ───────────────────────── AI 검증 ─────────────────────────

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "period_themes": {"type": "array", "items": {"type": "string"}, "description": "기간 중 시장을 움직인 핵심 테마 3~5개"},
        "overall": {"type": "string", "description": "뷰 검증 총평 2~4문장"},
        "per_view": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "view_date": {"type": "string"},
                    "verdict": {"type": "string", "description": "결과 한 줄 평가"},
                    "right": {"type": "string", "description": "근거 중 맞았던 부분"},
                    "missed": {"type": "string", "description": "놓친 변수·틀린 가정"},
                    "lesson": {"type": "string", "description": "다음에 적용할 교훈 1문장"},
                },
                "required": ["view_date", "verdict", "right", "missed", "lesson"],
                "additionalProperties": False,
            },
        },
        "bias_patterns": {"type": "array", "items": {"type": "string"}, "description": "반복되는 판단 습관·편향 (근거가 있을 때만)"},
        "next_focus": {"type": "array", "items": {"type": "string"}, "description": "다음 기간에 확인할 것 2~4개"},
    },
    "required": ["period_themes", "overall", "per_view", "bias_patterns", "next_focus"],
    "additionalProperties": False,
}

REVIEW_SYSTEM = """당신은 채권 운용역의 투자 일지를 검토하는 시니어 매니저입니다.
사용자가 과거에 적은 금리 뷰와 근거, 코멘트를 실제 시장 결과와 비교해 냉정하지만 건설적으로 피드백합니다.
- 결과(적중/빗나감)보다 '근거의 질'을 평가합니다: 맞았어도 운이었는지, 틀렸어도 논리는 타당했는지.
- 숫자는 입력에 있는 값만 씁니다.
- 뷰나 코멘트가 비어 있으면 그 사실을 지적하고, 기록 습관에 대한 조언을 줍니다.
- 한국어 개조식으로 간결하게."""


def ai_review(kind, journals, scored, period_moves_txt) -> dict | None:
    if not has_claude():
        return None
    by_date = {j["date"].isoformat(): j for j in journals_between(dt.date(2000, 1, 1), now_kst().date())}
    parts = [f"리뷰 종류: {kind}", f"\n## 기간 중 실제 시장 변화\n{period_moves_txt}",
             "\n## 기간 중 일기 요약 (날짜 | 한 줄 요약 | 주요 포인트)"]
    for j in journals:
        parts.append(f"- {j['date']} | {j['summary']} | {' / '.join(j['titles'][:6])}")
    parts.append("\n## 채점된 뷰 (당시 근거·코멘트 포함)")
    for r in scored:
        j = by_date.get(r["view_date"])
        parts.append(f"- [{r['view_date']}] {r['target']}: {r['view']} (기간 {r['horizon_days']}일, 확신도 {r['conviction'] or '-'}) "
                     f"→ 실제 {r['change_bp']}bp ({r['base_date']}→{r['end_date']}), {r['result']}\n  근거: {r['reason'] or '(비어 있음)'}")
        if j:
            for c in j["comments"][:8]:
                parts.append(f"  코멘트({c['about'][:30]}): {c['text']}")
            if j["wrapup"]:
                parts.append(f"  총평: {j['wrapup'][:600]}")
    if not scored:
        parts.append("(이번 기간에 채점된 뷰 없음)")
    parts.append("\n## 기간 중 작성한 코멘트")
    for j in journals:
        for c in j["comments"][:10]:
            parts.append(f"- {j['date']} ({c['about'][:30]}): {c['text']}")
    return claude_json(REVIEW_SYSTEM, "\n".join(parts), REVIEW_SCHEMA, max_tokens=16000, effort="high")


# ───────────────────────── 렌더링 ─────────────────────────

MOVE_KEYS = ["ktb3y", "ktb10y", "ktb30y", "ktb_10_3", "aa_ktb3", "ust10y", "kr_us_10", "usdkrw", "wti"]


def label_unit(key):
    if key in market.SPREADS:
        return market.SPREADS[key][0], "spread"
    return market.SERIES[key][0], market.SERIES[key][3]


def period_moves(data, start: dt.date, end: dt.date) -> tuple[str, str]:
    lines = ["| 지표 | 기간 시작 | 기간 끝 | 변화 |", "|---|---:|---:|---:|"]
    txt = []
    for k in MOVE_KEYS:
        label, unit = label_unit(k)
        s = series_for(data, k)
        bd, bv = market.value_on_or_before(s, (start - dt.timedelta(days=1)).isoformat())
        ed, ev = market.value_on_or_before(s, end.isoformat())
        if bv is None or ev is None:
            continue
        if unit == "spread":
            a, b, c = f"{bv * 100:.1f}bp", f"{ev * 100:.1f}bp", f"{(ev - bv) * 100:+.1f}bp"
        else:
            a, b, c = market.fmt_value(unit, bv), market.fmt_value(unit, ev), market.fmt_change(unit, ev, bv)
        lines.append(f"| {label} | {a} <sub>{bd[5:]}</sub> | {b} <sub>{ed[5:]}</sub> | {c} |")
        txt.append(f"{label}: {a}({bd}) → {b}({ed}), {c}")
    return "\n".join(lines), "\n".join(txt)


def render(kind, label, start, end, data, journals, scored, all_rows, ai) -> str:
    title = "주간 리뷰" if kind == "weekly" else "월간 리뷰"
    moves_md, _ = period_moves(data, start, end)
    md = [f"# 🔍 {label} {title}", "", f"<sub>기간 {fmt_date(start)} ~ {fmt_date(end)} · 자동 생성 {now_kst():%Y-%m-%d %H:%M} KST</sub>", "",
          "## 1. 기간 중 시장 변화", "", moves_md, ""]
    if ai and ai["period_themes"]:
        md += ["**🤖 이 기간의 핵심 테마**"] + [f"- {t}" for t in ai["period_themes"]] + [""]

    md += ["## 2. 일기 모음", ""]
    for j in journals:
        rel = "../../" + j["path"].relative_to(ROOT).as_posix()
        md.append(f"- [{fmt_date(j['date'])}]({rel}) — {j['summary']}")
    md += ["" if journals else "_이 기간 일기가 없습니다._", ""]

    md += ["## 3. 이 기간에 적은 나의 뷰", "", "| 날짜 | 국고 3Y | 국고 10Y | 커브 | 기간 | 확신도 | 근거 |", "|---|---|---|---|---|---|---|"]
    any_view = False
    for j in journals:
        v = j["views"]
        if any(norm_dir(v.get(k, "")) for k in TARGETS):
            any_view = True
            md.append(f"| {j['date'].strftime('%m/%d')} | {v.get('국고 3Y 방향', '')} | {v.get('국고 10Y 방향', '')} | "
                      f"{v.get('커브(10Y-3Y)', '')} | {v.get('기간', '')} | {v.get('확신도(1~5)', '')} | {v.get('근거', '')} |")
    if not any_view:
        md.append("| - | - | - | - | - | - | 기록된 뷰가 없습니다 |")
    md.append("")

    md += ["## 4. 뷰 채점 (기간이 끝난 뷰)", "",
           f"<sub>판정 기준: 강세=금리 {config().get('view_neutral_band_bp', 5)}bp 넘게 하락, 약세=상승, 중립=그 이내 · "
           "기준값은 일기 작성 전 영업일 종가</sub>", ""]
    if scored:
        md += ["| 뷰 날짜 | 대상 | 나의 뷰 | 실제 변화 | 결과 |", "|---|---|---|---:|:---:|"]
        for r in scored:
            icon = "✅" if r["result"] == "적중" else "❌"
            md.append(f"| {r['view_date'][5:]} | {r['target']} | {r['view']} ({r['horizon_days']}일) | "
                      f"{r['change_bp']}bp | {icon} {r['result']} |")
    else:
        md.append("_이번에 새로 채점할 뷰가 없습니다._")
    md += ["", f"📊 누적 적중률: {hit_rate(all_rows)}", ""]

    md += ["## 5. 🤖 AI 검증 세션", ""]
    if ai:
        md += [f"> {ai['overall']}", ""]
        for p in ai["per_view"]:
            md += [f"#### {p['view_date']} — {p['verdict']}", f"- ✅ 맞았던 부분: {p['right']}",
                   f"- ⚠️ 놓친 부분: {p['missed']}", f"- 💡 교훈: {p['lesson']}", ""]
        if ai["bias_patterns"]:
            md += ["**🪞 반복되는 판단 습관**"] + [f"- {b}" for b in ai["bias_patterns"]] + [""]
        md += ["**➡️ 다음에 볼 것**"] + [f"- {f}" for f in ai["next_focus"]] + [""]
    else:
        md += ["_ANTHROPIC_API_KEY가 없어 AI 검증을 건너뛰었습니다._", ""]

    md += ["## 6. ✍️ 나의 회고", "", "<!-- AI 검증을 읽고 동의/반박, 다음 기간 다짐을 적어보세요 -->",
           "- AI 피드백에 대한 생각: ", "- 잘한 점: ", "- 고칠 점: ", "- 다음 기간 다짐: ", ""]
    return "\n".join(md)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["weekly", "monthly"])
    ap.add_argument("--date", help="기준일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.date) if args.date else now_kst().date()

    if args.kind == "weekly":
        end, start = today, today - dt.timedelta(days=6)
        iso = end.isocalendar()
        label = f"{iso.year}-W{iso.week:02d}"
        out = REVIEWS / "weekly" / f"{label}.md"
    else:
        first_this = today.replace(day=1)
        end = first_this - dt.timedelta(days=1)
        start = end.replace(day=1)
        label = f"{start:%Y-%m}"
        out = REVIEWS / "monthly" / f"{label}.md"
    if out.exists() and not args.force:
        print(f"[review] 이미 있음: {out} (덮어쓰려면 --force)")
        return

    data = market.update(days=max(60, (today - start).days + 40))
    new_scored = score_views(data, today)
    all_rows = load_scorecard()
    if args.kind == "weekly":
        scored = new_scored
    else:   # 월간: 이번 달에 기간이 끝난 모든 뷰
        scored = [r for r in all_rows if start.isoformat() <= r["end_date"] <= end.isoformat()]
    journals = journals_between(start, end)
    _, moves_txt = period_moves(data, start, end)
    ai = ai_review("주간" if args.kind == "weekly" else "월간", journals, scored, moves_txt)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(args.kind, label, start, end, data, journals, scored, all_rows, ai), encoding="utf-8")
    print(f"[review] 작성 완료: {out}")
    notify(f"🔍 {label} {'주간' if args.kind == 'weekly' else '월간'} 리뷰 완료\n"
           f"새로 채점된 뷰 {len(new_scored)}개 · {hit_rate(all_rows).replace('**', '')}\n\n✍️ 회고 쓰기: {repo_url(out)}")


if __name__ == "__main__":
    main()
