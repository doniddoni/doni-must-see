#!/bin/bash
# 1시간마다: agent 브랜치에 새 코드가 있으면 받아서 다시 시작
# (root 로 실행 — 폴더 주인이 doni 라 git 이 '소유자 다름'으로 거부하지 않게 safe.directory 지정)
cd /opt/doni-live/code || exit 0
G="git -c safe.directory=/opt/doni-live/code"
old=$($G rev-parse HEAD)
sudo -u doni git pull -q --ff-only 2>/dev/null || $G pull -q --ff-only || exit 0
new=$($G rev-parse HEAD)
running=$(cat /opt/doni-live/running_rev 2>/dev/null)
if [ "$old" != "$new" ] || [ "$running" != "$new" ]; then
  /opt/doni-live/venv/bin/pip install -q -r requirements.txt
  chown -R doni:doni /opt/doni-live/code
  systemctl restart doni-live.service
  echo "$new" > /opt/doni-live/running_rev
  echo "$(date) 업데이트 ${running:-$old} → $new"
fi
