"""국내 60일 신고가 — 전 종목 당일 판정 + RS 등급.

경로 A(1순위): pykrx — 한국거래소 일별 스냅샷(전 종목 시가·고가·저가·종가·거래량·거래대금·등락률·시가총액).
경로 B(예비): FinanceDataReader — 전 종목 당일 시세(한국거래소) + 종목별 이력(네이버).
경로 C(예비): 네이버 모바일 시세 API — 시총 순 목록 + 종목별 이력. 한국거래소가 막혀도 동작.

결과: data/kr/<날짜>.json, data/kr/latest.json, data/kr/universe.json, data/status_kr.json
"""
import datetime as dt
import re
import sys
import time
import traceback

import pandas as pd
import requests

from common import (KST, log, isnum, won_str, rsr_rank, ohlc_str, verdict, ma_regime,
                    write_json, write_status, now_kst, tech, breadth, append_series, sector_rs)

MIN_MCAP = 1e12          # 시총 1조
MIN_VAL = 5e9            # 거래대금 50억
MIN_PRICE = 2000
MIN_CHG, MAX_CHG = 3.0, 29.5
HIST_DAYS = 75           # 스냅샷 거래일 수(60일 판정 + 여유)
EXCL_NAME = ("스팩", "KODEX", "TIGER", "ETN", "ETF", "KBSTAR", "ARIRANG", "HANARO", "SOL ", "ACE ", "PLUS ", "RISE ", "KOSEF",
             "기업인수목적", "인버스", "레버리지", "채권", "선물", "리츠")
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "ko-KR,ko;q=0.9", "Referer": "https://m.stock.naver.com/"}


def today_str():
    return now_kst().strftime("%Y%m%d")


def is_common_stock(code, name):
    if not code or len(code) != 6 or not code.isdigit() or not code.endswith("0"):
        return False   # 우선주·ETF 등은 코드 끝자리가 0이 아님
    if any(x in (name or "") for x in EXCL_NAME):
        return False
    if (name or "").endswith(("우", "우B", "우C", "우(전환)")):
        return False
    return True


def norm_market(mk):
    mk = str(mk or "").upper()
    if "KOSPI" in mk:
        return "KOSPI"
    if "KOSDAQ" in mk:
        return "KOSDAQ"
    return None


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[,\s%+]", "", str(s))
    if t in ("", "-", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ── 네이버 이력 (경로 B·C 공용) ──────────────────────────────────
def naver_hist(symbol, start, end):
    """siseJson: [(YYYYMMDD, o, h, l, c, v)] 오래된 순"""
    url = f"https://api.finance.naver.com/siseJson.naver?symbol={symbol}&requestType=1&startTime={start}&endTime={end}&timeframe=day"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    rows = []
    for m in re.finditer(r'\["?(\d{8})"?\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*(\d+)', r.text):
        d, o, h, l, c, v = m.groups()
        if float(c) > 0:
            rows.append((d, float(o), float(h), float(l), float(c), int(v)))
    return rows


def naver_index_closes(D):
    start = (dt.datetime.strptime(D, "%Y%m%d") - dt.timedelta(days=400)).strftime("%Y%m%d")
    try:
        rows = naver_hist("KOSPI", start, D)
        if len(rows) > 100:
            return [r[4] for r in rows]
    except Exception as e:
        log("naver index siseJson fail", repr(e)[:80])
    # fchart XML 예비
    r = requests.get(f"https://fchart.stock.naver.com/sise.nhn?symbol=KOSPI&timeframe=day&count=300&requestType=0", headers=UA, timeout=30)
    closes = [float(m.group(1)) for m in re.finditer(r'data="\d{8}\|[\d.]+\|[\d.]+\|[\d.]+\|([\d.]+)\|', r.text)]
    return closes


# ── 경로 A: pykrx ────────────────────────────────────────────────
def load_pykrx(D):
    from pykrx import stock
    days = []          # [(YYYYMMDD, DataFrame)] 오래된 순
    cur = dt.datetime.strptime(D, "%Y%m%d")
    tries = 0
    while len(days) < HIST_DAYS and tries < HIST_DAYS * 2:
        tries += 1
        if cur.weekday() < 5:
            d = cur.strftime("%Y%m%d")
            try:
                df = stock.get_market_ohlcv_by_ticker(d, market="ALL")
            except Exception as e:
                log("pykrx ohlcv fail", d, repr(e)[:120])
                df = None
            if df is not None and len(df) > 100 and (df["종가"] > 0).sum() > 100:
                days.append((d, df))
            elif len(days) == 0:
                raise RuntimeError(f"pykrx: {d} 스냅샷 없음(당일 미게시 또는 접속 차단)")
            time.sleep(0.4)
        cur -= dt.timedelta(days=1)
    days.sort(key=lambda x: x[0])
    if not days or days[-1][0] != D:
        raise RuntimeError(f"pykrx: {D} 스냅샷 없음 (마지막 {days[-1][0] if days else None})")
    cap = stock.get_market_cap_by_ticker(D, market="ALL")
    kospi = set(stock.get_market_ticker_list(D, market="KOSPI"))
    kosdaq = set(stock.get_market_ticker_list(D, market="KOSDAQ"))
    # 종목명: 시총 0.9조 이상만(판정에 쓰는 범위) — 호출 수 절약
    need = [t for t in cap.index if (t in kospi or t in kosdaq) and float(cap.loc[t, "시가총액"]) >= MIN_MCAP * 0.9]
    names = {}
    try:
        s = stock.get_market_ticker_and_name(D, market="ALL")
        names = {str(k): str(v) for k, v in s.items()}
    except Exception as e:
        log("ticker_and_name 없음 → 개별 조회", len(need), repr(e)[:60])
        for t in need:
            try:
                names[t] = stock.get_market_ticker_name(t)
            except Exception:
                names[t] = t
    end = dt.datetime.strptime(D, "%Y%m%d")
    try:
        idx = stock.get_index_ohlcv_by_date((end - dt.timedelta(days=400)).strftime("%Y%m%d"), D, "1001")
        closes = [float(x) for x in idx["종가"].tolist() if x and x > 0]
    except Exception as e:
        log("pykrx index fail → naver", repr(e)[:80])
        closes = naver_index_closes(D)
    return days, cap, names, kospi, kosdaq, closes


def build_from_pykrx(D):
    days, cap, names, kospi, kosdaq, closes = load_pykrx(D)
    dfD = days[-1][1]
    hist = {}   # code -> [(d,o,h,l,c,v)]
    for d, df in days:
        sub = df[["시가", "고가", "저가", "종가", "거래량"]]
        for code, r in sub.iterrows():
            if r["종가"] > 0:
                hist.setdefault(code, []).append((d, float(r["시가"]), float(r["고가"]), float(r["저가"]), float(r["종가"]), int(r["거래량"])))
    rows = []
    for code in dfD.index:
        if code not in cap.index:
            continue
        mk = "KOSPI" if code in kospi else "KOSDAQ" if code in kosdaq else None
        mcap = float(cap.loc[code, "시가총액"])
        if not mk or mcap < MIN_MCAP * 0.9:
            continue
        name = names.get(code, code)
        if not is_common_stock(code, name):
            continue
        r = dfD.loc[code]
        rows.append(dict(code=code, name=name, mk=mk, o=float(r["시가"]), h=float(r["고가"]), l=float(r["저가"]),
                         c=float(r["종가"]), v=int(r["거래량"]), val=float(r["거래대금"]), chg=float(r["등락률"]), mcap=mcap))
    return rows, hist, closes, "krx"


# ── 경로 B: FinanceDataReader ────────────────────────────────────
def build_from_fdr(D):
    import FinanceDataReader as fdr
    lst = fdr.StockListing("KRX")
    cols = {c.lower(): c for c in lst.columns}
    def col(*names):
        for n in names:
            if n.lower() in cols:
                return cols[n.lower()]
        raise KeyError(names)
    cCode, cName, cMk = col("Code", "Symbol"), col("Name"), col("Market")
    cClose, cChg, cMcap = col("Close"), col("ChagesRatio", "ChangesRatio", "ChangeRatio"), col("Marcap", "MarketCap")
    cAmt = col("Amount", "TradingValue")
    cOpen, cHigh, cLow, cVol = col("Open"), col("High"), col("Low"), col("Volume")
    rows = []
    for _, r in lst.iterrows():
        code, name, mk = str(r[cCode]).zfill(6), str(r[cName]), norm_market(r[cMk])
        if not mk or not is_common_stock(code, name):
            continue
        try:
            mcap = float(r[cMcap])
            if mcap < MIN_MCAP * 0.9:
                continue
            rows.append(dict(code=code, name=name, mk=mk, o=float(r[cOpen]), h=float(r[cHigh]), l=float(r[cLow]),
                             c=float(r[cClose]), v=int(r[cVol]), val=float(r[cAmt]), chg=float(r[cChg]), mcap=mcap))
        except Exception:
            continue
    if len(rows) < 100:
        raise RuntimeError(f"fdr 목록 {len(rows)}종목뿐")
    start = (dt.datetime.strptime(D, "%Y%m%d") - dt.timedelta(days=330)).strftime("%Y%m%d")
    hist = fill_hist_naver(D, rows, start)
    return rows, hist, naver_index_closes(D), "naver"


def fill_hist_naver(D, rows, start):
    """rows 전부(시총 0.9조 이상)의 네이버 이력. 당일 행이 없으면 미게시로 판단."""
    top = sorted(rows, key=lambda x: -x["mcap"])[0]
    h0 = naver_hist(top["code"], start, D)
    if not h0 or h0[-1][0] != D:
        raise RuntimeError(f"네이버 이력 마지막 날짜 {h0[-1][0] if h0 else None} ≠ {D} (당일 미반영)")
    hist = {}
    for i, x in enumerate(rows):
        for attempt in range(2):
            try:
                hist[x["code"]] = naver_hist(x["code"], start, D)
                break
            except Exception as e:
                log("naver hist fail", x["code"], repr(e)[:80])
                time.sleep(2)
        if i % 50 == 0:
            log("naver hist", i, "/", len(rows))
        time.sleep(0.12)
    return hist


# ── 경로 C: 네이버 모바일 목록 + 이력 ─────────────────────────────
def pick_unit(v, lo, hi, cands):
    for m in cands:
        if v is not None and lo <= v * m <= hi:
            return m
    return None


def build_from_naver(D):
    items, mult = [], None
    for mk in ("KOSPI", "KOSDAQ"):
        for page in range(1, 40):
            url = f"https://m.stock.naver.com/api/stocks/marketValue/{mk}?page={page}&pageSize=100"
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            j = r.json()
            stocks = j.get("stocks") or j.get("result") or []
            if not stocks:
                break
            if mult is None:   # 첫 페이지 최상단(삼성전자)으로 시총 단위 판정: 100조~3000조
                log("naver list keys", mk, sorted(stocks[0].keys())[:40])
                topmv = num(stocks[0].get("marketValue"))
                mult = pick_unit(topmv, 1e14, 3e15, (1e8, 1e6, 1e4, 1))
                if not mult:
                    raise RuntimeError(f"네이버 시총 단위 판정 실패 top={topmv}")
                log("naver mcap unit ×", mult)
            for s in stocks:
                s["_mk"] = mk
                items.append(s)
            mv = num(stocks[-1].get("marketValue"))
            if mv is not None and mv * mult < MIN_MCAP * 0.9:
                break   # 시총 순 정렬 — 1조 아래로 내려가면 중단
            time.sleep(0.2)
    if len(items) < 100:
        raise RuntimeError(f"네이버 목록 {len(items)}종목뿐")
    rows = []
    for s in items:
        code = str(s.get("itemCode") or s.get("code") or "").zfill(6)
        name = str(s.get("stockName") or s.get("name") or "")
        mcap = (num(s.get("marketValue")) or 0) * mult
        if mcap < MIN_MCAP * 0.9 or not is_common_stock(code, name):
            continue
        if str(s.get("stockEndType") or "stock").lower() not in ("stock", ""):
            continue
        rows.append(dict(code=code, name=name, mk=s["_mk"], mcap=mcap, o=None, h=None, l=None, c=num(s.get("closePrice")), v=None, val=None, chg=num(s.get("fluctuationsRatio"))))
    if len(rows) < 100:
        raise RuntimeError(f"네이버 시총 1조 이상 {len(rows)}종목뿐 (단위 오판 가능)")
    start = (dt.datetime.strptime(D, "%Y%m%d") - dt.timedelta(days=330)).strftime("%Y%m%d")
    hist = fill_hist_naver(D, rows, start)
    # 당일 시가·고가·저가·종가·거래량·거래대금·등락률은 이력 마지막 행으로 채움
    out = []
    for x in rows:
        h = hist.get(x["code"]) or []
        if not h or h[-1][0] != D:
            continue
        d0 = h[-1]
        prev = h[-2][4] if len(h) >= 2 else None
        x.update(o=d0[1], h=d0[2], l=d0[3], c=d0[4], v=d0[5], val=d0[4] * d0[5],
                 chg=(d0[4] / prev - 1) * 100 if prev else (x["chg"] or 0.0))
        out.append(x)
    return out, hist, naver_index_closes(D), "naver"


# ── 판정 ─────────────────────────────────────────────────────────
def judge(D, rows, hist, closes, src):
    flags = []
    universe = [x for x in rows if x["mcap"] >= MIN_MCAP]
    # 60거래일 수익률 (기준 집단)
    ret60 = {}
    for x in universe:
        h = [r for r in hist.get(x["code"], []) if r[0] <= D]
        if len(h) >= 61 and h[-61][4] > 0:
            ret60[x["code"]] = (h[-1][4] / h[-61][4] - 1) * 100
    rsr = rsr_rank(ret60)
    reg = ma_regime(closes)
    reg["name"] = "코스피"
    k60 = reg.get("ret60")
    pool = [x for x in universe if x["val"] >= MIN_VAL and x["c"] >= MIN_PRICE and MIN_CHG <= x["chg"] < MAX_CHG]
    cands, thin, split = [], [], []
    for x in pool:
        h = [r for r in hist.get(x["code"], []) if r[0] <= D]
        prev = [r for r in h if r[0] < D][-60:]
        if len(prev) < 40:
            thin.append(f"{x['name']} {len(prev)}행")
            continue
        hi60 = max(r[2] for r in prev)
        # 권리락 의심: 전일 대비 -40% 봉
        drop = any(h[i - 1][4] > 0 and h[i][4] < h[i - 1][4] * 0.6 for i in range(1, len(h)))
        if drop:
            split.append(x["name"])
        if x["c"] > hi60:
            cs, vd = verdict(x["o"], x["h"], x["l"], x["c"])
            c = dict(name=x["name"], code=x["code"], mk=x["mk"], price=int(round(x["c"])), chg=round(x["chg"], 2),
                     mcap=won_str(x["mcap"]), mcapWon=int(x["mcap"]), val=won_str(x["val"]), valWon=int(x["val"]),
                     hi60=int(round(hi60)), cs=cs, verdict=vd, ohlc=ohlc_str(h))
            if x["code"] in ret60:
                c["ret60"] = round(ret60[x["code"]], 1)
                if k60 is not None:
                    c["rs"] = int(round(ret60[x["code"]] - k60))
            if x["code"] in rsr:
                c["rsr"] = rsr[x["code"]]
            if drop:
                c["splitSuspect"] = True
            # 52주(약 250거래일) 신고가 표시 — 이력이 충분할 때만
            full = hist.get(x["code"], [])
            if len(full) >= 200:
                c["w52"] = bool(x["c"] > max(r[2] for r in full[:-1]))
            cands.append(c)
    cands.sort(key=lambda c: -c["valWon"])
    srcName = {"krx": "한국거래소 원본(pykrx)", "naver": "네이버 시세"}.get(src, src)
    flags.insert(0, f"국내 시세: {srcName} · 기준일 {D[:4]}-{D[4:6]}-{D[6:]} 당일 종가 · 풀 {len(pool)} → 60일 신고가 {len(cands)} · RS 기준 집단 {len(ret60)}종목")
    if thin:
        flags.append("이력 부족으로 판정 제외: " + ", ".join(thin))
    if split:
        flags.append("권리락 의심(-40% 봉): " + ", ".join(split) + " — 조정 여부 확인 필요")
    if "ma200" not in reg:
        flags.append("코스피 200일선 미계산(지수 이력 부족)")
    uni = []
    for x in sorted(universe, key=lambda x: -x["mcap"]):
        u = dict(code=x["code"], name=x["name"], mk=x["mk"], price=int(round(x["c"])), chg=round(x["chg"], 2), mcapWon=int(x["mcap"]), valWon=int(x["val"] or 0))
        if x["code"] in ret60:
            u["ret60"] = round(ret60[x["code"]], 1)
        if x["code"] in rsr:
            u["rsr"] = rsr[x["code"]]
        uni.append(u)
    date = f"{D[:4]}-{D[4:6]}-{D[6:]}"
    out = {"market": "KR", "date": date, "src": src, "generatedAt": now_kst().isoformat(timespec="seconds"),
           "regime": reg, "universe": {"n": len(universe), "minMcap": MIN_MCAP, "rsN": len(ret60)},
           "highs": {"raw": len(pool), "pass": len(cands), "basDt": date, "src": src},
           "candidates": cands, "poolCodes": [x["code"] for x in pool], "flags": flags}
    return out, uni


# ── 부가 산출물: 관심 종목 신호 · 시장 폭 · 업종 RS ──────────────
def load_sectors_kr():
    """code→업종(한국거래소 업종 분류). FinanceDataReader KRX-DESC → 실패하면 캐시."""
    cache = "data/kr/sectors.json"
    by = {}
    try:
        import FinanceDataReader as fdr
        lst = fdr.StockListing("KRX-DESC")
        cols = {c.lower(): c for c in lst.columns}
        cc = cols.get("code") or cols.get("symbol"); cs = cols.get("sector") or cols.get("industry")
        if cc and cs:
            for _, r in lst.iterrows():
                code, sec = str(r[cc]).zfill(6), str(r[cs]).strip()
                if sec and sec != "nan":
                    by[code] = sec
        if len(by) > 500:
            write_json(cache, {"at": now_kst().strftime("%Y-%m-%d"), "by": by})
            return by
        log("KRX-DESC 업종", len(by), "건뿐 → 캐시")
    except Exception as e:
        log("KRX-DESC 실패 → 캐시", repr(e)[:80])
    try:
        import json as _j
        return _j.load(open(cache, encoding="utf-8")).get("by", {})
    except Exception:
        return {}


def extras(D, uni, hist):
    date = f"{D[:4]}-{D[4:6]}-{D[6:]}"
    techs = {}
    for u in uni:
        t = tech(hist.get(u["code"], []), D, drop_pct=-8.0)
        if t:
            techs[u["code"]] = t
    sig_rows = []
    for u in uni:
        t = techs.get(u["code"])
        if not t:
            continue
        r = {"code": u["code"], "name": u["name"], "mk": u["mk"], "mcapWon": u["mcapWon"]}
        if u.get("rsr") is not None:
            r["rsr"] = u["rsr"]
        r.update(t)
        sig_rows.append(r)
    write_json("data/kr/signals.json", {"market": "KR", "date": date, "n": len(sig_rows), "rows": sig_rows})
    b = breadth(techs)
    if b:
        append_series("data/kr/breadth.json", date, b)
    secs = load_sectors_kr()
    if secs:
        sr = sector_rs(uni, techs, secs)
        append_series("data/kr/sector_rs.json", date, {"sectors": sr}, keep=70)
        log("sector_rs", len(sr), "업종")
    return len(sig_rows)


def main():
    D = sys.argv[1] if len(sys.argv) > 1 else today_str()
    deadline = time.time() + 40 * 60
    last_err = None
    while True:
        for name, fn in (("pykrx", build_from_pykrx), ("fdr", build_from_fdr), ("naver", build_from_naver)):
            try:
                log("try", name, D)
                rows, hist, closes, src = fn(D)
                if len(rows) < 100:
                    raise RuntimeError(f"{name}: 종목 {len(rows)}개뿐")
                out, uni = judge(D, rows, hist, closes, src)
                write_json(f"data/kr/{out['date']}.json", out)
                write_json("data/kr/latest.json", out)
                write_json("data/kr/universe.json", {"date": out["date"], "rows": uni})
                try:
                    log("signals", extras(D, uni, hist))
                except Exception as e:
                    log("extras FAIL", repr(e)[:200]); traceback.print_exc()
                write_status("KR", True, f"{out['date']} {src} 풀 {out['highs']['raw']} → 통과 {out['highs']['pass']}",
                             {"date": out["date"], "src": src, "pass": out["highs"]["pass"]})
                log("done", out["highs"])
                return 0
            except Exception as e:
                last_err = f"{name}: {repr(e)[:200]}"
                log("FAIL", last_err)
                traceback.print_exc()
        if time.time() > deadline:
            break
        log("당일 데이터 미게시 또는 실패 — 5분 후 재시도")
        time.sleep(300)
    write_status("KR", False, f"{D} 실패: {last_err}", {"date": D})
    return 1


if __name__ == "__main__":
    sys.exit(main())
