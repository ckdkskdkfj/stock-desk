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

from common import log, isnum, usd_tag, rsr_rank, ohlc_str, verdict, ma_regime, write_json, write_status, now_kst, tech, breadth, append_series, sector_rs

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
        if re.search(r"(Preferred|Depositary|Depository|Debenture|\bNotes?\b|\bTrust Preferred|\d+(\.\d+)?%|Series [A-Z]\b|Warrant|\bUnits?\b|\bRights?\b)", name, re.I):
            continue   # 우선주·예탁증서·채권형·워런트·유닛
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


# ── 부가 산출물: 관심 종목 신호 · 시장 폭 · 업종 RS · 실적 발표일 ──
def earnings_nasdaq(days=21):
    """나스닥 실적 캘린더 — 앞으로 days일. {SYM: 'YYYY-MM-DD'}"""
    by = {}
    today = dt.date.today()
    for i in range(days):
        d = today + dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        try:
            r = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}", headers=UA, timeout=30)
            rows = ((r.json().get("data") or {}).get("rows")) or []
            for x in rows:
                sym = (x.get("symbol") or "").strip()
                if sym and sym not in by:
                    by[sym] = d.isoformat()
        except Exception as e:
            log("earnings cal fail", d, repr(e)[:60])
        time.sleep(0.4)
    return by


def extras(D, uni, hist, byc):
    date = f"{D[:4]}-{D[4:6]}-{D[6:]}"
    techs = {}
    for t_, h in hist.items():
        t = tech(h, D, drop_pct=-7.0)
        if t:
            techs[t_] = t
    rows = []
    for u in uni:
        t = techs.get(u["code"])
        if not t:
            continue
        r = {"code": u["code"], "name": u["name"], "mcapUsd": u["mcapUsd"], "industryEn": u.get("industryEn", "")}
        if u.get("rsr") is not None:
            r["rsr"] = u["rsr"]
        r.update(t)
        for k in ("ma20", "ma60", "ma200", "hi60", "hi52"):   # 파일 크기 절약 — % 거리만 남김
            r.pop(k, None)
        rows.append(r)
    write_json("data/us/signals.json", {"market": "US", "date": date, "n": len(rows), "rows": rows})
    b = breadth(techs)
    if b:
        append_series("data/us/breadth.json", date, b)
    ind = {c: (x.get("industry") or "").strip() for c, x in byc.items() if x.get("industry")}
    sr = sector_rs(uni, techs, ind, min_n=4)
    append_series("data/us/sector_rs.json", date, {"sectors": sr}, keep=70)
    log("sector_rs", len(sr), "업종")
    try:
        er = earnings_nasdaq()
        write_json("data/us/earnings.json", {"asOf": date, "n": len(er), "by": er})
    except Exception as e:
        log("earnings FAIL", repr(e)[:100])
    return len(rows)


def _pct(a, b):
    try:
        a, b = float(a), float(b)
        if b == 0 or math.isnan(a) or math.isnan(b):
            return None
        return round((a / b - 1) * 100 * (1 if b > 0 else -1), 1)
    except Exception:
        return None


def _f(v):
    try:
        v = float(v)
        return None if math.isnan(v) else v
    except Exception:
        return None


def _col(df, *names):
    """yfinance 버전마다 열 이름 대소문자가 달라 이름을 느슨하게 찾는다."""
    low = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


ACT = {"up": "up", "down": "down", "main": "main", "init": "init", "reit": "reit"}


def analyst_one(t, since):
    """한 종목의 EPS 추정치 변화 · 상향/하향 수 · 최근 의견·목표가 변경(야후)."""
    import yfinance as yf
    tk = yf.Ticker(t)
    an = {}
    # 1) EPS 추정치 추이 — 올해(0y)·내년(+1y)
    try:
        et = tk.eps_trend
        if et is not None and len(et):
            cur, d7, d30, d90 = (_col(et, "current"), _col(et, "7daysAgo"), _col(et, "30daysAgo"), _col(et, "90daysAgo"))
            eps = []
            for p, lab in (("0y", "올해"), ("+1y", "내년")):
                if p in et.index and cur is not None:
                    r = et.loc[p]
                    now = _f(r[cur])
                    if now is None:
                        continue
                    e = {"p": lab, "now": round(now, 2)}
                    for k, c in (("d7", d7), ("d30", d30), ("d90", d90)):
                        if c is not None:
                            v = _pct(now, r[c])
                            if v is not None:
                                e[k] = v
                    eps.append(e)
            if eps:
                an["eps"] = eps
    except Exception as e:
        an["_e1"] = repr(e)[:60]
    # 2) 추정치 상향·하향 애널리스트 수 — 올해 기준
    try:
        er = tk.eps_revisions
        if er is not None and len(er):
            p = "0y" if "0y" in er.index else er.index[0]
            r = er.loc[p]
            for k, names in (("up7", ("upLast7days",)), ("up30", ("upLast30days",)),
                             ("dn7", ("downLast7Days", "downLast7days")), ("dn30", ("downLast30days", "downLast30Days"))):
                c = _col(er, *names)
                if c is not None and _f(r[c]) is not None:
                    an[k] = int(_f(r[c]))
    except Exception as e:
        an["_e2"] = repr(e)[:60]
    # 3) 최근 30일 의견·목표가 변경 — 최신 5건
    try:
        ud = tk.upgrades_downgrades
        if ud is not None and len(ud):
            ud = ud.copy()
            idx = pd.to_datetime(ud.index, errors="coerce")
            try:
                idx = idx.tz_localize(None)
            except Exception:
                pass
            ud.index = idx
            ud = ud[ud.index >= pd.Timestamp(since)].sort_index(ascending=False)
            cf, cg1, cg0, ca = _col(ud, "Firm"), _col(ud, "ToGrade"), _col(ud, "FromGrade"), _col(ud, "Action")
            cp1, cp0 = _col(ud, "currentPriceTarget"), _col(ud, "priorPriceTarget")
            acts = []
            for d, r in ud.head(5).iterrows():
                a = {"d": d.strftime("%Y-%m-%d")}
                if cf is not None: a["f"] = str(r[cf])
                if ca is not None: a["a"] = ACT.get(str(r[ca]).lower(), str(r[ca]))
                if cg0 is not None and str(r[cg0]).strip() not in ("", "nan"): a["g0"] = str(r[cg0])
                if cg1 is not None and str(r[cg1]).strip() not in ("", "nan"): a["g1"] = str(r[cg1])
                if cp0 is not None and _f(r[cp0]): a["pt0"] = round(_f(r[cp0]), 2)
                if cp1 is not None and _f(r[cp1]): a["pt1"] = round(_f(r[cp1]), 2)
                acts.append(a)
            if acts:
                an["acts"] = acts
            an["n30"] = int(len(ud))
    except Exception as e:
        an["_e3"] = repr(e)[:60]
    # 4) 목표가 컨센서스
    try:
        pt = tk.analyst_price_targets
        if isinstance(pt, dict) and _f(pt.get("mean")):
            an["tgt"] = round(_f(pt["mean"]), 2)
    except Exception:
        pass
    return an


def analyst_all(cands, date, cap=90):
    """신고가 종목(RS 등급 높은 순 cap개)의 애널리스트 데이터. 실패해도 본 산출물에는 영향 없음."""
    since = (pd.Timestamp(date) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    pick = sorted(cands, key=lambda c: -(c.get("rsr") or 0))[:cap]
    by, ok, errs = {}, 0, 0
    for i, c in enumerate(pick):
        try:
            an = analyst_one(c["code"], since)
            bad = [k for k in an if k.startswith("_e")]
            if i < 2 or (bad and errs < 3):
                log("analyst", c["code"], {k: an[k] for k in an if k.startswith("_e")} or sorted(an.keys()))
            if bad:
                errs += 1
            for k in bad:
                an.pop(k)
            if an:
                by[c["code"]] = an; ok += 1
        except Exception as e:
            errs += 1
            if errs <= 3:
                log("analyst fail", c["code"], repr(e)[:80])
        time.sleep(0.4)
    log("analyst", ok, "/", len(pick), "종목 · 오류", errs)
    return by


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
        try:
            anby = analyst_all(cands, date)
            for c in cands:
                if c["code"] in anby:
                    c["an"] = anby[c["code"]]
            write_json("data/us/analyst.json", {"date": date, "n": len(anby), "by": anby})
        except Exception as e:
            log("analyst FAIL", repr(e)[:200])
        out = {"market": "US", "date": date, "src": "nasdaq+yahoo", "generatedAt": now_kst().isoformat(timespec="seconds"),
               "regime": reg, "spy60": round(spy60, 2) if spy60 is not None else None,
               "universe": {"n": len(uni), "minMcap": MIN_MCAP, "rsN": len(ret60)},
               "highs": {"raw": len(uni), "pass": len(cands), "basDt": date, "src": "nasdaq+yahoo"},
               "candidates": cands, "flags": flags}
        write_json(f"data/us/{date}.json", out)
        write_json("data/us/latest.json", out)
        write_json("data/us/universe.json", {"date": date, "rows": uni_out})
        try:
            log("signals", extras(D, uni_out, hist, byc))
        except Exception as e:
            log("extras FAIL", repr(e)[:200]); traceback.print_exc()
        write_status("US", True, f"{date} 52주 신고가 {len(cands)} / 기준 집단 {len(ret60)}", {"date": date, "pass": len(cands)})
        return 0
    except Exception as e:
        traceback.print_exc()
        write_status("US", False, f"실패: {repr(e)[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
