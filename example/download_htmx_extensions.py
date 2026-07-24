#!/usr/bin/env python
"""
Download the htmx extensions we're using.

This is only intended for maintaining the example app.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Keep in sync with the vendored htmx 4 version in django_htmx/static/django_htmx/.
HTMX4_VERSION = "4.0.0-beta5"

ext_dir = Path(__file__).parent.resolve() / "example/static/ext"


def main() -> int:
    download_file(
        f"https://unpkg.com/htmx.org@{HTMX4_VERSION}/dist/ext/hx-prompt.js",
        ext_dir / "hx-prompt.js",
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
    )


if __name__ == "__main__":
    raise SystemExit(main())
