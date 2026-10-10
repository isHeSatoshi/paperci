"""Drop shared snapshot+triage rows so papers are re-triaged with the current gate.
Sealed claims are kept (they do not depend on triage)."""
import sqlite3
from pathlib import Path

c = sqlite3.connect(Path(__file__).resolve().parent.parent / "data" / "paperci.sqlite")
n = c.execute("DELETE FROM stages WHERE run_label='shared' AND stage IN ('snapshot','triage')").rowcount
c.execute("UPDATE papers SET status='new' WHERE status='prepared'")
c.commit()
print("deleted", n, "rows")
