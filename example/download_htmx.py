#!/usr/bin/env python
"""
Download the given htmx version and the extensions we're using.
"""
import argparse
import subprocess


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="e.g. 1.0.1")
    args = parser.parse_args(args)
    version = args.version

    download_file(version, "htmx.min.js")
    download_file(version, "ext/debug.js")
    download_file(version, "ext/event-header.js")

    print("✅")


def download_file(version, name):
    print(f"{name}...")
    subprocess.run(
        [
            "curl",
            "--silent",
            "--location",
            f"https://unpkg.com/htmx.org@{version}/dist/{name}",
            "-o",
            f"example/static/{name}",
        ],
        check=True,
    )
    # Fix lack of trailing newline in minified file as otherwise pre-commit has
    # to fix it.
    if name.endswith(".min.js"):
        with open(f"example/static/{name}", "a") as fp:
            fp.write("\n")


if __name__ == "__main__":
    main()
