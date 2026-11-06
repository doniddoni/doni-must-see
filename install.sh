#!/bin/bash
# Doni Live 설치 (오라클 서버 처음 만들 때 초기화 스크립트에서 root 로 실행)
# 미리 /etc/doni-live.env 에 MUSTSEE_PASSWORD, GH_TOKEN, TG_TOKEN, TG_CHAT 를 적어 둔다.
set -uo pipefail
exec > >(tee -a /var/log/doni-live-install.log) 2>&1
echo "== $(date) 설치 시작"
export DEBIAN_FRONTEND=noninteractive
# 메모리 1GB 서버면 스왑 2GB (pip 설치·pandas 용)
if [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 2000000 ] && [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
for i in 1 2 3; do apt-get update -y && apt-get install -y python3-venv git && break; sleep 20; done
id doni >/dev/null 2>&1 || useradd -r -m -d /opt/doni-live -s /usr/sbin/nologin doni
mkdir -p /opt/doni-live
if [ -d /opt/doni-live/code/.git ]; then git -C /opt/doni-live/code pull -q; else git clone -q -b agent --depth 1 https://github.com/doniddoni/doni-must-see.git /opt/doni-live/code; fi
python3 -m venv /opt/doni-live/venv
/opt/doni-live/venv/bin/pip install -q --upgrade pip
/opt/doni-live/venv/bin/pip install -q -r /opt/doni-live/code/requirements.txt
chown -R doni:doni /opt/doni-live
chown doni:doni /etc/doni-live.env && chmod 600 /etc/doni-live.env
cat > /etc/systemd/system/doni-live.service <<'UNIT'
[Unit]
Description=Doni Live 실시간 시장 수집
After=network-online.target
Wants=network-online.target
[Service]
User=doni
WorkingDirectory=/opt/doni-live
ExecStart=/opt/doni-live/venv/bin/python -u /opt/doni-live/code/agent.py
Restart=always
RestartSec=60
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/doni-live-update.service <<'UNIT'
[Unit]
Description=Doni Live 코드 자동 업데이트
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/doni-live/code/update.sh
UNIT
cat > /etc/systemd/system/doni-live-update.timer <<'UNIT'
[Unit]
Description=Doni Live 코드 자동 업데이트 (1시간마다)
[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now doni-live.service doni-live-update.timer
echo "== $(date) 설치 끝"
