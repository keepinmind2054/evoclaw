"""Safe .env file reader — does not pollute os.environ"""
import logging
import re
from pathlib import Path

_ENV_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$")

# p12b fix: resolve .env relative to this file's parent (project root), not CWD.
# Previously Path(".env") depended on the working directory at runtime — if the user
# ran `python run.py` from a different directory the file would silently not be found.
_ENV_PATH = Path(__file__).parent.parent / ".env"

def read_env_file(keys: list[str], env_path: Path | None = None) -> dict[str, str]:
    """Read specific keys from .env without setting them in process environment.

    Uses the project-root .env by default (resolved relative to this module's
    location, not the caller's CWD).  Pass env_path to override for tests.
    """
    path = env_path if env_path is not None else _ENV_PATH
    if not path.exists():
        return {}
    result = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
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
            else:
                # BUG-ENV-01 FIX: strip inline comments from unquoted values.
                # A line like KEY=myvalue # comment sets KEY to
                # "myvalue # comment" instead of "myvalue".  Only strip when
                # the value is NOT quoted — quoted values may legitimately
                # contain " # " characters that must be preserved.
                #
                # Two comment forms are handled:
                #   KEY=value # comment   -> "value"  (space before #)
                #   KEY=  # comment       -> ""        (value is only a comment;
                #                                       regex strips leading spaces
                #                                       so val starts with "#")
                # Note: KEY=value#tag is NOT treated as a comment because there
                # is no space before the "#", matching common .env conventions.
                if val.startswith("#"):
                    val = ""
                else:
                    comment_idx = val.find(" #")
                    if comment_idx != -1:
                        val = val[:comment_idx].rstrip()
            result[key] = val
    except Exception as exc:
        # p12b fix: log instead of silently swallowing — helps diagnose permission
        # or encoding problems with the .env file.
        logging.getLogger(__name__).warning("Failed to read .env at %s: %s", path, exc)
    return result
