#!/usr/bin/env bash
set -euo pipefail
# RK3588 native systemd update script
# Run after rsync to update Python packages and restart services

cd /opt/elder-guardian-agent

echo "=== Update guardian-shared ==="
pip3 install -e packages/guardian-shared 2>&1 | tail -3

echo "=== Update apps ==="
for app in guardian-core edge-mcp-server guardian-orchestrator vision-service voice-hmi-service wechat-adapter; do
    echo "--- $app ---"
    pip3 install -e apps/$app 2>&1 | tail -1
done

echo "=== Rebuild frontend ==="
if command -v pnpm &>/dev/null; then
    pnpm install 2>&1 | tail -3
    pnpm --filter web-dashboard build 2>&1 | tail -3
    pnpm --filter elder-hmi build 2>&1 | tail -3
fi

echo "=== Restart services ==="
for svc in guardian-core edge-mcp-server guardian-orchestrator vision-service voice-hmi-service wechat-adapter nginx; do
    systemctl restart $svc 2>/dev/null && echo "  $svc restarted" || echo "  $svc skip"
done

echo "=== Health checks ==="
sleep 3
for port in 8000 8010 8020; do
    status=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$port/health 2>/dev/null || echo "fail")
    echo "  port $port: $status"
done

echo "=== Done ==="
echo "Dashboard: http://192.168.10.64:5173"
echo "HMI:       http://192.168.10.64:5174"
