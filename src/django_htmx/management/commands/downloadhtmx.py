from __future__ import annotations

import os
from argparse import ArgumentParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.parse import urlunparse
from urllib.request import urlopen

from django.conf import settings
from django.core.checks import Tags
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError


class Command(BaseCommand):
    help = "Download the given htmx version and the extensions."
    requires_system_checks = [Tags.staticfiles]

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "version",
            nargs="?",
            help="version to download, e.g. '1.0.1. default: latest",
        )
        parser.add_argument(
            "--dest",
            "-d",
            type=Path,
            default=None,
            help="where to download HTMX to. default: the first STATICFILES_DIR",
        )

    def handle(self, *args: str, **options: Any) -> None:
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
        for file in ["htmx.js", "ext/debug.js", "ext/event-header.js"]:
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
