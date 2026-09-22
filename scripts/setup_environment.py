"""Create one isolated, pinned runtime environment without starting SC2 or models."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("macro", "micro"))
    parser.add_argument("--video", action="store_true", help="Also install optional replay rendering dependencies")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 10):
        parser.error("Use Python 3.10, the tested version for these pinned SC2 backends.")
    directory = ROOT / ".venvs" / args.component
    if not (directory / "pyvenv.cfg").exists():
        venv.EnvBuilder(with_pip=True).create(directory)
    binary = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(binary), "-m", "pip", "install", "--disable-pip-version-check", "-r",
                    str(ROOT / args.component / "requirements.txt")], check=True)
    if args.video:
        subprocess.run([str(binary), "-m", "pip", "install", "--disable-pip-version-check", "-r",
                        str(ROOT / "requirements-video.txt")], check=True)
    print(f"Ready: {directory}. Run python jev_star.py {args.component} --help")


if __name__ == "__main__":
    main()
