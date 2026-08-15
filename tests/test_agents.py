"""agents.py — the table of products, and how this machine is searched for them.

Every test here isolates both detection signals: PATH is repointed at an empty
directory and HOME at a temporary tree. Without that, the result would depend on
what the person running the suite happens to have installed, and the suite would
say something different on CI than on a developer's laptop.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from manage_precommit import agents


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home directory with no agent in it."""
    d = tmp_path / "home"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH with no executables on it, for every test in this file."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    # PATHEXT decides what counts as executable on Windows. Pinning it keeps
    # `put_binary` and `shutil.which` agreeing about that on every runner.
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return bindir


def put_binary(bindir: Path, name: str) -> Path:
    """A file `shutil.which` will find, spelled the way the platform requires.

    On Windows an extensionless file is not executable however its permissions
    read; `which` consults PATHEXT. A launcher installed there really is
    `claude.cmd` or `codex.exe`, so this is what the fixture must create.
    """
    exe = bindir / (f"{name}.cmd" if os.name == "nt" else name)
    exe.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


class TestTheTable:
    def test_every_agent_has_a_distinct_key(self):
        keys = [agent.key for agent in agents.AGENTS]
        assert sorted(keys) == sorted(set(keys))
        assert set(agents.BY_KEY) == set(keys)

    def test_the_three_supported_products_are_present(self):
        assert set(agents.BY_KEY) == {"claude", "codex", "copilot"}

    def test_skills_directories_are_the_documented_ones(self, home):
        where = {agent.key: agent.skills_path(home) for agent in agents.AGENTS}
        assert where["claude"] == home / ".claude" / "skills"
        # Codex scans no other user-scope directory, and Copilot reads this one
        # too -- which is why both point here rather than at ~/.copilot/skills.
        assert where["codex"] == home / ".agents" / "skills"
        assert where["copilot"] == home / ".agents" / "skills"


class TestDetection:
    def test_an_empty_machine_finds_nothing(self, home):
        assert [d for d in agents.detect_all(home=home) if d.found] == []

    def test_a_binary_on_path_is_enough(self, home, empty_path):
        put_binary(empty_path, "codex")
        found = {d.agent.key for d in agents.detect_all(home=home) if d.found}
        assert found == {"codex"}

    def test_a_configuration_directory_is_enough(self, home):
        """An IDE or extension install may never put a launcher on PATH."""
        (home / ".claude").mkdir()
        found = {d.agent.key for d in agents.detect_all(home=home) if d.found}
        assert found == {"claude"}

    def test_a_configuration_file_is_not_a_directory(self, home):
        """`~/.claude` as a file is not an install; is_dir must do the deciding."""
        (home / ".claude").write_text("not a directory\n", encoding="utf-8")
        assert not agents.detect(agents.BY_KEY["claude"], home=home).found

    def test_the_evidence_names_the_signal_that_fired(self, home, empty_path):
        exe = put_binary(empty_path, "copilot")
        on_path = agents.detect(agents.BY_KEY["copilot"], home=home)
        assert on_path.evidence is not None
        # Case-insensitively: `shutil.which` reports the extension as PATHEXT
        # spells it, which is upper case on Windows, while the file this fixture
        # created is `copilot.cmd`. The same path, written two ways.
        assert str(exe).lower() in on_path.evidence.lower()

        (home / ".codex").mkdir()
        by_config = agents.detect(agents.BY_KEY["codex"], home=home)
        assert by_config.evidence is not None
        assert str(home / ".codex") in by_config.evidence

    def test_path_wins_over_the_configuration_directory(self, home, empty_path):
        """Both signals present: the stronger one is the one reported."""
        put_binary(empty_path, "claude")
        (home / ".claude").mkdir()
        evidence = agents.detect(agents.BY_KEY["claude"], home=home).evidence
        assert evidence is not None and "on PATH" in evidence


class TestTargets:
    def test_agents_sharing_a_directory_collapse_into_one_target(self, home):
        """Codex and Copilot read the same directory.

        Installing once for both is the point: two links of the same name would
        leave Copilot -- which reads both directories -- listing the skill
        twice.
        """
        both = [agents.BY_KEY["codex"], agents.BY_KEY["copilot"]]
        targets = agents.targets_for(both, home=home)
        assert len(targets) == 1
        assert targets[0].path == home / ".agents" / "skills"
        assert [a.key for a in targets[0].agents] == ["codex", "copilot"]

    def test_a_shared_target_names_every_agent_that_reads_it(self, home):
        both = [agents.BY_KEY["codex"], agents.BY_KEY["copilot"]]
        label = agents.targets_for(both, home=home)[0].label
        assert "Codex" in label and "Copilot" in label

    def test_separate_directories_stay_separate(self, home):
        targets = agents.targets_for(agents.AGENTS, home=home)
        assert [t.path for t in targets] == [
            home / ".claude" / "skills",
            home / ".agents" / "skills",
        ]

    def test_no_agents_means_no_targets(self, home):
        assert agents.targets_for([], home=home) == []

    def test_a_bare_directory_target_labels_itself_by_path(self, home):
        """`--dest` builds one of these; it has no agent to name."""
        target = agents.Target(home / "elsewhere", ())
        assert target.label == str(home / "elsewhere")
        assert target.reload_hints == []

    def test_all_targets_does_not_depend_on_what_is_installed(self, home):
        """`uninstall` sweeps these: a link outlives the product that read it."""
        assert agents.all_targets(home=home) == agents.targets_for(agents.AGENTS, home=home)

    def test_every_target_carries_a_reload_hint(self, home):
        for target in agents.all_targets(home=home):
            assert target.reload_hints
            assert all(hint for hint in target.reload_hints)


class TestDefaultsToTheRealHome:
    """The `home` argument exists for the suite; omitting it must still work."""

    def test_detect_all_without_a_home_argument(self):
        assert len(agents.detect_all()) == len(agents.AGENTS)

    def test_all_targets_without_a_home_argument(self):
        assert all(t.path.is_absolute() for t in agents.all_targets())

    def test_detect_without_a_home_argument(self):
        assert agents.detect(agents.BY_KEY["claude"]).agent.key == "claude"
