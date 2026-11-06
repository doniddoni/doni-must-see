#!/bin/bash
# 1시간마다: agent 브랜치에 새 코드가 있으면 받아서 다시 시작
cd /opt/doni-live/code || exit 0
old=$(git rev-parse HEAD)
sudo -u doni git pull -q --ff-only 2>/dev/null || git pull -q --ff-only || exit 0
new=$(git rev-parse HEAD)
if [ "$old" != "$new" ]; then
  /opt/doni-live/venv/bin/pip install -q -r requirements.txt
  chown -R doni:doni /opt/doni-live/code
  systemctl restart doni-live.service
  echo "$(date) 업데이트 $old → $new"
fi
