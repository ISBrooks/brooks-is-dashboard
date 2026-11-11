"""Run the dashboard locally:  python scripts/serve.py  ->  http://127.0.0.1:8377"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn

if __name__ == "__main__":
    uvicorn.run("isadash.webapp:app", host="127.0.0.1", port=8377, reload=False)
