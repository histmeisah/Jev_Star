"""Refresh the active BurnySC2 environment's IDs from the installed game.

Run after launching a newly installed SC2 client once. Older SDK dictionaries
may still import removed ability names, so retain those names as legacy entries.
Existing names that changed numeric IDs follow the current game's stableid.json.
"""

import argparse
import importlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from loguru import logger
from sc2.generate_ids import IdGenerator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stable-id", type=Path)
    parser.add_argument("--game-version", required=True, help="Version reported by the installed SC2 client")
    args = parser.parse_args()
    generator = IdGenerator(game_version=args.game_version)
    stable_id = args.stable_id or Path(generator.DATA_JSON[generator.PF])
    data = json.loads(stable_id.read_text(encoding="utf-8"))
    logger.disable("sc2.generate_ids")
    current = generator.parse_data(data)
    changes = {}
    for category, module_name in generator.FILE_TRANSLATE.items():
        module = importlib.import_module(f"sc2.ids.{module_name}")
        enum = getattr(module, generator.ENUM_TRANSLATE[category])
        previous = {name: value.value for name, value in enum.__members__.items()}
        new = current[category]
        changes[category] = {
            "added": {k: v for k, v in new.items() if k not in previous},
            "changed": {k: [previous[k], v] for k, v in new.items() if k in previous and previous[k] != v},
            "retained_legacy_names": {k: v for k, v in previous.items() if k not in new},
        }
        current[category] = {**previous, **new}

    package_ids = Path(importlib.import_module("sc2.ids").__file__).parent
    workspace = Path(__file__).resolve().parents[1]
    # Limit this maintenance command to the workspace's disposable environments.
    if not package_ids.resolve().is_relative_to(workspace / ".venvs"):
        parser.error("Use a Python interpreter under this workspace's .venvs directory.")
    backup = workspace / ".tools" / ("sc2-ids-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(parents=True, exist_ok=False)
    for source in package_ids.glob("*.py"):
        shutil.copy2(source, backup / source.name)
    manifest = {"game_version": args.game_version, "stable_id_file": str(stable_id),
                "package_ids": str(package_ids), "changes": changes}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    generator.generate_python_code(current)
    print(f"Synced SC2 IDs for {args.game_version}. Previous files: {backup}")
    for category, change in changes.items():
        print(f"{category}: added={len(change['added'])}, changed={len(change['changed'])}, "
              f"retained_legacy_names={len(change['retained_legacy_names'])}")
    print("Start the bot in a new Python process so all imports use the new IDs.")


if __name__ == "__main__":
    main()
