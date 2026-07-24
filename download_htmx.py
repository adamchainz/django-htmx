#!/usr/bin/env uv run --script --no-project
"""
Download htmx to django_htmx/static/django_htmx/htmx-<major>.js and
htmx-<major>.min.js.
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
    major = args.version.split(".")[0]
    if major not in ("2", "4"):
        parser.error(f"Unsupported htmx major version: {major}")
    # Per: https://htmx.org/docs/#installing
    download_file(
        f"https://unpkg.com/htmx.org@{args.version}/dist/htmx.js",
        static_dir / f"htmx-{major}.js",
    )
    download_file(
        f"https://unpkg.com/htmx.org@{args.version}/dist/htmx.min.js",
        static_dir / f"htmx-{major}.min.js",
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
