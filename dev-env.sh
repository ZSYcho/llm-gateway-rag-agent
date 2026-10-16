# source ./dev-env.sh —— 本项目的工具路径与缓存目录，全部放 D 盘以节省 C 盘空间
export PATH="/d/tools/bun/bun-windows-x64:$PATH"
export BUN_INSTALL_CACHE_DIR="D:/tools/cache/bun"
export GOMODCACHE="D:/tools/cache/gomod"
export GOCACHE="D:/tools/cache/gobuild"
export npm_config_cache="D:/tools/cache/npm"
export PIP_CACHE_DIR="D:/tools/cache/pip"
export UV_CACHE_DIR="D:/tools/cache/uv"
export PYTHONUTF8=1   # Windows 下 Python 默认 cp1252，读含中文的 .env 会崩
