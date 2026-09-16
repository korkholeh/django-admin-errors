"""Clean-venv proof that the built wheel is complete (phase 10, T10, spec section 18 / risk #17).

Run by `.tox/package-install/bin/python` — the interpreter of a virtualenv that has *only* the
built `django-admin-errors` wheel and Django installed, nothing else from this repo — as the last
command of `[testenv:package]` in `tox.ini`. Deliberately not named `test_*.py`: `pytest`'s
`testpaths = ["tests"]` would otherwise try to collect it and fail, since it isn't meant to run
under pytest or against the editable/`src`-layout checkout, only against the *installed* package.

Exits 0 with nothing printed on success; exits 1 with a readable message identifying which check
failed otherwise.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.package_settings")


def main() -> int:
    import django

    django.setup()

    import admin_errors

    failures: list[str] = []

    from django.template import TemplateDoesNotExist
    from django.template.loader import get_template

    for template_name in (
        "admin/admin_errors/issue/change_list.html",
        "admin/admin_errors/issue/change_form.html",
        "admin/admin_errors/issue/event_detail.html",
    ):
        try:
            get_template(template_name)
        except TemplateDoesNotExist as exc:
            failures.append(f"template {template_name!r} not found in installed package: {exc}")

    from django.contrib.staticfiles import finders

    for static_path in ("admin_errors/admin_errors.css", "admin_errors/admin_errors.js"):
        if finders.find(static_path) is None:
            failures.append(f"static file {static_path!r} not found in installed package")

    import pathlib

    mo_path = (
        pathlib.Path(admin_errors.__file__).parent / "locale" / "uk" / "LC_MESSAGES" / "django.mo"
    )
    if not mo_path.is_file():
        failures.append(f"uk locale catalogue missing from installed package: {mo_path}")
    else:
        from django.utils import translation
        from django.utils.translation import gettext

        translation.activate("uk")
        try:
            translated = gettext("Resolve")
            if translated == "Resolve":
                failures.append("translation.activate('uk') did not translate a known msgid")
        finally:
            translation.deactivate()

    if failures:
        print("package_smoke: FAILED", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("package_smoke: OK — templates, static files and the uk catalogue all resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
