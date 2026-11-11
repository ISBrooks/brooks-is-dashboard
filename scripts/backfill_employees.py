"""Populate the employees table (fr_employee_id -> name, emp_type) for every
distinct seller in subscriptions, so the Revenue tab can group all office-staff
sellers by person and tell office (type 0) from field/tech (type 2).

  python scripts/backfill_employees.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from isadash.config import load_settings
from isadash.fieldroutes import FieldRoutesClient
from isadash.store import connect


def main() -> None:
    s = load_settings()
    conn = connect(s.db_path)
    ids = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT sold_by FROM subscriptions WHERE sold_by IS NOT NULL AND sold_by != ''")]
    print(f"{len(ids)} distinct sellers to resolve")
    fr = FieldRoutesClient(s.pestroutes_base_url, s.pestroutes_auth_key, s.pestroutes_auth_token)
    rows, done = [], 0
    try:
        for i in range(0, len(ids), 200):
            got = fr.request("employee", "get", {
                "employeeIDs": FieldRoutesClient._id_param(ids[i:i + 200])})
            for e in got.get("employees") or []:
                name = ((e.get("fname") or "") + " " + (e.get("lname") or "")).strip()
                rows.append((str(e.get("employeeID")), name, str(e.get("type"))))
            done += len(got.get("employees") or [])
    finally:
        fr.close()
    conn.executemany(
        "INSERT INTO employees(fr_employee_id, name, emp_type) VALUES(?,?,?) "
        "ON CONFLICT(fr_employee_id) DO UPDATE SET name=excluded.name, emp_type=excluded.emp_type",
        rows)
    conn.commit()
    office = conn.execute("SELECT COUNT(DISTINCT name) FROM employees WHERE emp_type='0'").fetchone()[0]
    conn.close()
    print(f"DONE: {done} employees stored; {office} distinct office-staff (type 0) people")


if __name__ == "__main__":
    main()
