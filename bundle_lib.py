"""
Must See '마켓' 화면용 데이터 묶음 만들기.

모으는 것
- 지수·섹터·대형주: research-automation 저장소 market/market_data.json (워크플로가 ra/ 에 받아 둠)
- 미국 실적 일정·기업 리스트: research-automation market/us_earnings.json
- 한국 실적 일정(KIND 예고·AWAKE IR·잠정실적)·기업 리스트: 이 저장소 calendar/calendar.json (DAOL 매크로는 뺌)
- 매크로 일정: Forex Factory 주간 경제지표(JSON) → mustsee/market/macro_hist.json 에 누적
  + 한국 고정 일정(금통위, 매월 1일 수출입)

결과: mustsee/market/bundle.json (커밋하지 않음, build_site.py 가 암호화해서 페이지에 올림)
기존 친구용 아티팩트 파이프라인(calendar.json, research-automation)은 읽기만 한다.

사용: python mustsee/market_bundle.py [--ra ra]
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
OUT_DIR = BASE / "market"
HIST = OUT_DIR / "macro_hist.json"
BUNDLE = OUT_DIR / "bundle.json"
LOG = OUT_DIR / "last_run.log"
INTRA_DIR = OUT_DIR / "intraday"          # 장중 5분봉, 하루(08:00~다음날 08:00 KST)에 파일 하나
INTRA_KEEP = 120                           # 보관 일수
# 장중 타임라인 종목: (야후 기호, 키, 이름, 종류) — 종류 rate 는 bp, 나머지는 %
INTRA = [
    ("ES=F", "ES", "S&P 선물", "px"), ("NQ=F", "NQ", "나스닥 선물", "px"),
    ("^GSPC", "SPX", "S&P 500", "px"), ("^IXIC", "IXIC", "나스닥", "px"),
    ("^KS11", "KOSPI", "코스피", "px"), ("^KQ11", "KOSDAQ", "코스닥", "px"),
    ("^N225", "N225", "니케이", "px"), ("^HSI", "HSI", "항셍", "px"),
    ("^TNX", "US10Y", "미 10년", "rate"), ("KRW=X", "USDKRW", "원/달러", "px"),
    ("DX-Y.NYB", "DXY", "달러인덱스", "px"), ("CL=F", "WTI", "WTI", "px"),
    ("GC=F", "GOLD", "금", "px"), ("^VIX", "VIX", "VIX", "px"),
]
# 섹터 예상 채점용 미국 섹터·스타일 ETF 일봉
SEC_ETF = ["SPY", "XLK", "SOXX", "XLC", "XLY", "XLP", "XLV", "XLF", "XLI", "XLE", "XLB", "XLU", "XLRE", "IVW", "IVE"]
KST = timezone(timedelta(hours=9))

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]
KEEP_DAYS = 400                      # 매크로 일정 보관 기간
HIGH_OK = {"USD", "CNY", "EUR", "JPY", "GBP"}   # 중요도 '상'은 이 나라만 (호주·뉴질랜드·캐나다·스위스 제외)
MEDIUM_OK = {"USD", "CNY", "EUR", "JPY"}         # 중요도 '중'은 이 나라만
CTRY = {"USD": "US", "EUR": "EU", "GBP": "UK", "JPY": "JP", "CNY": "CN", "AUD": "AU",
        "NZD": "NZ", "CAD": "CA", "CHF": "CH", "ALL": "GL"}

# 자주 나오는 지표 이름 한국어로 (없는 건 영어 그대로)
KO = {
    "CPI m/m": "CPI 전월비", "CPI y/y": "CPI 전년비", "Core CPI m/m": "근원 CPI 전월비", "Core CPI y/y": "근원 CPI 전년비",
    "PPI m/m": "PPI 전월비", "Core PPI m/m": "근원 PPI 전월비", "PPI y/y": "PPI 전년비",
    "Non-Farm Employment Change": "비농업 고용", "Unemployment Rate": "실업률",
    "Average Hourly Earnings m/m": "평균 시간당 임금", "Unemployment Claims": "신규 실업수당 청구",
    "ADP Non-Farm Employment Change": "ADP 민간고용", "JOLTS Job Openings": "JOLTS 구인",
    "Retail Sales m/m": "소매판매", "Core Retail Sales m/m": "근원 소매판매",
    "Advance GDP q/q": "GDP 속보치", "Prelim GDP q/q": "GDP 잠정치", "Final GDP q/q": "GDP 확정치",
    "Advance GDP Price Index q/q": "GDP 물가지수 속보치",
    "Core PCE Price Index m/m": "근원 PCE 물가 전월비", "PCE Price Index m/m": "PCE 물가 전월비",
    "ISM Manufacturing PMI": "ISM 제조업 PMI", "ISM Services PMI": "ISM 서비스업 PMI",
    "ISM Manufacturing Prices": "ISM 제조업 가격지수",
    "Federal Funds Rate": "FOMC 기준금리 결정", "FOMC Statement": "FOMC 성명서",
    "FOMC Press Conference": "FOMC 기자회견", "FOMC Meeting Minutes": "FOMC 의사록",
    "FOMC Economic Projections": "FOMC 경제전망(점도표)",
    "Prelim UoM Consumer Sentiment": "미시간대 소비자심리(예비)", "Revised UoM Consumer Sentiment": "미시간대 소비자심리(확정)",
    "Prelim UoM Inflation Expectations": "미시간대 기대인플레(예비)",
    "CB Consumer Confidence": "컨퍼런스보드 소비자신뢰", "Empire State Manufacturing Index": "뉴욕 제조업지수",
    "Philly Fed Manufacturing Index": "필라델피아 제조업지수", "Durable Goods Orders m/m": "내구재 주문",
    "Core Durable Goods Orders m/m": "근원 내구재 주문",
    "Building Permits": "건축허가", "Housing Starts": "주택착공", "Existing Home Sales": "기존주택판매",
    "New Home Sales": "신규주택판매", "Pending Home Sales m/m": "잠정주택판매",
    "Flash Manufacturing PMI": "제조업 PMI 속보", "Flash Services PMI": "서비스업 PMI 속보",
    "Final Manufacturing PMI": "제조업 PMI 확정", "Final Services PMI": "서비스업 PMI 확정",
    "Trade Balance": "무역수지", "Industrial Production m/m": "산업생산", "Industrial Production y/y": "산업생산 전년비",
    "Caixin Manufacturing PMI": "차이신 제조업 PMI", "Caixin Services PMI": "차이신 서비스업 PMI",
    "Manufacturing PMI": "제조업 PMI", "Non-Manufacturing PMI": "비제조업 PMI",
    "GDP q/y": "GDP 전년비", "Retail Sales y/y": "소매판매 전년비", "New Loans": "신규 위안화 대출",
    "Main Refinancing Rate": "ECB 기준금리 결정", "ECB Press Conference": "ECB 기자회견",
    "Monetary Policy Statement": "통화정책 성명", "Monetary Policy Meeting Minutes": "통화정책회의 의사록",
    "BOJ Policy Rate": "BOJ 기준금리 결정", "BOJ Press Conference": "BOJ 기자회견",
    "BOJ Outlook Report": "BOJ 경제·물가 전망", "Tokyo Core CPI y/y": "도쿄 근원 CPI",
    "National Core CPI y/y": "전국 근원 CPI", "Official Bank Rate": "BOE 기준금리 결정",
    "German Prelim CPI m/m": "독일 CPI 속보 전월비", "German Prelim GDP q/q": "독일 GDP 속보",
    "Prelim Flash GDP q/q": "GDP 속보", "Annual Budget Release": "연간 예산안 발표", "Core CPI Flash Estimate y/y": "근원 CPI 속보 전년비",
    "CPI Flash Estimate y/y": "CPI 속보 전년비", "German ifo Business Climate": "독일 ifo 기업환경", "German Flash Manufacturing PMI": "독일 제조업 PMI 속보",
    "Treasury Currency Report": "美 재무부 환율보고서", "Crude Oil Inventories": "원유 재고",
    "Employment Cost Index q/q": "고용비용지수", "Prelim Unit Labor Costs q/q": "단위노동비용(예비)",
}
COUNTRY_KO = {"US": "미국", "EU": "유럽", "UK": "영국", "JP": "일본", "CN": "중국", "AU": "호주",
              "NZ": "뉴질랜드", "CA": "캐나다", "CH": "스위스", "GL": "글로벌", "KR": "한국"}

# 야후 경제 캘린더: 주요국 핵심 지표만 (이름은 야후 표기에서 끝의 * 를 뗀 것). H=중요도 상, M=중
YAHOO_PICK = {
    "US": {
        "Non-Farm Payrolls": ("비농업 고용", "H"), "Unemployment Rate": ("실업률", "H"),
        "CPI MM, SA": ("CPI 전월비", "H"), "CPI YY, NSA": ("CPI 전년비", "H"),
        "Core CPI MM, SA": ("근원 CPI 전월비", "H"), "Core CPI YY, NSA": ("근원 CPI 전년비", "H"),
        "FOMC Decision Fed Funds Tgt Rate": ("FOMC 기준금리 결정", "H"), "GDP Advance": ("GDP 속보치", "H"),
        "Core PCE Price Index MM": ("근원 PCE 물가 전월비", "H"), "Core PCE Price Index YY": ("근원 PCE 물가 전년비", "H"),
        "ISM Manufacturing PMI": ("ISM 제조업 PMI", "H"), "ISM N-Mfg PMI": ("ISM 서비스업 PMI", "H"),
        "Retail Sales MM": ("소매판매 전월비", "H"), "PPI Final Demand MM": ("PPI 전월비", "H"),
        "Initial Jobless Clm": ("신규 실업수당 청구", "M"), "ADP National Employment": ("ADP 민간고용", "M"),
        "Average Earnings MM": ("평균 시간당 임금", "M"), "PPI Final Demand YY": ("PPI 전년비", "M"),
        "U Mich Sentiment Prelim": ("미시간대 소비자심리(예비)", "M"), "U Mich 1Yr Inf Prelim": ("미시간대 1년 기대인플레(예비)", "M"),
        "Consumer Confidence": ("컨퍼런스보드 소비자신뢰", "M"), "Retail Control": ("소매판매 컨트롤그룹", "M"),
        "Durable Goods": ("내구재 주문", "M"), "PCE Price Index YY": ("PCE 물가 전년비", "M"),
        "Industrial Production MM": ("산업생산", "M"), "Housing Starts Number": ("주택착공", "M"),
        "Existing Home Sales": ("기존주택판매", "M"), "Employment Costs": ("고용비용지수", "M"),
        "Philly Fed Business Indx": ("필라델피아 제조업지수", "M"), "NY Fed Manufacturing": ("뉴욕 제조업지수", "M"),
        "JOLTS Job Openings": ("JOLTS 구인", "M"),
    },
    "CN": {
        "CPI YY": ("CPI 전년비", "H"), "PPI YY": ("PPI 전년비", "H"), "NBS Manufacturing PMI": ("국가통계국 제조업 PMI", "H"),
        "Exports YY": ("수출 전년비", "H"), "Industrial Output YY": ("산업생산 전년비", "H"), "Retail Sales YY": ("소매판매 전년비", "H"),
        "NBS Non-Mfg PMI": ("국가통계국 비제조업 PMI", "M"), "Trade Balance USD": ("무역수지", "M"), "Imports YY": ("수입 전년비", "M"),
        "New Yuan Loans": ("신규 위안화 대출", "M"), "M2 Money Supply YY": ("M2 전년비", "M"),
        "Urban Investment (YTD)YY": ("고정자산투자(누적)", "M"), "China House Prices YY": ("신규주택가격 전년비", "M"),
    },
    "JP": {
        "CPI, Core Nationwide YY": ("전국 근원 CPI", "H"), "CPI Tokyo Ex fresh food YY": ("도쿄 근원 CPI", "H"),
        "GDP QQ Annualised": ("GDP(연율)", "H"), "Unemployment Rate": ("실업률", "M"), "Exports YY": ("수출 전년비", "M"),
        "Industrial O/P Prelim MM SA": ("산업생산(예비)", "M"), "Retail Sales YY": ("소매판매 전년비", "M"),
        "Trade Balance Total Yen": ("무역수지", "M"), "BOJ Policy Rate": ("BOJ 기준금리 결정", "H"),
    },
    "EU": {
        "ECB Refinancing Rate": ("ECB 기준금리 결정", "H"), "HICP Flash YY": ("HICP 속보 전년비", "H"),
        "GDP Flash Estimate QQ": ("GDP 속보 전기비", "H"), "Unemployment Rate": ("실업률", "M"),
        "Economic Sentiment": ("경기체감지수", "M"), "Retail Sales MM": ("소매판매 전월비", "M"),
    },
    "DE": {
        "CPI Prelim YY": ("독일 CPI 속보 전년비", "M"), "ZEW Economic Sentiment": ("독일 ZEW 경기기대", "M"),
        "GDP Flash QQ SA": ("독일 GDP 속보", "M"), "Industrial Output MM": ("독일 산업생산", "M"),
    },
    "GB": {
        "BOE Bank Rate": ("BOE 기준금리 결정", "H"), "CPI YY": ("CPI 전년비", "H"),
        "GDP Prelim QQ": ("GDP 예비 전기비", "M"), "Retail Sales MM": ("소매판매 전월비", "M"), "ILO Unemployment Rate": ("실업률", "M"),
    },
    "KR": {
        "Export Growth Prelim": ("수출 증가율(잠정)", "H"), "CPI Growth YY": ("CPI 전년비", "H"),
        "GDP Growth QQ Advance": ("GDP 속보 전기비", "H"), "Trade Balance Prelim": ("무역수지(잠정)", "M"),
        "Unemployment Rate": ("실업률", "M"), "Industrial Output Growth": ("산업생산", "M"),
        "Current Account Bal NSA": ("경상수지", "M"), "PPI Growth YY": ("PPI 전년비", "M"),
        "Consumer Sentiment Ind": ("소비자심리지수", "M"), "FX Reserves": ("외환보유액", "M"),
    },
}
YAHOO_CTRY = {"US": "US", "CN": "CN", "JP": "JP", "EU": "EU", "DE": "DE", "GB": "UK", "KR": "KR"}
FF_COVERED = {"US", "CN", "JP", "EU", "DE", "UK"}   # 이번 주는 Forex Factory(중요도·예상치)가 더 정확해서 야후는 한국만 씀
MONTH_KO = {m: f"{i}월" for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def yahoo_event(r: dict) -> dict | None:
    reg = (r.get("Region") or "").strip()
    name = re.sub(r"\s*\*\s*$", "", (r.get("Event") or "").strip())
    pick = YAHOO_PICK.get(reg, {}).get(name)
    if not pick:
        return None
    try:
        dt = datetime.fromisoformat(str(r["Event Time"])).astimezone(timezone.utc)
    except Exception:
        return None
    # 미국·유럽 지표가 UTC 자정이면 시각 미정(종일)
    allday = dt.strftime("%H:%M") == "00:00" and reg in ("US", "EU", "DE", "GB")
    k = dt.astimezone(KST)
    per = r.get("For") or ""
    per = re.sub(r"\b(" + "|".join(MONTH_KO) + r")\b", lambda m: MONTH_KO[m.group(1)], per)
    per = re.sub(r"\bQ([1-4])\b", r"\1분기", per)
    bits = []
    if not allday:
        bits.append(k.strftime("%H:%M") + " KST")
    if per:
        bits.append(per + "분")
    for lab, key in (("예상", "Expected"), ("이전", "Last")):
        if r.get(key) not in (None, "", "None"):
            bits.append(f"{lab} {r[key]}")
    return {"date": k.strftime("%Y-%m-%d"), "time": "" if allday else k.strftime("%H:%M"),
            "country": YAHOO_CTRY[reg], "title": pick[0], "orig": name, "imp": pick[1],
            "forecast": r.get("Expected") or "", "previous": r.get("Last") or "",
            "actual": "" if r.get("Actual") in (None, "None") else r["Actual"],
            "note": " · ".join(bits), "src": "yahoo", "per": per}


def dedupe_windows(events: list[dict]) -> list[dict]:
    """야후는 발표일이 정해지지 않은 지표(중국 M2·신규대출 등)를 기간 내 매일 올림 → 첫날 하나만, 기간 표시."""
    groups: dict = {}
    for e in sorted(events, key=lambda x: (x["date"], x.get("time") or "")):
        groups.setdefault((e["country"], e["title"], e.get("per") or e["date"]), []).append(e)
    out = []
    for g in groups.values():
        e = dict(g[0])
        if len(g) > 1:
            e["note"] = (e["note"] + " · " if e["note"] else "") + f"{g[0]['date'][5:].replace('-', '/')}~{g[-1]['date'][5:].replace('-', '/')} 중 발표"
            e["end"] = g[-1]["date"]
        out.append(e)
    return out


# 한국 고정 일정 — 금통위(기준금리 결정) 일자. 매년 연말 한은이 다음 해 일정을 공개하면 여기에 추가
BOK_MPC = ["2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28",
           "2026-07-16", "2026-08-27", "2026-10-22", "2026-11-26"]

logs: list[str] = []


def log(s: str):
    print(s)
    logs.append(s)


def ko_title(title: str, country: str) -> str:
    t = title.strip()
    if t in KO:
        return KO[t]
    m = re.match(r"^(Fed Chair|ECB President|BOJ Gov|BOE Gov) (.+?) Speaks$", t)
    if m:
        who = {"Fed Chair": "연준 의장", "ECB President": "ECB 총재", "BOJ Gov": "BOJ 총재", "BOE Gov": "BOE 총재"}[m.group(1)]
        return f"{who} {m.group(2)} 발언"
    return t


def fetch_ff() -> list[dict]:
    out = []
    for url in FF_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (doni-must-see market calendar)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = json.loads(r.read().decode("utf-8"))
            log(f"[매크로] {url.rsplit('/', 1)[-1]} {len(rows)}건 수신")
            out.extend(rows)
        except Exception as e:  # 다음 주 파일은 주 후반에만 올라오는 경우가 있음
            log(f"[매크로] {url.rsplit('/', 1)[-1]} 수신 실패: {e}")
    return out


def to_event(r: dict) -> dict | None:
    imp = (r.get("impact") or "").strip()
    cur = (r.get("country") or "").strip().upper()
    if imp not in ("High", "Medium") or (imp == "High" and cur not in HIGH_OK) or (imp == "Medium" and cur not in MEDIUM_OK):
        return None
    try:
        dt = datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00")).astimezone(KST)
    except Exception:
        return None
    c = CTRY.get(cur, cur)
    title = ko_title(r.get("title") or "", c)
    # FF 는 시각이 정해지지 않은 일정(종일·잠정)을 자정으로 둔다
    allday = "T00:00:00" in str(r["date"])
    bits = []
    if not allday:
        bits.append(dt.strftime("%H:%M") + " KST")
    if r.get("forecast"):
        bits.append("예상 " + str(r["forecast"]))
    if r.get("previous"):
        bits.append("이전 " + str(r["previous"]))
    return {
        "date": dt.strftime("%Y-%m-%d"), "time": "" if allday else dt.strftime("%H:%M"),
        "country": c, "title": title, "orig": r.get("title") or "",
        "imp": "H" if imp == "High" else "M", "forecast": r.get("forecast") or "",
        "previous": r.get("previous") or "", "actual": r.get("actual") or "",
        "note": " · ".join(bits), "src": "ff",
    }


def fetch_yahoo(start: str, end: str) -> list[dict]:
    """야후 파이낸스 경제 캘린더 (finance.yahoo.com/calendar/economic 과 같은 데이터)."""
    try:
        import time
        import yfinance as yf
    except ImportError:
        log("[야후] yfinance 없음")
        return []
    rows, off = [], 0
    try:
        cal = yf.Calendars(start=start, end=end)
        while off < 3000:
            df = cal.get_economic_events_calendar(start=start, end=end, limit=100, offset=off, force=True)
            if df is None or df.empty:
                break
            df = df.reset_index()
            for rec in df.to_dict("records"):
                rows.append({str(k): (None if v is None or (isinstance(v, float) and v != v) else str(v)) for k, v in rec.items()})
            if len(df) < 100:
                break
            off += 100
            time.sleep(0.4)
    except Exception as e:
        log(f"[야후] 수신 실패: {e}")
    log(f"[야후] {start}~{end} {len(rows)}건 수신")
    return rows


def fetch_intraday(today: date):
    """야후 5분봉(프리·애프터 포함) → 날짜별 파일에 합쳐 저장. 하루 = 08:00 KST ~ 다음날 08:00."""
    try:
        import yfinance as yf
    except ImportError:
        return
    INTRA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df = yf.download([t[0] for t in INTRA], period="5d", interval="5m", prepost=True,
                         group_by="ticker", auto_adjust=False, progress=False, threads=True)
    except Exception as e:
        log(f"[장중] 수신 실패: {e}")
        return
    days: dict = {}
    got = 0
    for sym, key, name, kind in INTRA:
        try:
            ser = df[sym]["Close"].dropna()
        except Exception:
            continue
        if ser.empty:
            continue
        got += 1
        idx = ser.index.tz_convert("Asia/Seoul") if ser.index.tz is not None else ser.index.tz_localize("UTC").tz_convert("Asia/Seoul")
        for ts, v in zip(idx, ser.values):
            w = (ts - timedelta(hours=8))
            day = w.strftime("%Y-%m-%d")
            m = int(w.hour * 60 + w.minute)          # 08:00 기준 몇 분째
            days.setdefault(day, {}).setdefault(key, {})[m] = round(float(v), 4)
    for day, series in days.items():
        f = INTRA_DIR / f"{day}.json"
        old = load_json(f) or {"date": day, "s": {}}
        for key, pts in series.items():
            cur = {int(m): v for m, v in old["s"].get(key, [])}
            cur.update(pts)
            old["s"][key] = sorted([m, v] for m, v in cur.items())
        old["names"] = {k: [n, kd] for _, k, n, kd in INTRA}
        f.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    cut = (today - timedelta(days=INTRA_KEEP)).isoformat()
    for f in INTRA_DIR.glob("*.json"):
        if f.stem < cut:
            f.unlink()
    log(f"[장중] 종목 {got}/{len(INTRA)} · 날짜 {', '.join(sorted(days))}")


def fetch_sectors() -> dict:
    """미국 섹터·스타일 ETF 일봉 종가 (최근 6개월)."""
    try:
        import yfinance as yf
        df = yf.download(SEC_ETF, period="6mo", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
    except Exception as e:
        log(f"[섹터 ETF] 수신 실패: {e}")
        return {}
    out = {}
    for sym in SEC_ETF:
        try:
            ser = df[sym]["Close"].dropna()
        except Exception:
            continue
        out[sym] = [[ts.strftime("%Y-%m-%d"), round(float(v), 3)] for ts, v in zip(ser.index, ser.values)]
    log(f"[섹터 ETF] {len(out)}/{len(SEC_ETF)}개 · 마지막 {max((v[-1][0] for v in out.values() if v), default='-')}")
    return out


def korea_fixed(today: date) -> list[dict]:
    ev = [{"date": d, "time": "", "country": "KR", "title": "금통위 기준금리 결정", "orig": "",
           "imp": "H", "note": "한국은행 금융통화위원회", "src": "fixed"} for d in BOK_MPC]
    return ev


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ra", default="ra", help="research-automation 체크아웃 폴더")
    a = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(KST)
    today = now.date()

    # 1) 매크로: Forex Factory(이번 주, 중요도·예상치) + 야후(지난 1주 ~ 앞으로 6주) → 기존 누적과 합침
    hist = load_json(HIST) or {"events": []}
    old = [e for e in hist.get("events", []) if e.get("src") in ("ff", "yahoo")]

    def replace(events, new, src, lo, hi):
        """src 의 [lo, hi] 기간 기록은 이번에 받은 것으로 교체 (일정 이동·취소 반영)."""
        if not new:
            return events
        return [e for e in events if not (e["src"] == src and lo <= e["date"] <= hi)] + new

    ff = [e for e in (to_event(r) for r in fetch_ff()) if e]
    if ff:
        old = replace(old, ff, "ff", min(e["date"] for e in ff), max(e["date"] for e in ff))
    ylo, yhi = (today - timedelta(days=7)).isoformat(), (today + timedelta(days=42)).isoformat()
    ya = dedupe_windows([e for e in (yahoo_event(r) for r in fetch_yahoo(ylo, yhi)) if e])
    old = replace(old, ya, "yahoo", ylo, yhi)
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    events = sorted((e for e in old if e["date"] >= cutoff), key=lambda e: (e["date"], e.get("time") or "", e["src"]))
    HIST.write_text(json.dumps({"updated": now.strftime("%Y-%m-%d %H:%M"), "events": events},
                               ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    # 화면용: Forex Factory 가 있는 날짜(주요국)는 FF 것만, 그 밖의 날짜·한국은 야후
    ff_days = {e["date"] for e in events if e["src"] == "ff"}
    shown = [e for e in events if e["src"] == "ff" or not (e["country"] in FF_COVERED and e["date"] in ff_days)]
    log(f"[매크로] FF {len(ff)}건 · 야후 {len(ya)}건 · 누적 {len(events)}건 · 화면 {len(shown)}건 (마지막 일정 {max((e['date'] for e in shown), default='-')})")
    macro = shown + korea_fixed(today)

    # 2) 지수·미국 실적 (research-automation)
    ra = ROOT / a.ra if not Path(a.ra).is_absolute() else Path(a.ra)
    data = load_json(ra / "market" / "market_data.json")
    us = load_json(ra / "market" / "us_earnings.json")
    log(f"[지수] {'갱신 ' + data.get('generated_at_kst', '') if data else '없음 — RA_READ_TOKEN 시크릿 확인 필요'}")
    log(f"[미국 실적] {'갱신 ' + us.get('generated_at_kst', '') if us else '없음'}")

    # 3) 한국 실적 (이 저장소 calendar.json, DAOL 은 제외)
    cal = load_json(ROOT / "calendar" / "calendar.json") or {}
    cal = {k: v for k, v in cal.items() if k != "daol"}
    log(f"[한국 실적] {'갱신 ' + str(cal.get('generated_at_kst', '')) if cal else '없음'}")

    # 4) 장중 5분봉 · 섹터 ETF
    fetch_intraday(today)
    sec = fetch_sectors()

    bundle = {"v": 1, "generated_at_kst": now.strftime("%Y-%m-%d %H:%M"),
              "data": data, "us": us, "cal": cal, "macro": macro, "sec": sec}
    BUNDLE.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"[묶음] {BUNDLE.stat().st_size // 1024:,}KB")
    LOG.write_text(now.strftime("%Y-%m-%d %H:%M KST") + "\n" + "\n".join(logs) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 마켓 묶음이 실패해도 Must See 배포는 계속
        log(f"[오류] {e}")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text(datetime.now(KST).strftime("%Y-%m-%d %H:%M KST") + "\n" + "\n".join(logs) + "\n", encoding="utf-8")
        sys.exit(0)
