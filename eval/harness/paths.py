"""Repository locations shared by the eval harness."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval"
EVAL_ENV = EVAL_DIR / ".env"
BACKEND_DIR = ROOT / "repos" / "gemini-fullstack-langgraph-quickstart" / "backend"


def read_env(path: Path = EVAL_ENV) -> dict[str, str]:
    """Parse KEY=VALUE lines; values are never printed by the harness."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values
