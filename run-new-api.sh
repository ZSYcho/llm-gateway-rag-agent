#!/usr/bin/env bash
# 在 Windows 原生运行 new-api；postgres/redis 在 infra/docker-compose.yml
cd "$(dirname "$0")/runtime/new-api"
export SQL_DSN="postgresql://root:devpass123@localhost:5432/new-api"
export REDIS_CONN_STRING="redis://:devpass123@localhost:6379"
export SESSION_SECRET="local-dev-session-secret-change-me"
export TZ=Asia/Singapore
export ERROR_LOG_ENABLED=true
export BATCH_UPDATE_ENABLED=true
export PORT=3000
exec ../../bin/new-api.exe --log-dir ./logs
