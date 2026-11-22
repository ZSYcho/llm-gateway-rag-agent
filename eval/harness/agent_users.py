"""Create agent API keys for the eval identities and the hashed users file.

    python -m eval.harness.agent_users

Raw keys are appended to eval/.env (git-ignored) when missing; the agent
server reads only their SHA-256 hashes from runtime/agent-users.json (see
AGENT_USERS_FILE in the agent's .env.example).
"""

import hashlib
import json
import secrets

from eval.harness.paths import EVAL_ENV, ROOT, read_env

IDENTITIES = {
    "eval-ops": ["ops"],
    "eval-redteam": ["redteam"],
}
USERS_FILE = ROOT / "runtime" / "agent-users.json"


def env_name(identity: str) -> str:
    return "EVAL_AGENT_KEY_" + identity.removeprefix("eval-").upper()


def main() -> None:
    env = read_env()
    new_lines = []
    users = []
    for identity, groups in IDENTITIES.items():
        name = env_name(identity)
        key = env.get(name)
        if not key:
            key = "agk-" + secrets.token_urlsafe(24)
            new_lines.append(f"{name}={key}")
        users.append({
            "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
            "identity": identity,
            "groups": groups,
        })
    if new_lines:
        with EVAL_ENV.open("a", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
    USERS_FILE.parent.mkdir(exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")
    print(f"{len(users)} identities -> {USERS_FILE.relative_to(ROOT)}; new keys: {len(new_lines)}")


if __name__ == "__main__":
    main()
