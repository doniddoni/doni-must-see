"""
정해진 시각에 GitHub 작업 실행 (agent.py 가 1분마다 부름)

2026-11-07부터 깃허브 '예약 실행(schedule)'이 3~8시간씩 늦거나 아예 빠짐.
반면 '직접 실행(workflow_dispatch)'은 1분 안에 시작함 → 이 서버가 시각표대로 직접 실행 버튼을 누름.
깃허브 쪽 예약은 비상용으로 남겨 둠: 이 서버가 살아 있고 열쇠가 정상이면(status.json 의 sched.auth)
늦게 도착한 예약 실행은 첫 단계(gate)에서 바로 끝냄. 서버가 멈추면 예전처럼 예약대로(늦더라도) 돎.

열쇠: /etc/doni-live.env 의 GH_RUN_TOKEN 이 있으면 그것, 없으면 Must See '지금 가져오기' 열쇠
(data/sync.bin 의 run — GitHub Secret MUSTSEE_RUN_BUTTON_TOKEN). 두 저장소 모두 Actions 읽기·쓰기 권한 필요.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
RA = "doniddoni/research-automation"
TKM = "doniddoni/telegram-keyword-monitor"
REPOS = (RA, TKM)
ALL, WEEKDAY, TUE_SAT = set(range(7)), {0, 1, 2, 3, 4}, {1, 2, 3, 4, 5}   # 월=0

# (이름, 저장소, 워크플로 파일, 요일, 한국시각, 입력값)
JOBS = [
    ("증시 캘린더",      TKM, "calendar.yml",  TUE_SAT, "05:30", {}),
    ("시장 지수 1차",    RA,  "market.yml",    TUE_SAT, "06:15", {}),   # 미국 마감(06:10) 전이면 워크플로가 기다림
    ("뉴스 06:30",       RA,  "news.yml",      ALL,     "06:30", {"digest": "true"}),   # + 전일 종합 엑셀
    ("포트폴리오 가격",  RA,  "portfolio.yml", TUE_SAT, "06:35", {}),
    ("시장 지수 재확인", RA,  "market.yml",    TUE_SAT, "07:25", {"recheck": "true"}),
    ("Must See 오전",    TKM, "mustsee.yml",   ALL,     "07:55", {}),   # 전날 PDF 같이 전송
    ("스포티파이",       RA,  "spotify.yml",   ALL,     "08:40", {}),
    ("뉴스 10:30",       RA,  "news.yml",      WEEKDAY, "10:30", {}),
    ("한터",             RA,  "hanteo.yml",    ALL,     "12:30", {}),
    ("뉴스 14:00",       RA,  "news.yml",      WEEKDAY, "14:00", {}),
    ("뉴스 18:00",       RA,  "news.yml",      ALL,     "18:00", {}),
    ("Must See 밤",      TKM, "mustsee.yml",   ALL,     "22:55", {}),
]
GRACE = timedelta(minutes=40)   # 서버가 잠깐 멈췄다 돌아와도 이 안이면 실행
RETRY = 300                     # 실패하면 5분 뒤 다시
AUTH_EVERY = 1800               # 열쇠 권한 확인 30분마다


def _api(method, url, token, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "doni-live"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class Scheduler:
    def __init__(self, path, get_token, notify, log):
        self.path, self.get_token, self.notify, self.log = path, get_token, notify, log
        try:
            self.st = json.loads(path.read_text())
        except Exception:
            self.st = {}
        self.st.setdefault("done", {})       # "날짜|이름" → {"at", "code"}
        self.st.setdefault("fail", {})       # "날짜|이름" → {"n", "last", "code"}
        self.auth = {r: "확인 전" for r in REPOS}
        self.t_auth = 0.0
        self.token, self.t_token = None, 0.0

    def _save(self):
        cut = (datetime.now(KST) - timedelta(days=3)).strftime("%Y-%m-%d")
        for k in ("done", "fail"):
            self.st[k] = {x: v for x, v in self.st[k].items() if x[:10] >= cut}
        self.path.write_text(json.dumps(self.st, ensure_ascii=False))

    def _tok(self, force=False):
        if force or not self.token or time.time() - self.t_token > 1800:
            try:
                self.token = self.get_token() or None
            except Exception as e:
                self.log("실행 열쇠 읽기 실패", type(e).__name__)
            self.t_token = time.time()
        return self.token

    def check_auth(self):
        tok = self._tok(force=True)
        for r in REPOS:
            if not tok:
                self.auth[r] = "열쇠 없음"
                continue
            c = _api("GET", f"https://api.github.com/repos/{r}/actions/workflows?per_page=1", tok)
            # 읽기가 되고, 최근 실행 시도가 권한 문제로 막히지 않았으면 정상
            if c == 200 and not str(self.auth.get(r, "")).startswith("실행 거부"):
                self.auth[r] = "ok"
            elif c != 200:
                self.auth[r] = f"권한 없음 {c}"
        self.t_auth = time.time()

    def tick(self):
        """1분마다. 실행한 작업 이름 목록을 돌려줌"""
        n = datetime.now(KST)
        if time.time() - self.t_auth > AUTH_EVERY:
            self.check_auth()
        ran = []
        for name, repo, wf, days, hm, inputs in JOBS:
            for back in (0, 1):   # 자정 직전 작업이 자정을 넘겨 늦은 경우까지
                d = (n - timedelta(days=back)).date()
                if d.weekday() not in days:
                    continue
                slot = datetime.fromisoformat(f"{d.isoformat()}T{hm}:00+09:00")
                key = f"{d.isoformat()}|{name}"
                if not (slot <= n < slot + GRACE) or key in self.st["done"]:
                    continue
                f = self.st["fail"].get(key, {})
                if f and time.time() - f.get("last", 0) < RETRY:
                    continue
                tok = self._tok(force=bool(f))
                code = _api("POST", f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches", tok,
                            {"ref": "main", "inputs": inputs}) if tok else "열쇠 없음"
                if code == 204:
                    self.st["done"][key] = {"at": n.strftime("%H:%M"), "code": 204}
                    self.st["fail"].pop(key, None)
                    if str(self.auth.get(repo, "")).startswith("실행 거부"):
                        self.auth[repo] = "ok"
                    ran.append(name)
                    self.log("GitHub 실행", name)
                else:
                    f = {"n": f.get("n", 0) + 1, "last": time.time(), "code": code}
                    self.st["fail"][key] = f
                    if code in (401, 403, 404, "열쇠 없음"):
                        self.auth[repo] = f"실행 거부 {code}"
                    self.log("GitHub 실행 실패", name, code)
                    if f["n"] == 1:
                        self.notify(f"[Doni Live] GitHub 작업 실행 실패 — {name} ({wf}) 응답 {code}\n"
                                    "5분마다 다시 시도. 계속 실패하면 '지금 가져오기' 열쇠의 저장소·Actions 권한 확인 필요")
                self._save()
        return ran

    def status(self):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        runs = {k[11:]: v["at"] for k, v in self.st["done"].items() if k[:10] == today}
        fails = {k[11:]: str(v["code"]) for k, v in self.st["fail"].items() if k[:10] == today}
        return {"auth": dict(self.auth), "today": runs, "fail": fails, "jobs": len(JOBS)}
