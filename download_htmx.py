#!/usr/bin/env uv run --script --no-project
"""
Download htmx to django_htmx/static/django_htmx/htmx-<major>.js and
htmx-<major>.min.js, plus the vendored extensions for that major version to
django_htmx/static/django_htmx/ext/<name>-<major>.js and
<name>-<major>.min.js.

For htmx 4, the htmax bundle of htmx plus popular extensions is also
downloaded, to django_htmx/static/django_htmx/htmax-4.js and
htmax-4.min.js.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

# Extensions are keyed by their htmx 4 names, as bundled in the htmx.org
# package and downloaded with the same version as htmx itself.
# Values give the standalone htmx 2 package name and version, for those
# extensions that also have htmx 2 versions:
# https://github.com/bigskysoftware/htmx-extensions
EXTENSIONS: dict[str, tuple[str, str] | None] = {
    "htmx-2-compat": None,
    "hx-browser-indicator": None,
    "hx-download": None,
    "hx-head": ("head-support", "2.0.5"),
    "hx-optimistic": None,
    "hx-preload": ("preload", "2.1.2"),
    "hx-prompt": None,
    "hx-ptag": None,
    "hx-sse": ("sse", "2.2.4"),
    "hx-targets": None,
    "hx-upsert": None,
    "hx-ws": ("ws", "2.0.4"),
}

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
    if major == "2":
        for name, htmx_2_source in EXTENSIONS.items():
            if htmx_2_source is None:
                continue
            htmx_2_name, htmx_2_version = htmx_2_source
            for suffix in ("", ".min"):
                download_file(
                    f"https://unpkg.com/htmx-ext-{htmx_2_name}@{htmx_2_version}/dist/{htmx_2_name}{suffix}.js",
                    static_dir / f"ext/{name}-2{suffix}.js",
                )
    else:
        for suffix in ("", ".min"):
            download_file(
                f"https://unpkg.com/htmx.org@{args.version}/dist/htmax{suffix}.js",
                static_dir / f"htmax-4{suffix}.js",
            )
        for name in EXTENSIONS:
            for suffix in ("", ".min"):
                download_file(
                    f"https://unpkg.com/htmx.org@{args.version}/dist/ext/{name}{suffix}.js",
                    static_dir / f"ext/{name}-4{suffix}.js",
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
