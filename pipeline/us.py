"""미국 52주 신고가 — 시총 10억달러 이상 전 종목 당일 판정 + RS 등급.

종목 목록·시총: 나스닥 스크리너(전 상장 종목, 무료·무키). 시세 이력: 야후(yfinance) 1년치 일괄 내려받기.
결과: data/us/<날짜>.json, data/us/latest.json, data/us/universe.json, data/status_us.json
"""
import datetime as dt
import json
import math
import re
import sys
import time
import traceback

import pandas as pd
import requests

from common import log, isnum, usd_tag, rsr_rank, ohlc_str, verdict, ma_regime, write_json, write_status, now_kst

MIN_MCAP = 1e9
BATCH = 200
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[,$%\s]", "", str(s))
    if t in ("", "NA", "N/A", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def universe_nasdaq():
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=false&limit=0&offset=0&download=true"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    rows = r.json()["data"]["rows"]
    out = []
    for x in rows:
        sym = (x.get("symbol") or "").strip()
        mc = num(x.get("marketCap"))
        if not sym or mc is None or mc < MIN_MCAP:
            continue
        if any(ch in sym for ch in "^/ ") or len(sym) > 6:
            continue   # 워런트·우선주·유닛
        name = (x.get("name") or "").strip()
        if re.search(r"\b(Acquisition|SPAC)\b", name, re.I) and re.search(r"Corp|Co\b|Ltd|Inc", name):
            continue
        out.append(dict(code=sym, name=re.sub(r"\s+(Common Stock|Class [A-C] Common Stock|Ordinary Shares|American Depositary Shares|Common Shares).*$", "", name).strip(),
                        mcap=mc, sector=x.get("sector") or "", industry=x.get("industry") or "", country=x.get("country") or "",
                        chgS=num(x.get("pctchange")), lastS=num(x.get("lastsale"))))
    if len(out) < 500:
        raise RuntimeError(f"nasdaq screener: {len(out)}종목뿐")
    return out


def yf_hist(tickers, period="1y"):
    import yfinance as yf
    hist = {}
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i + BATCH]
        ysyms = [t.replace(".", "-") for t in chunk]
        for attempt in range(3):
            try:
                df = yf.download(ysyms, period=period, interval="1d", group_by="ticker", auto_adjust=False, threads=True, progress=False, timeout=60)
                break
            except Exception as e:
                log("yf batch fail", i, attempt, repr(e)[:100])
                time.sleep(10 * (attempt + 1))
                df = None
        if df is None or df.empty:
            continue
        for t, ys in zip(chunk, ysyms):
            try:
                sub = df[ys] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(subset=["Close"])
                rows = [(ix.strftime("%Y%m%d"), float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]), int(r["Volume"]) if isnum(r["Volume"]) else 0)
                        for ix, r in sub.iterrows() if isnum(r["Close"]) and r["Close"] > 0]
                if rows:
                    hist[t] = rows
            except Exception:
                pass
        log("yf", min(i + BATCH, len(tickers)), "/", len(tickers), "hist", len(hist))
        time.sleep(1.5)
    return hist


def main():
    # 기준일: 미국 마지막 거래일 = 실행 시각(한국 아침) 기준 전날(미국 날짜)
    want = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        uni = universe_nasdaq()
        log("universe", len(uni))
        codes = [x["code"] for x in uni]
        hist = yf_hist(codes)
        spy = yf_hist(["SPY", "^GSPC"])
        # 기준일 D = 이력 최빈 마지막 날짜
        lasts = {}
        for t, h in hist.items():
            lasts[h[-1][0]] = lasts.get(h[-1][0], 0) + 1
        D = max(lasts.items(), key=lambda kv: kv[1])[0]
        if want and D != want:
            raise RuntimeError(f"기준일 {D} ≠ 요청 {want}")
        date = f"{D[:4]}-{D[4:6]}-{D[6:]}"
        byc = {x["code"]: x for x in uni}
        ret60 = {}
        for t, h in hist.items():
            if h[-1][0] == D and len(h) >= 61 and h[-61][4] > 0:
                ret60[t] = (h[-1][4] / h[-61][4] - 1) * 100
        rsr = rsr_rank(ret60)
        spyc = [r[4] for r in spy.get("SPY", [])]
        spy60 = (spyc[-1] / spyc[-61] - 1) * 100 if len(spyc) >= 61 else None
        reg = ma_regime([r[4] for r in spy.get("^GSPC", [])])
        reg["name"] = "S&P 500"
        cands, flags = [], []
        for t, h in hist.items():
            if h[-1][0] != D or len(h) < 120:
                continue
            d0 = h[-1]
            prev_hi = max(r[2] for r in h[:-1][-251:])
            if d0[2] < prev_hi:
                continue
            x = byc[t]
            prevc = h[-2][4] if len(h) >= 2 else None
            chg = (d0[4] / prevc - 1) * 100 if prevc else x.get("chgS")
            cs, vd = verdict(d0[1], d0[2], d0[3], d0[4])
            c = dict(name=x["name"], code=t, price=round(d0[4], 2), chg=round(chg, 2) if chg is not None else None,
                     mcap=usd_tag(x["mcap"]), mcapUsd=int(x["mcap"]), val=usd_tag(d0[4] * d0[5]), valUsd=int(d0[4] * d0[5]),
                     sectorEn=x["sector"], industryEn=x["industry"], hi52=round(prev_hi, 2), basis="52W",
                     cs=cs, verdict=vd, w52=True, ohlc=ohlc_str(h, 2))
            if len(h) >= 240 and d0[2] >= max(r[2] for r in h[:-1]):
                c["basis"] = "52W"   # 1년 이력만 있어 사상 최고 판정은 하지 않음
            if t in ret60:
                c["ret60"] = round(ret60[t], 1)
                if spy60 is not None:
                    c["rs"] = int(round(ret60[t] - spy60))
            if t in rsr:
                c["rsr"] = rsr[t]
            if c["valUsd"] < 5e6:
                c["thin"] = True
            cands.append(c)
        cands.sort(key=lambda c: -c["mcapUsd"])
        flags.append(f"미국 시세: 나스닥 스크리너 종목 목록 + 야후 일별 시세 · 기준일 {date} · 시총 10억달러 이상 {len(uni)}종목 중 이력 확보 {len([1 for h in hist.values() if h[-1][0]==D])} · 52주 신고가 {len(cands)} · RS 기준 집단 {len(ret60)}")
        missing = len(uni) - len(hist)
        if missing > len(uni) * 0.1:
            flags.append(f"야후 시세 누락 {missing}종목 — 신고가 판정에서 빠졌을 수 있음")
        thin = [c["code"] for c in cands if c.get("thin")]
        if thin:
            flags.append("거래대금 500만달러 미만(유동성 주의): " + ", ".join(thin[:40]) + (" 외" if len(thin) > 40 else ""))
        uni_out = []
        for x in sorted(uni, key=lambda x: -x["mcap"]):
            u = dict(code=x["code"], name=x["name"], mcapUsd=int(x["mcap"]), sectorEn=x["sector"], industryEn=x["industry"])
            h = hist.get(x["code"])
            if h and h[-1][0] == D:
                u["price"] = round(h[-1][4], 2)
                if len(h) >= 2 and h[-2][4] > 0:
                    u["chg"] = round((h[-1][4] / h[-2][4] - 1) * 100, 2)
            if x["code"] in ret60:
                u["ret60"] = round(ret60[x["code"]], 1)
            if x["code"] in rsr:
                u["rsr"] = rsr[x["code"]]
            uni_out.append(u)
        out = {"market": "US", "date": date, "src": "nasdaq+yahoo", "generatedAt": now_kst().isoformat(timespec="seconds"),
               "regime": reg, "spy60": round(spy60, 2) if spy60 is not None else None,
               "universe": {"n": len(uni), "minMcap": MIN_MCAP, "rsN": len(ret60)},
               "highs": {"raw": len(uni), "pass": len(cands), "basDt": date, "src": "nasdaq+yahoo"},
               "candidates": cands, "flags": flags}
        write_json(f"data/us/{date}.json", out)
        write_json("data/us/latest.json", out)
        write_json("data/us/universe.json", {"date": date, "rows": uni_out})
        write_status("US", True, f"{date} 52주 신고가 {len(cands)} / 기준 집단 {len(ret60)}", {"date": date, "pass": len(cands)})
        return 0
    except Exception as e:
        traceback.print_exc()
        write_status("US", False, f"실패: {repr(e)[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
