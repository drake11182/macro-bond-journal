"""입력 소스 수집: 공개 텔레그램 채널, 한국경제 RSS, 경제지표 일정, 기사 본문.

원문 전체는 저장하지 않습니다(저작권). AI 요약에만 쓰이고, 기록에는 요약 + 링크만 남습니다.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re
import xml.etree.ElementTree as ET

import requests
import yaml
from bs4 import BeautifulSoup

from common import DATA, KST, UA, config, now_kst


# ───────────────────────── 텔레그램 공개 채널 ─────────────────────────

def telegram_posts(username: str, since: dt.datetime, max_pages: int = 6) -> list[dict]:
    """t.me/s/<채널> 웹 미리보기에서 since 이후 글을 가져온다 (API 키 불필요)."""
    posts: list[dict] = []
    before = None
    for _ in range(max_pages):
        url = f"https://t.me/s/{username}" + (f"?before={before}" if before else "")
        html = requests.get(url, headers=UA, timeout=30).text
        soup = BeautifulSoup(html, "html.parser")
        msgs = soup.select("div.tgme_widget_message[data-post]")
        if not msgs:
            break
        oldest_id = None
        for m in msgs:
            post_id = int(m["data-post"].split("/")[-1])
            oldest_id = post_id if oldest_id is None else min(oldest_id, post_id)
            t = m.select_one("time[datetime]")
            if not t:
                continue
            ts = dt.datetime.fromisoformat(t["datetime"]).astimezone(KST)
            if ts <= since:
                continue
            body = m.select_one(".tgme_widget_message_text")
            text = body.get_text("\n", strip=True) if body else ""
            doc = m.select_one(".tgme_widget_message_document_title")
            if doc:
                text = f"[첨부: {doc.get_text(strip=True)}]\n{text}"
            if not text.strip():
                continue
            posts.append({
                "ts": ts.isoformat(),
                "link": f"https://t.me/{username}/{post_id}",
                "text": text[:4000],
            })
        # 이 페이지의 가장 오래된 글도 since 이후면 더 이전 페이지를 본다
        oldest_ts = min((dt.datetime.fromisoformat(t["datetime"]) for t in soup.select(
            "div.tgme_widget_message time[datetime]")), default=None)
        if oldest_ts is None or oldest_ts.astimezone(KST) <= since or oldest_id is None:
            break
        before = oldest_id
    posts.sort(key=lambda p: p["ts"])
    return posts


def all_channel_posts(since: dt.datetime) -> list[dict]:
    out = []
    for ch in config().get("telegram_channels", []):
        if not ch.get("username"):
            continue
        try:
            for p in telegram_posts(ch["username"], since):
                p["source"] = ch["name"]
                out.append(p)
        except Exception as e:
            print(f"[telegram] {ch['name']} 수집 실패: {e}")
    print(f"[telegram] 채널 글 {len(out)}건")
    return out


# ───────────────────────── 한국경제 RSS ─────────────────────────

def news_items(since: dt.datetime, limit: int = 120) -> list[dict]:
    out, seen = [], set()
    for feed in config().get("news_rss", []):
        try:
            root = ET.fromstring(requests.get(feed["url"], headers=UA, timeout=30).content)
        except Exception as e:
            print(f"[news] {feed['name']} 실패: {e}")
            continue
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            if not link or link in seen:
                continue
            pub = it.findtext("pubDate")
            try:
                ts = email.utils.parsedate_to_datetime(pub).astimezone(KST)
            except Exception:
                continue
            if ts <= since:
                continue
            seen.add(link)
            desc = BeautifulSoup(it.findtext("description") or "", "html.parser").get_text(" ", strip=True)
            out.append({
                "ts": ts.isoformat(), "source": feed["name"], "link": link,
                "title": (it.findtext("title") or "").strip(), "desc": desc[:200],
            })
    out.sort(key=lambda x: x["ts"], reverse=True)
    print(f"[news] 기사 {len(out)}건")
    return out[:limit]


# ───────────────────────── 기사 본문 (봇으로 받은 링크용) ─────────────────────────

def fetch_article(url: str) -> tuple[str, str]:
    """(제목, 본문) — 실패하면 ("", "")."""
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.encoding = r.apparent_encoding or r.encoding
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return "", ""
    title = ""
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title:
        title = soup.title.get_text(strip=True)
    for sel in ["#articletxt", "div.article-body", "article", "#articleBody", "div#newsct_article"]:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return title, node.get_text("\n", strip=True)[:12000]
    paras = [p.get_text(" ", strip=True) for p in soup.select("p")]
    return title, "\n".join(p for p in paras if len(p) > 40)[:12000]


# ───────────────────────── 경제지표 일정 ─────────────────────────

IMPACT_RANK = {"Low": 0, "Medium": 1, "High": 2, "Holiday": -1}


def global_events() -> list[dict]:
    """해외 주요 지표 (이번 주). 시간은 KST로 변환."""
    cal = config().get("calendar", {})
    countries = set(cal.get("countries", ["USD"]))
    min_rank = IMPACT_RANK.get(cal.get("min_impact", "Medium"), 1)
    try:
        rows = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                            headers=UA, timeout=30).json()
    except Exception as e:
        print(f"[calendar] 해외 일정 실패: {e}")
        return []
    out = []
    for r in rows:
        if r.get("country") not in countries or IMPACT_RANK.get(r.get("impact"), 0) < min_rank:
            continue
        ts = dt.datetime.fromisoformat(r["date"]).astimezone(KST)
        out.append({
            "ts": ts, "country": r["country"], "title": r["title"],
            "impact": r.get("impact", ""), "forecast": r.get("forecast", ""),
            "previous": r.get("previous", ""),
        })
    return out


def korea_events() -> list[dict]:
    f = DATA / "events_kr.yaml"
    if not f.exists():
        return []
    out = []
    for e in yaml.safe_load(f.read_text(encoding="utf-8")) or []:
        d = e["date"] if isinstance(e["date"], dt.date) else dt.date.fromisoformat(str(e["date"]))
        hh, mm = (str(e.get("time", "00:00")).split(":") + ["0"])[:2]
        out.append({
            "ts": dt.datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=KST),
            "country": "KRW", "title": e["title"], "impact": e.get("impact", "High"),
            "forecast": "", "previous": "", "has_time": "time" in e,
        })
    return out


FLAG = {"USD": "🇺🇸", "KRW": "🇰🇷", "CNY": "🇨🇳", "EUR": "🇪🇺", "JPY": "🇯🇵", "GBP": "🇬🇧"}


def events_table(start: dt.datetime, days: int) -> tuple[str, str]:
    """(마크다운 표, AI에게 줄 텍스트)"""
    end = start + dt.timedelta(days=days)
    evs = [e for e in global_events() + korea_events() if start.date() <= e["ts"].date() <= end.date()]
    evs.sort(key=lambda e: e["ts"])
    if not evs:
        return "_이 기간에 등록된 주요 일정이 없습니다._", "(없음)"
    lines = ["| 일시(KST) | 국가 | 일정 | 중요도 | 예상 | 이전 |", "|---|:---:|---|:---:|---:|---:|"]
    txt = []
    for e in evs:
        when = e["ts"].strftime("%m/%d(") + "월화수목금토일"[e["ts"].weekday()] + ")"
        if e.get("has_time", True):
            when += e["ts"].strftime(" %H:%M")
        star = "★★★" if e["impact"] == "High" else "★★" if e["impact"] == "Medium" else "★"
        lines.append(f"| {when} | {FLAG.get(e['country'], e['country'])} | {e['title']} | {star} | "
                     f"{e['forecast'] or '-'} | {e['previous'] or '-'} |")
        txt.append(f"{when} {e['country']} {e['title']} (예상 {e['forecast'] or '-'}, 이전 {e['previous'] or '-'})")
    return "\n".join(lines), "\n".join(txt)


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s)>\]]+", text or "")


if __name__ == "__main__":
    since = now_kst() - dt.timedelta(hours=24)
    for p in all_channel_posts(since)[:3]:
        print(p["ts"], p["source"], p["link"], p["text"][:80].replace("\n", " "))
    for n in news_items(since)[:3]:
        print(n["ts"], n["title"])
    print(events_table(now_kst(), 7)[0])
