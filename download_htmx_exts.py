#!/usr/bin/env python

from __future__ import annotations

import subprocess
from pathlib import Path

from django_htmx.jinja import HTMX_EXT_NAMES

_PROJECT_ROOT_PATH = Path(__file__).parent


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


def main() -> int:
    for ext in sorted(HTMX_EXT_NAMES):
        output_filename = (
            _PROJECT_ROOT_PATH
            / "src/django_htmx/static/django_htmx/"
            / f"htmx-ext-{ext}.js"
        )

        download_file(f"https://cdn.jsdelivr.net/npm/htmx-ext-{ext}", output_filename)

        print(f"download htmx ext finished, {output_filename}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
