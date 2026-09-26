"""Add settings that exist in .env.example but are missing from .env. Never overwrites.

    make env-update            # append missing settings (defaults from .env.example)
    make env-update CHECK=1    # only report what's missing (used by `make doctor`)

Only uncommented `NAME=value` lines of .env.example are considered. Values are the
example's (non-secret defaults or empty), so secrets stay yours to fill in.
"""

import re
import sys
import time
from pathlib import Path

LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def keys(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if m := LINE.match(raw.strip()):
            found.setdefault(m[1], m[2])
    return found


def main() -> int:
    example, env = Path(".env.example"), Path(".env")
    if not env.exists():
        print(".env not found: run `make env` first")
        return 1
    missing = {k: v for k, v in keys(example).items() if k not in keys(env)}
    if "--check" in sys.argv:
        if missing:
            print(f"⚠️  .env lacks {len(missing)} setting(s) from .env.example "
                  f"(e.g. {', '.join(list(missing)[:4])}): run make env-update")  # fmt: skip
        else:
            print("✅ .env has every setting in .env.example")
        return 0
    if not missing:
        print(".env already has every setting in .env.example")
        return 0
    with env.open("a", encoding="utf-8") as f:
        f.write(f"\n# ---- added by make env-update on {time.strftime('%Y-%m-%d')} "
                "(defaults from .env.example; fill in what you need)\n")  # fmt: skip
        for k, v in missing.items():
            f.write(f"{k}={v}\n")
    env.chmod(0o600)
    print(f"added {len(missing)} setting(s) to .env: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
