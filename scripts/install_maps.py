"""Install the shipped maps without overwriting a different existing map."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("macro", "micro", "all"))
    parser.add_argument("--sc2-path", type=Path, default=Path(os.environ["SC2PATH"]) if os.environ.get("SC2PATH") else None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.sc2_path is None or not args.sc2_path.is_dir():
        parser.error("Set SC2PATH or supply --sc2-path pointing to the installed StarCraft II directory.")
    components = ("macro", "micro") if args.component == "all" else (args.component,)
    entries = []
    for component in components:
        folder = ROOT / component / ("Maps" if component == "macro" else "SMAC_HARD_maps")
        target = args.sc2_path.resolve() / "Maps"
        if component == "micro":
            target /= "new_maps"
        for source in sorted(folder.glob("*.SC2Map")):
            destination = target / source.name
            assert destination.resolve().is_relative_to(args.sc2_path.resolve())
            exists = destination.exists()
            if exists and source.read_bytes() != destination.read_bytes():
                parser.error(f"Existing map differs; left unchanged: {destination}")
            entries.append((source, destination, exists))
    for source, destination, exists in entries:
        if not args.dry_run and not exists:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        print(f"{'present' if exists else 'would install' if args.dry_run else 'installed'}: {destination.name}")
    print(f"Checked {len(entries)} maps.")


if __name__ == "__main__":
    main()
