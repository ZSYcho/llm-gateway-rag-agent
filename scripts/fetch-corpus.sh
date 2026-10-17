#!/usr/bin/env bash
# 拉取知识库语料到 runtime/corpus（稀疏检出，只取 Markdown 文档）
#   new-api-docs   QuantumNous/new-api-docs（文档部分 CC0-1.0），中文文档
#   langchain-docs langchain-ai/docs（MIT），src/oss/langgraph
# 之后入库：cd repos/gemini-fullstack-langgraph-quickstart/backend &&
#   .venv/Scripts/python -m agent.kb_ingest --corpus-dir ../../../runtime/corpus
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/runtime/corpus"
mkdir -p "$DEST"

fetch() { # <repo url> <dir> <sparse pattern...>
  local url="$1" dir="$2"; shift 2
  if [ -d "$DEST/$dir/.git" ]; then
    git -C "$DEST/$dir" pull -q --depth 1
  else
    git clone -q --depth 1 --filter=blob:none --sparse "$url" "$DEST/$dir"
    git -C "$DEST/$dir" sparse-checkout set --no-cone "$@"
  fi
  echo "$dir @ $(git -C "$DEST/$dir" rev-parse --short HEAD)"
}

fetch https://github.com/QuantumNous/new-api-docs.git new-api-docs '/docs/**/*.md'
fetch https://github.com/langchain-ai/docs.git langchain-docs '/src/oss/langgraph/**'
