#!/usr/bin/env python
"""
Download the htmx version and the extensions we're using.

This is only intended for maintaining the example app.
"""
from __future__ import annotations

import subprocess


def main() -> int:
    # Per: https://htmx.org/docs/#installing
    # Note using @ to pin version because htmx hasn’t made v2 the “latest” at
    # time of writing.
    download_file(
        "https://unpkg.com/htmx.org@2.0.1/dist/htmx.js",
        "htmx.js",
    )
    # Per: https://github.com/bigskysoftware/htmx-extensions/tree/main/src/debug
    download_file(
        "https://unpkg.com/htmx-ext-debug/debug.js",
        "ext/debug.js",
    )
    # Per: https://github.com/bigskysoftware/htmx-extensions/tree/main/src/event-header
    download_file(
        "https://unpkg.com/htmx-ext-event-header/event-header.js",
        "ext/event-header.js",
    )

    print("✅")
    return 0


def download_file(url: str, name: str) -> None:
    print(f"{name}...")
    proc = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            url,
            "-o",
            f"example/static/{name}",
        ],
    )
    if proc.returncode != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
