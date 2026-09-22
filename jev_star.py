"""Dispatch JEV-Star commands to the appropriate isolated Python environment."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
COMMANDS = {
    "macro": ("macro", "sc2_rl_agent.starcraftenv_test.run_jev"),
    "macro-report": ("macro", "sc2_rl_agent.starcraftenv_test.run_report"),
    "macro-video": ("macro", "sc2_rl_agent.starcraftenv_test.export_replay_video"),
    "macro-serve": ("macro", "sc2_rl_agent.starcraftenv_test.serve_replays"),
    "micro": ("micro", "jev_micro.run"),
    "micro-suite": ("micro", "jev_micro.suite"),
    "micro-video": ("micro", "jev_micro.replay_video"),
    "micro-realtime": ("micro", "jev_micro.realtime"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Arguments after COMMAND are passed unchanged. Relative data paths are resolved inside macro/ or micro/.")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    component, module = COMMANDS[args.command]
    binary = ROOT / ".venvs" / component / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not binary.is_file():
        parser.error(f"Missing {component} environment. Run: python scripts/setup_environment.py {component}")
    forwarded = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / component)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.call([str(binary), "-X", "utf8", "-B", "-m", module, *forwarded],
                           cwd=ROOT / component, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
