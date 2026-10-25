"""Remove untouched intake records by their source name, e.g. Google's own mail that
reached For Review before the intake pre-filter existed (it is now dropped on intake,
see pipeline/intake_filter.py). Dry run unless --apply.

  python3 scripts/purge_records_by_source.py <data_dir>                    # list sources
  python3 scripts/purge_records_by_source.py <data_dir> --google           # dry run: what would go
  python3 scripts/purge_records_by_source.py <data_dir> --google --apply   # back up, then delete
  python3 scripts/purge_records_by_source.py <data_dir> --source "Some Sender" --apply

<data_dir> is server.data_dir from the configuration (stop arta first).

--google matches every source whose name contains "google" (case-insensitive);
--source NAME matches one name exactly (case-insensitive), repeatable. Only
untouched records go: stage For Review or Filtered, not saved, no note, never
applied, no prepared CV set. Anything else is listed as kept. `intake_seen` is
left alone, so a deleted message is not read in again. A copy of the database is
made first (arta.sqlite3.bak-<time>).
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

UNTOUCHED = (
    "stage IN ('for_review', 'filtered') AND saved = 0 AND notes = '' "
    "AND application_status IS NULL AND prepared_cv_set_id IS NULL"
)


def select(conn, google: bool, sources: list[str]):
    """(deletable, kept): rows (id, source, stage, name) of the matching sources."""
    wanted = [s.lower() for s in sources]
    rows = conn.execute(f"SELECT id, source, stage, name, ({UNTOUCHED}) AS untouched FROM opportunities").fetchall()
    hit = [r for r in rows if (google and "google" in r[1].lower()) or r[1].lower() in wanted]
    return [r[:4] for r in hit if r[4]], [r[:4] for r in hit if not r[4]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--google", action="store_true")
    ap.add_argument("--source", action="append", default=[])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    db_path = a.data_dir / "arta.sqlite3"
    if not db_path.is_file():
        print(f"No database at {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    if not (a.google or a.source):
        print("Sources in the database (untouched / all):")
        for src, n, u in conn.execute(
            f"SELECT source, COUNT(*), SUM({UNTOUCHED}) FROM opportunities GROUP BY source ORDER BY 2 DESC"
        ):
            print(f"  {n:4d} / {u or 0:4d}  {src}")
        return 0

    delete, kept = select(conn, a.google, a.source)
    for label, rows in (("DELETE", delete), ("KEEP (touched)", kept)):
        for _id, src, stage, name in rows:
            print(f"{label:15s} {stage:10s} {src[:30]:30s} {name[:60]}")
    print(f"{len(delete)} to delete, {len(kept)} kept.")
    if not a.apply or not delete:
        if delete:
            print("Dry run. Nothing changed. Add --apply to delete.")
        return 0

    backup = db_path.with_name(f"arta.sqlite3.bak-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    dest = sqlite3.connect(backup)
    conn.backup(dest)
    dest.close()
    with conn:
        conn.executemany("DELETE FROM opportunities WHERE id = ?", [(r[0],) for r in delete])
    print(f"Deleted {len(delete)}. Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
