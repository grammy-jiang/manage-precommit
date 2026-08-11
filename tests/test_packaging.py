"""What actually ends up in the wheel.

The package exists to deliver the skill, so a wheel that builds without the
skill files in it is the one failure that must never ship -- and it is the one
every other gate passes. release.yml opens the artifact too, but that is at tag
time, after check-tag and five test matrices have run. Catching it here catches
it on the pull request instead.

Marked slow-ish by nature: it shells out to `python -m build`, which is a few
seconds. Worth it -- reading pyproject.toml and reasoning about what hatchling
*would* include is exactly the kind of derivation that agrees with itself and
disagrees with the artifact.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = "manage_precommit"

# Everything the skill needs at runtime, by role rather than by listing the
# directory: a file added later should not have to be remembered here, but a
# file that stops shipping must fail.
REQUIRED = [
    f"{PKG}/__init__.py",
    f"{PKG}/cli.py",
    f"{PKG}/skill/SKILL.md",
    f"{PKG}/skill/scripts/shared.py",
    f"{PKG}/skill/scripts/config.py",
    f"{PKG}/skill/scripts/precommit.py",
    f"{PKG}/skill/scripts/gitwork.py",
    f"{PKG}/skill/scripts/summary.py",
    f"{PKG}/skill/scripts/hookoutput.py",
]


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, Path]:
    """One build for the whole module: it is the expensive part."""
    if not (REPO / "pyproject.toml").is_file():
        pytest.skip("no checkout to build from")
    out = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"python -m build failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1, f"unexpected artifacts: {list(out.iterdir())}"
    return {"wheel": wheels[0], "sdist": sdists[0]}


def wheel_names(built) -> set[str]:
    return set(zipfile.ZipFile(built["wheel"]).namelist())


class TestTheWheelCarriesTheSkill:
    def test_every_required_file_is_present(self, built):
        missing = [name for name in REQUIRED if name not in wheel_names(built)]
        assert not missing, f"{built['wheel'].name} is missing: {missing}"

    def test_every_template_ships(self, built):
        """The catalog is these files. A missing one is a key that cannot be
        generated, discovered at the moment somebody selects it."""
        on_disk = {p.name for p in (REPO / "src" / PKG / "skill" / "templates").glob("*.yaml")}
        shipped = {n.rsplit("/", 1)[-1] for n in wheel_names(built) if "/skill/templates/" in n}
        assert on_disk and on_disk == shipped

    def test_every_asset_ships(self, built):
        """Assets are copied INTO the user's repository -- lint-mermaid.mjs is
        the program a hook runs. Absent, the hook is a broken entry."""
        on_disk = {p.name for p in (REPO / "src" / PKG / "skill" / "assets").iterdir()}
        shipped = {n.rsplit("/", 1)[-1] for n in wheel_names(built) if "/skill/assets/" in n}
        assert on_disk and on_disk == shipped

    def test_the_references_ship(self, built):
        """SKILL.md loads these on demand; a missing one is a dead link at the
        moment the agent needs the detail."""
        on_disk = {p.name for p in (REPO / "src" / PKG / "skill" / "references").glob("*.md")}
        shipped = {n.rsplit("/", 1)[-1] for n in wheel_names(built) if "/skill/references/" in n}
        assert on_disk and on_disk == shipped

    def test_the_console_script_is_declared(self, built):
        entry = next(n for n in wheel_names(built) if n.endswith("entry_points.txt"))
        text = zipfile.ZipFile(built["wheel"]).read(entry).decode()
        assert "manage-precommit = manage_precommit.cli:main" in text

    def test_the_licence_ships(self, built):
        assert any(n.endswith("licenses/LICENSE") for n in wheel_names(built))


class TestTheLayoutIsNotRemapped:
    def test_the_installed_tree_matches_the_checkout(self, built):
        """No force-include, no build-time remap: a path in a traceback is
        traceable to this repository by relative position. Checked against the
        artifact rather than against pyproject.toml, because the configuration
        is what would be wrong."""
        for name in REQUIRED:
            assert (REPO / "src" / name).is_file(), f"{name} is not at that path in the checkout"

    def test_no_test_or_tooling_file_leaked_into_the_wheel(self, built):
        """A wheel is what a user installs. Tests, CI and the campaign scratch
        have no business inside it."""
        leaked = [
            n
            for n in wheel_names(built)
            if n.startswith(("tests/", ".github/")) or "/tests/" in n or n.endswith("conftest.py")
        ]
        assert leaked == []


class TestTheSdistIsBuildable:
    def test_it_carries_the_skill_and_the_build_configuration(self, built):
        with tarfile.open(built["sdist"]) as archive:
            names = archive.getnames()
        stripped = {n.split("/", 1)[1] for n in names if "/" in n}
        assert "pyproject.toml" in stripped
        assert f"src/{PKG}/skill/SKILL.md" in stripped
        assert any(n.startswith(f"src/{PKG}/skill/scripts/") for n in stripped)
