#!/bin/sh
# 本机 FRP/Caddy 部署：显式覆盖旧 .env 中较小的并发配置，不修改密钥文件。
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root/backend"

export LLM_MAX_CONCURRENCY=50 RERANK_REMOTE_MAX_CONCURRENCY=50
export MODEL_IO_WORKERS=50 MODEL_IO_QUEUE_SIZE=50
export RATE_LIMIT_IO_WORKERS=50 RATE_LIMIT_IO_MAX_IN_FLIGHT=50
export RATE_LIMIT_PER_MIN=50
# 隧道未提供可验证的访客 IP；保留会话限流，避免所有访客共用回环 IP 的额度。
export RATE_LIMIT_IP_ENABLED=false
export TRUST_PROXY_HEADERS=false ALLOW_TRACE=false DEBUG=false
exec .venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010 --no-proxy-headers
