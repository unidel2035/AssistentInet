#!/bin/bash
# Deploy AssistentInet (Soundmeter) to VPS
# Usage: ./deploy.sh
# From hive: cd /home/hive/soundmeter && ./deploy.sh

VPS="ai-agent@185.233.200.13"
REMOTE_DIR="/home/ai-agent/soundmeter"

echo "=== AssistentInet Deploy ==="

# 1. Push local changes to GitHub
echo "[1/3] Pushing to GitHub..."
git add -A && git diff --quiet && git diff --cached --quiet || git commit -m "Update $(date '+%Y-%m-%d %H:%M')"
git push origin master
echo "      OK"

# 2. Pull on VPS + restart Docker
echo "[2/3] Deploying on VPS..."
ssh "$VPS" "
  cd $REMOTE_DIR &&
  git pull origin master &&
  docker compose down &&
  docker compose up -d --build
"
echo "      OK"

# 3. Check status
echo "[3/3] Checking status..."
sleep 3
ssh "$VPS" "docker ps --filter name=soundmeter --format '  {{.Names}} — {{.Status}}'"
echo ""
echo "=== Done ==="
echo "Web: http://185.233.200.13:8090"
