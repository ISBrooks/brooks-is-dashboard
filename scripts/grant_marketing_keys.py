"""ONE-OFF (2026-10-08): the Sources tab became Marketing with four new
sub-tabs and permission keys (marketing.volume / forms / bysource / spend).
Give those keys to every user who already holds "sources", so nobody who
could see Sources yesterday loses the tab or their admin standing today.

    python scripts/grant_marketing_keys.py            # dry run
    python scripts/grant_marketing_keys.py --write
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash import auth                      # noqa: E402
from isadash.config import load_settings      # noqa: E402
from isadash.store import connect             # noqa: E402

NEW = [k for k in auth.ALL_KEYS if k.startswith("marketing.")]


def main(write: bool) -> None:
    conn = connect(load_settings().db_path)
    try:
        for r in conn.execute("SELECT id, email, permissions FROM users"):
            try:
                perms = json.loads(r["permissions"] or "[]")
            except ValueError:
                perms = []
            if "*" in perms or "sources" not in perms:
                continue
            add = [k for k in NEW if k not in perms]
            if not add:
                continue
            print(f"{r['email']}: + {', '.join(add)}")
            if write:
                conn.execute("UPDATE users SET permissions = ? WHERE id = ?",
                             (json.dumps(perms + add), r["id"]))
        if write:
            conn.commit()
            auth.forget_cached_sessions()
            print("written")
        else:
            print("dry run — add --write to apply")
    finally:
        conn.close()


if __name__ == "__main__":
    main("--write" in sys.argv)
