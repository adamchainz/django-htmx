#!/usr/bin/env uv run --script --no-project
"""
Download the vendored htmx extensions to
django_htmx/static/django_htmx/ext/<name>-<major>.js and
<name>-<major>.min.js.

Only extensions with versions for both htmx 2 and htmx 4 are vendored.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# htmx 2 extensions, from their standalone packages:
# https://github.com/bigskysoftware/htmx-extensions
HTMX_2_EXTENSIONS = {
    "head-support": "2.0.5",
    "preload": "2.1.2",
    "sse": "2.2.4",
    "ws": "2.0.4",
}

# htmx 4 extensions are bundled in the htmx.org package.
# Keep in sync with the vendored htmx 4 version in
# django_htmx/static/django_htmx/.
HTMX_4_VERSION = "4.0.0-beta5"
# Map vendored names to file names in the htmx.org package.
HTMX_4_EXTENSIONS = {
    "head-support": "hx-head",
    "preload": "hx-preload",
    "sse": "hx-sse",
    "ws": "hx-ws",
}

ext_dir = Path(__file__).parent.resolve() / "src/django_htmx/static/django_htmx/ext/"


def main() -> int:
    assert HTMX_2_EXTENSIONS.keys() == HTMX_4_EXTENSIONS.keys()
    for name, version in HTMX_2_EXTENSIONS.items():
        for suffix in ("", ".min"):
            download_file(
                f"https://unpkg.com/htmx-ext-{name}@{version}/dist/{name}{suffix}.js",
                ext_dir / f"{name}-2{suffix}.js",
            )
    for name, source in HTMX_4_EXTENSIONS.items():
        for suffix in ("", ".min"):
            download_file(
                f"https://unpkg.com/htmx.org@{HTMX_4_VERSION}/dist/ext/{source}{suffix}.js",
                ext_dir / f"{name}-4{suffix}.js",
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
