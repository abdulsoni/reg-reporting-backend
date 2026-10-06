"""Create the demo system databases.

Usage:
    uv run python scripts/seed_databases.py            # create if missing
    uv run python scripts/seed_databases.py --force    # rebuild every system
    uv run python scripts/seed_databases.py --reset    # rebuild + wipe app data
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reg_reporting_back.core.config import settings  # noqa: E402
from reg_reporting_back.core.database import reset_application_data  # noqa: E402
from reg_reporting_back.core.systems import SYSTEMS  # noqa: E402
from reg_reporting_back.modules.seed.service import (  # noqa: E402
    seed_all,
    seeded_systems,
    system_database_path,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild every system database even if it already exists",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="also delete reports, traces and connections from the app database",
    )
    args = parser.parse_args()

    existing = set(seeded_systems())
    missing = [name for name in SYSTEMS if name not in existing]

    if not args.force and not missing:
        print(f"All {len(existing)} demo systems already exist in {settings.data_dir}")
        print("Use --force to rebuild them.")
    else:
        targets = SYSTEMS if args.force else {name: SYSTEMS[name] for name in missing}
        for path in seed_all():
            if path.stem in targets:
                print(f"  created  {path.name}")

    if args.reset:
        reset_application_data()
        print("  cleared  reports, traces and connections")

    print(f"\n{len(seeded_systems())} systems available in {settings.data_dir}:")
    for definition in SYSTEMS.values():
        path = system_database_path(definition)
        mark = "ok" if path.exists() else "missing"
        print(f"  [{mark:>7}] {definition.name:<16} {path.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())