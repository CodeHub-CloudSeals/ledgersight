"""Back up a LedgerSight data folder: database (online, consistent) + uploaded files.

    python -m ledgersight.backup <destination-folder>

Uses AVOCET_DATA_DIR (default .ledgersight-data). The signing key is NOT copied: keep it in the secret
store (AVOCET_SIGNING_KEY_PEM). Restore = stop the app, copy the folder back as AVOCET_DATA_DIR, start.
"""
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core.store import Store


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Path(os.environ.get("AVOCET_DATA_DIR", ".ledgersight-data"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(argv[1]) / f"ledgersight-backup-{stamp}"
    Store(src / "ledgersight.db").backup(dest / "ledgersight.db")
    for p in src.iterdir():
        if p.is_dir():
            shutil.copytree(p, dest / p.name)
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
