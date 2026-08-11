"""The shipped skill's metadata is a contract, so it is parsed and checked.

The Agent Skills format (<https://agentskills.io/specification>) is read by more
than one product, and they all ignore fields they do not know -- but the
packaging and upload paths around them do not: a field outside the spec is a
hard error there, not a shrug.

Parsed with a real YAML parser rather than by hand, deliberately. A hand-rolled
reader agrees with whatever this file happens to contain today and disagrees
with the parsers that actually load the skill.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from manage_precommit import cli

try:  # tomllib landed in 3.11; on 3.10 the checks needing it simply do not run
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    tomllib = None

SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def frontmatter() -> dict[str, object]:
    text = (cli.skill_source() / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    end = text.index("\n---\n", 3)
    data = yaml.safe_load(text[4:end])
    assert isinstance(data, dict)
    return data


def skill_files() -> list[Path]:
    root = cli.skill_source()
    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in (".md", ".py")]


class TestFrontmatterFollowsTheSpec:
    def test_it_is_valid_yaml_with_the_required_fields(self):
        assert {"name", "description"} <= set(frontmatter())

    def test_no_field_outside_the_specification(self):
        """A field the spec does not define fails an upload outright, and the
        host-specific ones (`model`, `context`, `argument-hint`) are exactly what
        makes a skill unportable. The cost of adding one should be a failing
        test here, not a bug report from somebody on another host."""
        assert set(frontmatter()) <= SPEC_FIELDS

    def test_the_name_is_a_legal_skill_name(self):
        name = frontmatter()["name"]
        assert isinstance(name, str)
        assert len(name) <= 64
        assert NAME_PATTERN.match(name), "lowercase, digits and inner hyphens only"

    def test_the_name_matches_the_directory_it_is_installed_as(self):
        """A host lists a skill by directory; a mismatch reads as two different
        things depending on where you look."""
        assert frontmatter()["name"] == cli.SKILL_NAME

    def test_the_description_fits_the_limit_and_says_when_to_use_it(self):
        description = frontmatter()["description"]
        assert isinstance(description, str)
        assert 0 < len(description) <= 1024
        # The host matches a skill against this string and nothing else.
        assert "Use when" in description

    def test_compatibility_fits_the_limit(self):
        compatibility = frontmatter().get("compatibility")
        assert isinstance(compatibility, str)
        assert len(compatibility) <= 500

    def test_allowed_tools_is_a_string_not_a_list(self):
        """The spec says a space-separated string.

        Defect this pins: it was a YAML list, which Claude Code accepts and the
        spec does not describe -- the kind of difference that works everywhere
        it is tested and fails on the one host nobody tried.
        """
        assert isinstance(frontmatter()["allowed-tools"], str)

    def test_metadata_is_a_map_of_strings(self):
        metadata = frontmatter().get("metadata")
        assert isinstance(metadata, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())

    def test_metadata_does_not_carry_a_second_copy_of_the_version(self):
        """The packaged version has one home, and it is pyproject.toml."""
        assert "version" not in frontmatter().get("metadata", {})  # type: ignore[operator]


class TestAllowedToolsMatchesWhatTheSkillPermits:
    def test_bash_is_not_pre_approved_wholesale(self):
        """Defect this pins: `allowed-tools` listed bare `Bash`, which grants
        every command there is -- far broader than the four this procedure runs,
        in a skill whose entire subject is not letting the agent improvise."""
        assert "Bash" not in str(frontmatter()["allowed-tools"]).split()

    def test_git_is_not_pre_approved(self):
        """SKILL.md forbids running git directly -- scripts/gitwork.py is the
        only path to a mutation. Pre-approving `git` would hand the agent the
        very thing the procedure exists to keep out of its hands."""
        assert "Bash(git" not in str(frontmatter()["allowed-tools"])

    def test_pre_commit_is_not_pre_approved(self):
        """precommit.py --verify runs it, with the install and the vacuous-pass
        judgement around it. An agent running it directly gets neither."""
        assert "Bash(pre-commit" not in str(frontmatter()["allowed-tools"])

    def test_every_bash_command_the_body_runs_is_granted(self):
        """The grant is narrow, so it has to be complete: a command the
        procedure runs and the frontmatter omits turns a step into a permission
        prompt in the middle of a half-written config."""
        granted = {
            m.group(1)
            for m in re.finditer(r"Bash\((\w[\w-]*):", str(frontmatter()["allowed-tools"]))
        }
        body = (cli.skill_source() / "SKILL.md").read_text(encoding="utf-8")
        run = set()
        for block in re.findall(r"```bash\n(.*?)```", body, re.S):
            for line in block.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith(("#", "-")):
                    run.add(stripped.split()[0])
        assert run <= granted, f"run but not granted: {sorted(run - granted)}"


class TestTheSkillSaysWhatItActuallyNeeds:
    def test_the_python_version_it_claims_is_the_one_the_package_supports(self):
        """A skill claiming a floor above the package's tells the agent to stop
        before doing anything, on a machine that would have worked."""
        if tomllib is None:
            pytest.skip("tomllib needs 3.11+; CI checks this on every later version")
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if not pyproject.is_file():
            pytest.skip("no checkout")
        requires = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
            "requires-python"
        ]
        floor = requires.removeprefix(">=").strip()
        body = (cli.skill_source() / "SKILL.md").read_text(encoding="utf-8")
        claimed = re.findall(r"Python (\d+\.\d+)\+", body)
        assert claimed, "SKILL.md should state the Python version it needs"
        assert set(claimed) == {floor}

    def test_compatibility_names_the_things_that_are_not_python(self):
        """`compatibility` is where a host looks before running a skill at all."""
        compatibility = str(frontmatter()["compatibility"])
        for needed in ("git", "pre-commit", "node"):
            assert needed in compatibility


class TestNothingPointsAtSomethingThatIsGone:
    def test_no_file_calls_a_subcommand_that_no_longer_exists(self):
        """The installer has exactly two commands. Anything else spelled as
        `manage-precommit <word>` is a path that exits 2."""
        # Inside backticks only: the prose says "the manage-precommit skill"
        # constantly, and that is not an invocation.
        stale = re.compile(r"`manage-precommit\s+(?!install\b|uninstall\b|-)(\w+)")
        offenders = [
            f"{path.name}:{i}: {line.strip()[:70]}"
            for path in skill_files()
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if stale.search(line)
        ]
        assert offenders == []

    def test_every_script_the_body_names_exists(self):
        """SKILL.md invokes scripts by path. A rename that misses one turns a
        step into "No such file or directory" halfway through a run."""
        body = (cli.skill_source() / "SKILL.md").read_text(encoding="utf-8")
        named = set(re.findall(r"scripts/(\w+\.py)", body))
        assert named, "SKILL.md should invoke the scripts by path"
        present = {p.name for p in (cli.skill_source() / "scripts").glob("*.py")}
        assert named <= present, f"named but missing: {sorted(named - present)}"
