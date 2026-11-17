"""Run the dashboard locally on MADE-UP data:

    python scripts/serve_sample.py     ->  http://127.0.0.1:8378

For trying a change in a browser without touching anything real: the database
is tests/sample_data.py's invented customers, agents and calls (rebuilt in a
temp folder on every start), there is no sign-in, the real .env is ignored and
the outside services have placeholder credentials, so the feeds that need
Genesys or VoiceOps show their "unavailable" state. Marketing's spend sheet is
a made-up one (tests/sample_data.py), so Spend & ROAS works.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False            # never read the real .env
_cfg = (ROOT / "src" / "isadash" / "config.py").read_text(encoding="utf-8")
for _name in set(re.findall(r'"([A-Z][A-Z0-9_]{3,})"', _cfg)):
    os.environ.pop(_name, None)
os.environ.update({
    "PESTROUTES_BASE_URL": "https://placeholder.invalid",
    "PESTROUTES_AUTH_KEY": "placeholder",
    "GENESYS_CLIENT_ID": "placeholder",
    "GENESYS_INSIDE_SALES_QUEUE_IDS": "placeholder",
    "OUTBOUND_AGENTS": "Robin Vale",
    "MM_DEFAULT_HOURS_AGENTS": "Robin Vale",
    "FORM_DEDUP_WINDOW_HOURS": "336",
    "FORM_CONVERSION_WINDOW_DAYS": "90",
    "FORM_EXCLUDE_SOURCES": "Source 22",
})

import sample_data  # noqa: E402
from isadash import cache, webapp  # noqa: E402

if __name__ == "__main__":
    import uvicorn

    d = Path(tempfile.mkdtemp(prefix="isadash_sample_"))
    db, fl, master = d / "isadash.sqlite", d / "attribution.sqlite", d / "sourcing_master.csv"
    groups = d / "dispute_groups.csv"
    sample_data.build(db, fl, master, groups)
    from isadash import marketing
    marketing._spend_sheet = lambda settings: sample_data.spend_sheet()     # no Google sheet locally
    # changed in place so every module holding the settings object sees it
    for _k, _v in dict(db_path=db, formleads_db=str(fl), sourcing_master=str(master),
                       sourcing_groups=str(groups), app_password="", admin_password="").items():
        object.__setattr__(webapp._settings, _k, _v)
    cache.configure(db)
    print(f"sample database: {db}")
    uvicorn.run(webapp.app, host="127.0.0.1", port=8378)
