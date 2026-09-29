#!/usr/bin/env bash
# 起一个临时服务跑测试, 跑完收干净。避免后台进程跨命令存活带来的干扰。
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-5099}"
MEDIA="${MEDIA:-/tmp/favtest/media}"
DB="${DB:-/tmp/favtest/fav.db}"
LOG="${LOG:-/tmp/favtest/server.log}"

PY=./.venv/bin/python
mkdir -p "$(dirname "$LOG")"

pkill -f "app.py --videos $MEDIA" 2>/dev/null
sleep 0.6

$PY app.py --videos "$MEDIA" --port "$PORT" --db "$DB" ${SERVER_ARGS:-} > "$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1 && break
  sleep 0.25
done
if ! curl -fsS "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1; then
  echo "服务没起来, 日志:"; cat "$LOG"; exit 1
fi

PORT="$PORT" $PY "${1:-tests/e2e.py}"
RC=$?

# 等服务端把 ffmpeg 和临时分片收干净再返回, 否则下一个测试会被上一轮的
# 残留目录干扰 (实测挂起中的 ffmpeg 收干净要 2~3 秒)
kill $SRV 2>/dev/null
wait $SRV 2>/dev/null
trap - EXIT
exit $RC
