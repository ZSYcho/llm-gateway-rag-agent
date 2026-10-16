#!/usr/bin/env bash
# 构建 new-api：前端（bun）→ go:embed → Linux 静态二进制，供 deploy/new-api/Dockerfile 打包
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/dev-env.sh"
cd "$ROOT/repos/new-api/web"
bun install --frozen-lockfile
DISABLE_ESLINT_PLUGIN=true VITE_REACT_APP_VERSION="$(cat ../VERSION)" bun run build
cd "$ROOT/repos/new-api"
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOWORK=off \
  go build -ldflags "-s -w -X 'github.com/QuantumNous/new-api/common.Version=$(cat VERSION)'" \
  -o "$ROOT/deploy/new-api/new-api" .
echo "built: deploy/new-api/new-api"
