from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase


class TemplateTagsTests(SimpleTestCase):
    def test_htmx_versions_match(self):
        base_dir = Path(__file__).resolve().parent.parent
        htmx_js_path = base_dir / "src/django_htmx/static/django_htmx/htmx.js"
        htmx_min_js_path = base_dir / "src/django_htmx/static/django_htmx/htmx.min.js"
        scripts_rst_path = base_dir / "docs/template_tags.rst"

        htmx_js_version = read_version(
            htmx_js_path,
            r"version: '(\d+\.\d+\.\d+)'",
        )
        htmx_min_js_version = read_version(
            htmx_min_js_path, r'version:"(\d+\.\d+\.\d+)"'
        )
        scripts_rst_version = read_version(
            scripts_rst_path,
            r"The current vendored version of htmx is (\d+\.\d+\.\d+)\.",
        )

        assert htmx_js_version == htmx_min_js_version
        assert htmx_js_version == scripts_rst_version


def read_version(path: Path, regex: str) -> str:
    content = path.read_text()
    match = re.search(regex, content)
    assert match
    return match[1]
