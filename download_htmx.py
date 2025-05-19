#!/usr/bin/env python
"""
Download htmx to django_htmx/static/htmx.min.js.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

static_dir = Path(__file__).parent.resolve() / "src/django_htmx/static/django_htmx/"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="The version of htmx to download, e.g. 2.0.4")
    args = parser.parse_args()
    # Per: https://htmx.org/docs/#installing
    download_file(
        f"https://unpkg.com/htmx.org@{args.version}/dist/htmx.js",
        static_dir / "htmx.js",
    )
    download_file(
        f"https://unpkg.com/htmx.org@{args.version}/dist/htmx.min.js",
        static_dir / "htmx.min.js",
    )
    print("✅")
    return 0


def download_file(url: str, destination: Path) -> None:
    print(f"{destination.name}...")
    subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            url,
            "-o",
            str(destination),
        ],
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
