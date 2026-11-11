"""Read-only: print what the Sourcing App card's unmapped-numbers section will show.
Run on the droplet as isdash from /home/isdash/brooks-is-dash (the deploy script does this)."""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/isdash/brooks-is-dash/src")
from isadash import software  # noqa: E402
from isadash.config import load_settings  # noqa: E402

u = software.unmapped_calls(load_settings(), datetime.now(timezone.utc))
print(f"last {u['days']} days: {u['answered']} answered calls, {u['calls']} of them to "
      f"{len(u['numbers'])} unmapped numbers (master sheet has {u['master_count']} numbers)"
      + (f" | ERROR: {u['error']}" if u["error"] else ""))
for n in u["numbers"][:8]:
    print(f"  {n['display']:>16}  {n['calls']:5} calls  last {n['last'][:16] if n['last'] else '-'}")
