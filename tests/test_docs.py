from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

base_dir = Path(__file__).resolve().parent.parent
static_dir = base_dir / "src/django_htmx/static/django_htmx"
scripts_rst_path = base_dir / "docs/template_tags.rst"


class TemplateTagsTests(SimpleTestCase):
    def test_htmx_2_versions_match(self):
        htmx_js_version = read_version(
            static_dir / "htmx-2.js",
            r"version: '(\d+\.\d+\.\d+)'",
        )
        htmx_min_js_version = read_version(
            static_dir / "htmx-2.min.js",
            r'version:"(\d+\.\d+\.\d+)"',
        )
        scripts_rst_version = read_version(
            scripts_rst_path,
            r"htmx 2, the default — currently version (\d+\.\d+\.\d+)\.",
        )

        assert htmx_js_version == htmx_min_js_version
        assert htmx_js_version == scripts_rst_version

    def test_htmx_4_versions_match(self):
        htmx_js_version = read_version(
            static_dir / "htmx-4.js",
            r"version = '(\d+\.\d+\.\d+(?:-\w+)?)'",
        )
        htmx_min_js_version = read_version(
            static_dir / "htmx-4.min.js",
            r'version="(\d+\.\d+\.\d+(?:-\w+)?)"',
        )
        scripts_rst_version = read_version(
            scripts_rst_path,
            r"htmx 4, currently in beta — version (\d+\.\d+\.\d+(?:-\w+)?)\.",
        )

        assert htmx_js_version == htmx_min_js_version
        assert htmx_js_version == scripts_rst_version

    def test_htmx_2_extension_versions_match(self):
        script_text = (base_dir / "download_htmx_extensions.py").read_text()
        script_versions = dict(
            re.findall(r'"([a-z-]+)": "(\d+\.\d+\.\d+)"', script_text)
        )
        docs_versions = {
            name: version
            for version, name in re.findall(
                r"`(\d+\.\d+\.\d+) <https://htmx\.org/extensions/([a-z-]+)/>`__",
                scripts_rst_path.read_text(),
            )
        }

        assert script_versions == docs_versions

    def test_htmx_4_extension_version_matches(self):
        htmx_js_version = read_version(
            static_dir / "htmx-4.js",
            r"version = '(\d+\.\d+\.\d+(?:-\w+)?)'",
        )
        script_version = read_version(
            base_dir / "download_htmx_extensions.py",
            r'HTMX_4_VERSION = "(\d+\.\d+\.\d+(?:-\w+)?)"',
        )

        assert htmx_js_version == script_version


def read_version(path: Path, regex: str) -> str:
    content = path.read_text()
    match = re.search(regex, content)
    assert match
    return match[1]
