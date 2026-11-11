"""Give permission keys to every user who already holds another key.

    python scripts/grant_keys.py --if agent.scorecard --give managers.timesheets,managers.agents --write

Dry run without --write. Users with "*" are skipped (they have everything)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash import auth                      # noqa: E402
from isadash.config import load_settings      # noqa: E402
from isadash.store import connect             # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    cond = args[args.index("--if") + 1]
    give = [k for k in args[args.index("--give") + 1].split(",") if k in auth.ALL_KEYS]
    write = "--write" in args
    conn = connect(load_settings().db_path)
    try:
        for r in conn.execute("SELECT id, email, permissions FROM users WHERE kind = 'staff'"):
            perms = json.loads(r["permissions"] or "[]")
            if "*" in perms or cond not in perms:
                continue
            add = [k for k in give if k not in perms]
            if not add:
                continue
            print(f"{r['email']}: + {', '.join(add)}")
            if write:
                conn.execute("UPDATE users SET permissions = ? WHERE id = ?", (json.dumps(perms + add), r["id"]))
        if write:
            conn.commit()
            auth.forget_cached_sessions()
            print("written")
        else:
            print("dry run — add --write to apply")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
