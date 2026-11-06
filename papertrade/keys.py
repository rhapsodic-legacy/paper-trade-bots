"""Read local keys (free data-source keys, the Claude Code token) from a file next to the code.

The file is `keys.env`, `api_keys` or `api_keys.txt` in the project folder, one key per line:

    EIA_API_KEY=abc123
    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...   # from `claude setup-token`; lasts a year

Blank lines and lines starting with # are ignored. Keys already set in the environment win.
These files are in .gitignore, so they never reach GitHub.
"""
import os
from pathlib import Path

FILES = ("keys.env", "api_keys", "api_keys.txt")
ROOT = Path(__file__).resolve().parent.parent


def parse(text: str) -> dict[str, str]:
    keys = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if name and value:
            keys[name] = value
    return keys


def load_keys(root: Path = ROOT, environ=os.environ) -> list[str]:
    """Put the keys from the first key files found into the environment. Returns the names loaded."""
    loaded = []
    for name in FILES:
        path = root / name
        if path.is_file():
            for key, value in parse(path.read_text()).items():
                if key not in environ:
                    environ[key] = value
                    loaded.append(key)
    return loaded
