"""Packaging regression tests for the deployable wheel.

Render installs the project with ``uv sync --no-editable``, which builds and
installs a **wheel**. Hatchling only packages git-tracked files, so a runtime
module that is untracked -- for example because an over-broad ``.gitignore``
rule hides it -- silently disappears from the deployed artifact::

    ModuleNotFoundError: No module named 'visionops.models'

This test builds the wheel the same way the image does and imports every runtime
module from *inside the extracted wheel*, with the checkout's editable source
off the path, so a missing package fails loudly instead of passing locally.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Alembic runs ``env.py`` and the version scripts with a ``Config`` in scope; they
# are not imported directly by the runtime, so importing them here would be a
# false failure. Every other module in the wheel must import cleanly.
_SKIP_FILES = {"visionops/db/migrations/env.py"}
_SKIP_PREFIXES = ("visionops/db/migrations/versions/",)


def _subprocess_env() -> dict[str, str]:
    """Environment for child processes, with coverage auto-start disabled.

    The project venv installs ``pytest-cov``'s ``.pth`` hook, which starts
    coverage in any child interpreter when ``COV_CORE_*`` is set. A child that
    records statement coverage while the parent records branch coverage makes
    ``coverage combine`` abort, so strip those variables for our subprocesses.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE", "COVERAGE_PROCESS"))
    }


def _build_wheel(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = sorted(out_dir.glob("visionops-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    return wheels[0]


def _runtime_modules(names: list[str]) -> list[str]:
    modules: list[str] = []
    for name in names:
        if not name.startswith("visionops/") or not name.endswith(".py"):
            continue
        if name in _SKIP_FILES or name.startswith(_SKIP_PREFIXES):
            continue
        module = name[: -len(".py")].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        modules.append(module)
    return sorted(modules)


def test_wheel_ships_and_imports_every_runtime_module(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path / "dist")

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        # The package that regressed; asserted by path for a self-explanatory diff.
        assert "visionops/models/__init__.py" in names
        assert "visionops/models/entities.py" in names
        archive.extractall(tmp_path / "site")

    site = tmp_path / "site"
    modules = _runtime_modules(names)

    code = (
        "import importlib, sys\n"
        "from pathlib import Path\n"
        "expected = Path(sys.argv[1]).resolve()\n"
        "for name in sys.argv[2:]:\n"
        "    importlib.import_module(name)\n"
        "import visionops\n"
        "resolved = Path(visionops.__file__).resolve()\n"
        "assert expected in resolved.parents, (\n"
        "    f'imported {resolved}, not the wheel extracted at {expected}'\n"
        ")\n"
        "print(f'imported {len(sys.argv) - 2} modules from {resolved.parent}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(site), *modules],
        cwd=tmp_path,
        env={**_subprocess_env(), "PYTHONPATH": str(site)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"importing the wheel failed:\n{result.stderr}"
