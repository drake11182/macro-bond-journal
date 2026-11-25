"""시장 지표 수집 → data/market.csv (date,key,value) 에 누적 저장.

출처
- 한국은행 ECOS: 국고채 만기별, 통안, CD, 회사채(AA-/BBB-), 기준금리, 원/달러
- 미 재무부: 미국채 2Y/10Y/30Y
- Yahoo Finance (실패 시 FRED): WTI, 브렌트
- FRED: 미국 IG 회사채 스프레드(OAS)

python scripts/market.py              # 최근 20일 갱신
python scripts/market.py --days 400   # 과거 데이터 백필 (리뷰용)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os

import requests

from common import DATA, UA, now_kst

MARKET_CSV = DATA / "market.csv"

# key: (라벨, 출처, 코드, 단위)   단위: pct(금리, bp로 변화 표시) / krw / usd / pctp(% 변화)
SERIES = {
    "base":   ("기준금리",      "ecos", ("722Y001", "0101000"), "pct"),
    "cd91":   ("CD 91일",       "ecos", ("817Y002", "010502000"), "pct"),
    "msb1y":  ("통안 1Y",       "ecos", ("817Y002", "010400001"), "pct"),
    "ktb1y":  ("국고 1Y",       "ecos", ("817Y002", "010190000"), "pct"),
    "ktb2y":  ("국고 2Y",       "ecos", ("817Y002", "010195000"), "pct"),
    "ktb3y":  ("국고 3Y",       "ecos", ("817Y002", "010200000"), "pct"),
    "ktb5y":  ("국고 5Y",       "ecos", ("817Y002", "010200001"), "pct"),
    "ktb10y": ("국고 10Y",      "ecos", ("817Y002", "010210000"), "pct"),
    "ktb20y": ("국고 20Y",      "ecos", ("817Y002", "010220000"), "pct"),
    "ktb30y": ("국고 30Y",      "ecos", ("817Y002", "010230000"), "pct"),
    "ktb50y": ("국고 50Y",      "ecos", ("817Y002", "010240000"), "pct"),
    "aa3y":   ("회사채 AA- 3Y", "ecos", ("817Y002", "010300000"), "pct"),
    "bbb3y":  ("회사채 BBB- 3Y", "ecos", ("817Y002", "010320000"), "pct"),
    "usdkrw": ("원/달러",       "ecos", ("731Y001", "0000001"), "krw"),
    "ust2y":  ("미국채 2Y",     "ust", "2 Yr", "pct"),
    "ust10y": ("미국채 10Y",    "ust", "10 Yr", "pct"),
    "ust30y": ("미국채 30Y",    "ust", "30 Yr", "pct"),
    "us_ig_oas": ("미 IG 회사채 OAS", "fred", "BAMLC0A0CM", "pct"),
    "wti":    ("WTI",           "oil", ("CL=F", "DCOILWTICO"), "usd"),
    "brent":  ("브렌트",        "oil", ("BZ=F", "DCOILBRENTEU"), "usd"),
}

# 스프레드: key: (라벨, A, B)  → A - B (bp)
SPREADS = {
    "ktb_10_3":  ("국고 10Y-3Y", "ktb10y", "ktb3y"),
    "ktb_30_10": ("국고 30Y-10Y", "ktb30y", "ktb10y"),
    "ktb3_base": ("국고 3Y-기준금리", "ktb3y", "base"),
    "aa_ktb3":   ("크레딧 AA- 3Y", "aa3y", "ktb3y"),
    "bbb_ktb3":  ("크레딧 BBB- 3Y", "bbb3y", "ktb3y"),
    "kr_us_10":  ("한미 10Y (국고-미국채)", "ktb10y", "ust10y"),
    "ust_10_2":  ("미국채 10Y-2Y", "ust10y", "ust2y"),
}

KR_TABLE = ["base", "cd91", "msb1y", "ktb1y", "ktb2y", "ktb3y", "ktb5y", "ktb10y",
            "ktb20y", "ktb30y", "ktb50y"]
CREDIT_TABLE = ["aa3y", "bbb3y", "us_ig_oas"]
GLOBAL_TABLE = ["ust2y", "ust10y", "ust30y", "usdkrw", "wti", "brent"]


# ───────────────────────── fetchers ─────────────────────────

def _ecos(stat: str, item: str, start: dt.date, end: dt.date) -> dict[str, float]:
    key = os.environ.get("ECOS_API_KEY", "sample")
    n = 10 if key == "sample" else 10000          # sample 키는 10건 제한 (테스트용)
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/{n}/{stat}/D/"
           f"{start:%Y%m%d}/{end:%Y%m%d}/{item}")
    j = requests.get(url, timeout=30).json()
    rows = j.get("StatisticSearch", {}).get("row", [])
    out = {}
    for r in rows:
        try:
            out[dt.datetime.strptime(r["TIME"], "%Y%m%d").date().isoformat()] = float(r["DATA_VALUE"])
        except (ValueError, KeyError):
            pass
    return out


def _ust(start: dt.date, end: dt.date) -> dict[str, dict[str, float]]:
    """재무부 Daily Treasury Par Yield Curve CSV (연도별)."""
    out: dict[str, dict[str, float]] = {}
    for year in range(start.year, end.year + 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={year}&page&_format=csv")
        txt = requests.get(url, headers=UA, timeout=60).text
        for row in csv.DictReader(io.StringIO(txt)):
            d = dt.datetime.strptime(row["Date"], "%m/%d/%Y").date()
            if not (start <= d <= end):
                continue
            for col, v in row.items():
                if col != "Date" and v:
                    out.setdefault(col, {})[d.isoformat()] = float(v)
    return out


def _fred(sid: str, start: dt.date) -> dict[str, float]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start.isoformat()}"
    out = {}
    for row in csv.DictReader(io.StringIO(requests.get(url, headers=UA, timeout=60).text)):
        d = row.get("observation_date") or row.get("DATE")
        v = row.get(sid)
        if d and v not in (None, "", "."):
            out[d] = float(v)
    return out


def _yahoo(ticker: str, days: int) -> dict[str, float]:
    import yfinance as yf

    df = yf.download(ticker, period=f"{max(days, 5)}d", progress=False, auto_adjust=False)
    if df.empty:
        return {}
    close = df["Close"]
    if hasattr(close, "columns"):          # 신버전 yfinance는 멀티컬럼
        close = close.iloc[:, 0]
    return {idx.date().isoformat(): float(v) for idx, v in close.dropna().items()}


def fetch(days: int = 20) -> dict[str, dict[str, float]]:
    end = now_kst().date()
    start = end - dt.timedelta(days=days)
    result: dict[str, dict[str, float]] = {}
    ust = None
    for key, (label, src, code, _unit) in SERIES.items():
        try:
            if src == "ecos":
                result[key] = _ecos(code[0], code[1], start, end)
            elif src == "ust":
                if ust is None:
                    ust = _ust(start, end)
                result[key] = ust.get(code, {})
            elif src == "fred":
                result[key] = _fred(code, start)
            elif src == "oil":
                try:
                    result[key] = _yahoo(code[0], days)
                except Exception:
                    result[key] = {}
                if not result[key]:
                    result[key] = _fred(code[1], start)
        except Exception as e:
            print(f"[market] {label} 수집 실패: {e}")
            result[key] = {}
        print(f"[market] {label}: {len(result[key])}건")
    return result


# ───────────────────────── storage ─────────────────────────

def load() -> dict[str, dict[str, float]]:
    data: dict[str, dict[str, float]] = {}
    if MARKET_CSV.exists():
        with MARKET_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                data.setdefault(r["key"], {})[r["date"]] = float(r["value"])
    return data


def save(data: dict[str, dict[str, float]]) -> None:
    DATA.mkdir(exist_ok=True)
    rows = sorted((d, k, v) for k, s in data.items() for d, v in s.items())
    with MARKET_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "key", "value"])
        for d, k, v in rows:
            w.writerow([d, k, f"{v:.4f}".rstrip("0").rstrip(".")])


def update(days: int = 20) -> dict[str, dict[str, float]]:
    data = load()
    for k, s in fetch(days).items():
        data.setdefault(k, {}).update(s)
    save(data)
    return data


# ───────────────────────── analytics ─────────────────────────

def spread_series(data, a: str, b: str) -> dict[str, float]:
    sa, sb = data.get(a, {}), data.get(b, {})
    if b == "base":   # 기준금리는 변경일만 있을 수 있으므로 직전 값으로 채움
        out, keys = {}, sorted(sb)
        for d, v in sa.items():
            prev = [x for x in keys if x <= d]
            if prev:
                out[d] = v - sb[prev[-1]]
        return out
    return {d: sa[d] - sb[d] for d in sa.keys() & sb.keys()}


def value_on_or_before(series: dict[str, float], day: str):
    keys = [d for d in series if d <= day]
    if not keys:
        return None, None
    d = max(keys)
    return d, series[d]


def latest_change(series: dict[str, float]):
    """(기준일, 최신값, 직전값) — 데이터 부족 시 None."""
    keys = sorted(series)
    if not keys:
        return None
    last = keys[-1]
    prev = series[keys[-2]] if len(keys) >= 2 else None
    return last, series[last], prev


def fmt_change(unit: str, cur: float, prev: float | None) -> str:
    if prev is None:
        return "-"
    if unit == "pct":
        bp = (cur - prev) * 100
        return f"{bp:+.1f}bp"
    if unit == "krw":
        return f"{cur - prev:+.1f}원"
    return f"{(cur / prev - 1) * 100:+.2f}%"


def fmt_value(unit: str, v: float) -> str:
    if unit == "pct":
        return f"{v:.3f}%"
    if unit == "krw":
        return f"{v:,.1f}"
    return f"${v:,.2f}"


def table(data, keys: list[str]) -> str:
    lines = ["| 구분 | 수준 | 전일비 | 기준일 |", "|---|---:|---:|:---:|"]
    for k in keys:
        label, _, _, unit = SERIES[k]
        lc = latest_change(data.get(k, {}))
        if not lc:
            lines.append(f"| {label} | 수집 실패 | - | - |")
            continue
        d, cur, prev = lc
        lines.append(f"| {label} | {fmt_value(unit, cur)} | {fmt_change(unit, cur, prev)} | {d[5:]} |")
    return "\n".join(lines)


def spread_table(data) -> str:
    lines = ["| 스프레드 | 수준 | 전일비 | 기준일 |", "|---|---:|---:|:---:|"]
    for _, (label, a, b) in SPREADS.items():
        lc = latest_change(spread_series(data, a, b))
        if not lc:
            lines.append(f"| {label} | - | - | - |")
            continue
        d, cur, prev = lc
        chg = f"{(cur - prev) * 100:+.1f}bp" if prev is not None else "-"
        lines.append(f"| {label} | {cur * 100:.1f}bp | {chg} | {d[5:]} |")
    return "\n".join(lines)


def snapshot_text(data) -> str:
    """AI에게 넘겨줄 숫자 요약 (AI가 숫자를 지어내지 않도록 실제 값만 전달)."""
    out = []
    for k, (label, _, _, unit) in SERIES.items():
        lc = latest_change(data.get(k, {}))
        if lc:
            d, cur, prev = lc
            out.append(f"{label}: {fmt_value(unit, cur)} ({fmt_change(unit, cur, prev)}, {d})")
    for _, (label, a, b) in SPREADS.items():
        lc = latest_change(spread_series(data, a, b))
        if lc:
            d, cur, prev = lc
            chg = f"{(cur - prev) * 100:+.1f}bp" if prev is not None else "-"
            out.append(f"{label}: {cur * 100:.1f}bp ({chg}, {d})")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    args = ap.parse_args()
    d = update(args.days)
    print(table(d, KR_TABLE + CREDIT_TABLE + GLOBAL_TABLE))
    print(spread_table(d))
