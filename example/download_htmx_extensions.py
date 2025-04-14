#!/usr/bin/env python
"""
Download the htmx version and the extensions we're using.

This is only intended for maintaining the example app.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ext_dir = Path(__file__).parent.resolve() / "example/static/ext"


def main() -> int:
    # Per: https://github.com/bigskysoftware/htmx-extensions/tree/main/src/event-header
    download_file(
        "https://unpkg.com/htmx-ext-event-header/event-header.js",
        ext_dir / "event-header.js",
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
