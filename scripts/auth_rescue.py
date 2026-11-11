"""Emergency sign-in when email delivery is down.

Run ON THE SERVER as the isdash user. Prints a one-time sign-in code for an
existing, non-disabled account. Order matters: on the login page enter the
email and click "Email me a code" FIRST (that opens the code box), THEN run
this and type the code it prints — each new code retires the one before it.

  .venv/bin/python scripts/auth_rescue.py dallin@brookspest.com

The code lasts ten minutes and works once, like an emailed one.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash import auth  # noqa: E402
from isadash.config import load_settings  # noqa: E402
from isadash.store import connect  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    email = auth.norm_email(sys.argv[1])
    conn = connect(load_settings().db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE email = ? AND status != 'disabled'", (email,)).fetchone()
        if not row:
            raise SystemExit(f"No active account for {sys.argv[1]!r}.")
        code = auth.new_code(conn, email)
        conn.execute("INSERT INTO auth_log (at, event, email, ip, detail) "
                     "VALUES (strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), 'rescue_code', ?, NULL, 'server console')",
                     (email,))
        conn.commit()
    finally:
        conn.close()
    print(f"Sign-in code for {email}: {code}  (valid {auth.CODE_TTL_MIN} minutes, one use)")


if __name__ == "__main__":
    main()
