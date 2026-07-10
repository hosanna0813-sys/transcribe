#!/bin/sh
# 啟動 PO Token 產生器(bgutil)並監管:程序死亡自動重啟(退避 3 秒),
# /readyz 以 4416 埠 ping 回報其存活;Web 主程序由 exec uvicorn 接手 PID 1 訊號。
set -eu

(
  while true; do
    node /opt/bgutil/server/build/main.js || true
    echo "[start.sh] PO Token provider exited, restarting in 3s..." >&2
    sleep 3
  done
) &

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}"
