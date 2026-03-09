"""Safe .env file reader — does not pollute os.environ"""
import re
from pathlib import Path

_ENV_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$")

def read_env_file(keys: list[str]) -> dict[str, str]:
    """Read specific keys from .env without setting them in process environment."""
    env_path = Path(".env")
    if not env_path.exists():
        return {}
    result = {}
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _ENV_LINE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if key not in keys:
                continue
            # Strip surrounding quotes
            if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            result[key] = val
    except Exception:
        pass
    return result
