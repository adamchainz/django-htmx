from __future__ import annotations

import os
from argparse import ArgumentParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.parse import urlunparse
from urllib.request import urlopen

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError


class Command(BaseCommand):
    help = "Executes HTMX-related tasks"

    def add_arguments(self, parser: ArgumentParser) -> None:
        subcommands = parser.add_subparsers(
            title="subcommands", dest="subcommand", required=True
        )

        download = subcommands.add_parser(
            "download", help="download htmx (with extensions)"
        )

        download.add_argument(
            "version",
            nargs="?",
            help="version to download, e.g. '1.0.1. default: latest",
        )
        download.add_argument(
            "--ext",
            "-e",
            action="append",
            type=str,
            help="additional extension to download. Can be specified multiple times",
        )
        download.add_argument(
            "--dest",
            "-d",
            type=Path,
            default=None,
            help="where to download HTMX to. default: the first STATICFILES_DIR",
        )

    def handle(self, *args: str, **options: Any) -> None:
        subcommand = options.pop("subcommand", None)

        if subcommand == "download":
            return self._handle_download(*args, **options)

    def _handle_download(self, *args: str, **options: Any) -> None:
        dest_dir = options["dest"]
        if dest_dir is None:
            if len(settings.STATICFILES_DIRS) == 0:
                raise CommandError(
                    "Destination location not defined. "
                    "Use '--dest' or set STATICFILES_DIRS in your settings"
                )
            dest_dir = Path(settings.STATICFILES_DIRS[0])

        package_spec = "htmx.org"
        if options["version"] is not None:
            package_spec += f"@{options['version']}"

        actual_version = options["version"]
        files = ["htmx.js"]
        if options["ext"] is not None:
            files += [f"ext/{extension}" for extension in options["ext"]]
        for file in files:
            self.stderr.write(self.style.HTTP_INFO(f"Downloading {file}"))
            url = urlunparse(
                (
                    "https",
                    "unpkg.com",
                    f"{package_spec}/dist/{file}",
                    "",
                    "",
                    "",
                )
            )

            with urlopen(url) as response:
                if not actual_version:
                    actual_version = (
                        urlparse(response.geturl()).path.split("/")[1].split("@")[-1]
                    )

                with open(dest_dir / file, "wb") as fp:
                    fp.write(response.read())
                    if file.endswith(".min.js"):
                        fp.write(os.linesep.encode())

        self.stderr.write(self.style.SUCCESS(f"Downloaded htmx v{actual_version}"))
