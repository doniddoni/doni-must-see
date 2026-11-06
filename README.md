# Doni Live (agent 브랜치)

오라클 무료 서버에서 계속 도는 실시간 시장 수집기. Must See 마켓 화면이 읽어 감.

- `agent.py` 본체 (2분봉 시세 5분마다, 지표 발표 전후 1분마다 실제치 확인 → 텔레그램 알림)
- `watch.py` 급변 알림: Must See 마켓 '알림' 버튼에서 저장한 설정(stars 브랜치 `alert_conf.bin`, 암호화 — 봇 토큰·대화방·지수·종목·기준값)을 5분마다 읽어 판단
- `tg.py` 텔레그램 실시간 (agent.py 가 띄움): 서버 전용 로그인(QR, 알림 봇으로 전송) → 키워드 즉시 알림(엔미레 뉴스 봇) + Must See 새 글 감지 시 GitHub 수집 작업 실행. 설정은 Must See 사이트 `data/srv.bin`(암호화), 로그인은 서버 `/opt/doni-live/tg.session` 에만
- `bundle_lib.py` 지표 이름·종목 목록 (telegram-keyword-monitor `mustsee/market_bundle.py` 사본 — 그쪽을 고치면 여기도 복사)
- `install.sh` 처음 설치, `update.sh` 1시간마다 이 브랜치에서 새 코드 받기
- 결과: `live` 브랜치 `live.bin`(암호화) · `status.json`(시각·건수·오류만)
- 설정 `/etc/doni-live.env`: MUSTSEE_PASSWORD, GH_TOKEN(doni-must-see Contents 쓰기), TG_TOKEN, TG_CHAT (선택 — Must See 알림 설정에 봇이 있으면 그쪽을 씀)
- 서버에서 로그 보기: `journalctl -u doni-live -n 50`
