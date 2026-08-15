"""The installer.

It is the one part of this package that is not the skill, and the only part a
user runs directly, so every refusal it can make is tested here -- a refusal
that does not fire is an installer that deletes somebody's directory.

Nothing here touches the real ~/.claude: every test passes `--dest`, or points
HOME at a tmp_path when the default is what is under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from manage_precommit import __version__, agents, cli

SKILL = Path(__file__).resolve().parents[1] / "src" / "manage_precommit" / "skill"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An empty skills directory to install into."""
    d = tmp_path / "skills"
    d.mkdir()
    return d


def foreign(tmp_path: Path, name: str = "elsewhere") -> Path:
    """A directory that `install` did not create and must never remove."""
    d = tmp_path / name
    d.mkdir()
    return d


# --- what the package ships -------------------------------------------------


def test_the_packaged_skill_is_found_beside_this_module():
    """One path, not a search: checkout and site-packages are the same tree."""
    assert cli.skill_source() == SKILL
    assert (cli.skill_source() / "SKILL.md").is_file()


def test_a_package_without_its_skill_files_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "cli.py"))
    with pytest.raises(FileNotFoundError, match="packaged skill files not found"):
        cli.skill_source()


def test_the_default_destinations_follow_home(tmp_path):
    """Every path is derived from HOME so a test can point it at a tmp tree and
    get the whole table with it."""
    paths = {t.path for t in agents.all_targets(home=tmp_path)}
    assert paths == {tmp_path / ".claude" / "skills", tmp_path / ".agents" / "skills"}


# --- telling our own link from somebody else's ------------------------------


def test_a_plain_directory_is_not_a_link(root):
    (root / "manage-precommit").mkdir()
    assert cli.link_target(root / "manage-precommit") is None
    assert cli.is_our_link(root / "manage-precommit") is False


def test_our_own_link_is_recognised(root):
    dest = cli.install(root, force=False)
    assert cli.link_target(dest) == SKILL
    assert cli.is_our_link(dest) is True


def test_a_relative_link_is_resolved_against_the_link_not_the_cwd(root, tmp_path, monkeypatch):
    """readlink() hands back what was stored, which may be relative -- and a
    relative target means nothing without the link's own directory."""
    (root / "skill").mkdir()
    (root / "skill" / "SKILL.md").write_text("mine\n")
    link = root / "manage-precommit"
    link.symlink_to(Path("skill"))
    monkeypatch.chdir(tmp_path)  # a cwd in which "skill" does not exist
    assert cli.link_target(link) == Path("skill")
    assert cli.is_our_link(link) is True


def test_a_link_to_something_that_is_not_a_skill_is_not_ours(root, tmp_path):
    link = root / "manage-precommit"
    link.symlink_to(foreign(tmp_path))
    assert cli.is_our_link(link) is False


def test_a_directory_named_skill_without_a_SKILL_md_is_not_ours(root, tmp_path):
    """The name alone is not evidence -- the contract is the file."""
    link = root / "manage-precommit"
    link.symlink_to(foreign(tmp_path, "skill"))
    assert cli.is_our_link(link) is False


# --- install ----------------------------------------------------------------


def test_install_links_the_skill(root):
    dest = cli.install(root, force=False)
    assert dest == root / "manage-precommit"
    assert dest.is_symlink()
    assert (dest / "SKILL.md").is_file()


def test_install_creates_the_skills_directory(tmp_path):
    root = tmp_path / "nested" / "skills"
    assert cli.install(root, force=False).is_symlink()


def test_installing_twice_is_idempotent(root):
    first = cli.install(root, force=False)
    second = cli.install(root, force=False)
    assert first == second
    assert second.is_symlink()


def test_install_refuses_a_link_to_something_else(root, tmp_path):
    (root / "manage-precommit").symlink_to(foreign(tmp_path))
    with pytest.raises(FileExistsError, match="symlink to something else"):
        cli.install(root, force=False)


def test_install_replaces_a_foreign_link_when_forced(root, tmp_path):
    (root / "manage-precommit").symlink_to(foreign(tmp_path))
    assert cli.install(root, force=True).resolve() == SKILL
    assert (tmp_path / "elsewhere").is_dir(), "the link's target was removed"


def test_install_refuses_a_real_directory(root):
    (root / "manage-precommit").mkdir()
    with pytest.raises(FileExistsError, match="already exists and is not a symlink"):
        cli.install(root, force=False)


def test_install_replaces_a_real_directory_only_when_forced(root):
    hand_written = root / "manage-precommit"
    hand_written.mkdir()
    (hand_written / "SKILL.md").write_text("mine\n")
    assert cli.install(root, force=True).is_symlink()


# --- uninstall --------------------------------------------------------------


def test_uninstall_removes_our_link_and_nothing_else(root):
    cli.install(root, force=False)
    assert cli.uninstall(root, force=False) == root / "manage-precommit"
    assert not (root / "manage-precommit").exists()
    assert (SKILL / "SKILL.md").is_file(), "the link's target was removed"


def test_uninstall_of_nothing_is_not_an_error(root):
    assert cli.uninstall(root, force=False) is None


def test_uninstall_refuses_a_real_directory(root):
    (root / "manage-precommit").mkdir()
    with pytest.raises(FileExistsError, match="is a directory, not a symlink"):
        cli.uninstall(root, force=False)


def test_uninstall_refuses_a_foreign_link(root, tmp_path):
    (root / "manage-precommit").symlink_to(foreign(tmp_path))
    with pytest.raises(FileExistsError, match="which is not a packaged skill"):
        cli.uninstall(root, force=False)


def test_uninstall_removes_a_foreign_link_when_forced(root, tmp_path):
    (root / "manage-precommit").symlink_to(foreign(tmp_path))
    assert cli.uninstall(root, force=True) is not None
    assert (tmp_path / "elsewhere").is_dir(), "the link's target was removed"


# --- the two commands -------------------------------------------------------


def test_install_command_reports_what_it_linked(root, capsys):
    assert cli.main(["install", "--dest", str(root)]) == 0
    out = capsys.readouterr().out
    assert f"Linked {root / 'manage-precommit'}" in out
    # --dest names a directory, not a product, so there is no reload hint to
    # give: this installer does not guess which agent reads a path somebody
    # typed. The hint appears when an AGENT was resolved -- see below.
    assert "Upgrading the package" in out


def test_install_command_reports_a_refusal_and_fails(root, tmp_path, capsys):
    (root / "manage-precommit").symlink_to(foreign(tmp_path))
    assert cli.main(["install", "--dest", str(root)]) == 1
    assert "symlink to something else" in capsys.readouterr().err


def test_install_dry_run_changes_nothing(root, capsys):
    assert cli.main(["install", "--dest", str(root), "--dry-run"]) == 0
    assert "Would link" in capsys.readouterr().out
    assert not (root / "manage-precommit").exists()


def test_install_dry_run_reports_the_refusal_the_real_run_would_make(root, capsys):
    """A dry run that hides a refusal is the one output nobody can check."""
    (root / "manage-precommit").mkdir()
    assert cli.main(["install", "--dest", str(root), "--dry-run"]) == 1
    assert "already exists and is not a symlink" in capsys.readouterr().err


def test_install_defaults_to_the_home_skills_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["install"]) == 0
    assert (tmp_path / ".claude" / "skills" / "manage-precommit").is_symlink()
    assert str(tmp_path / ".claude") in capsys.readouterr().out


def test_uninstall_command_removes_the_link(root, capsys):
    cli.main(["install", "--dest", str(root)])
    assert cli.main(["uninstall", "--dest", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Removed" in out
    assert "pipx uninstall" in out


def test_uninstall_command_says_when_there_was_nothing(root, capsys):
    assert cli.main(["uninstall", "--dest", str(root)]) == 0
    assert "Nothing to remove" in capsys.readouterr().out


def test_uninstall_command_reports_a_refusal_and_fails(root, capsys):
    (root / "manage-precommit").mkdir()
    assert cli.main(["uninstall", "--dest", str(root)]) == 1
    assert "is a directory, not a symlink" in capsys.readouterr().err


def test_uninstall_dry_run_changes_nothing(root, capsys):
    cli.main(["install", "--dest", str(root)])
    capsys.readouterr()
    assert cli.main(["uninstall", "--dest", str(root), "--dry-run"]) == 0
    assert "Would remove" in capsys.readouterr().out
    assert (root / "manage-precommit").is_symlink()


def test_uninstall_dry_run_says_when_there_is_nothing(root, capsys):
    assert cli.main(["uninstall", "--dest", str(root), "--dry-run"]) == 0
    assert "Nothing at" in capsys.readouterr().out


def test_uninstall_dry_run_reports_a_refusal(root, tmp_path, capsys):
    (root / "manage-precommit").symlink_to(foreign(tmp_path))
    assert cli.main(["uninstall", "--dest", str(root), "--dry-run"]) == 1
    assert "not a packaged skill" in capsys.readouterr().err


def test_uninstall_defaults_to_the_home_skills_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    cli.main(["install"])
    capsys.readouterr()
    assert cli.main(["uninstall"]) == 0
    assert not (tmp_path / ".claude" / "skills" / "manage-precommit").exists()


# --- argv handling ----------------------------------------------------------


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out.strip() == __version__


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_is_the_module_docstring(flag, capsys):
    assert cli.main([flag]) == 0
    assert "manage-precommit install" in capsys.readouterr().out


def test_no_arguments_prints_usage_and_fails(capsys):
    """Nothing to do is not success: `manage-precommit` alone installed nothing."""
    assert cli.main([]) == 2
    assert "manage-precommit install" in capsys.readouterr().out


@pytest.mark.parametrize(("command", "script"), sorted(cli.MOVED_TO_SCRIPTS.items()))
def test_a_skill_command_says_where_the_work_lives(command, script, capsys):
    """`manage-precommit detect` is the obvious thing to type and has never been
    a command here. "unknown command" would send someone to --help to find out
    it is not there either."""
    assert cli.main([command]) == 2
    err = capsys.readouterr().err
    assert "is not a command of this installer" in err
    assert f"scripts/{script}" in err


def test_an_unknown_command_prints_usage(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "unknown command 'frobnicate'" in capsys.readouterr().err


def test_main_reads_sys_argv_when_given_nothing(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["manage-precommit", "--version"])
    assert cli.main() == 0
    assert capsys.readouterr().out.strip() == __version__


# -- round 15 ----------------------------------------------------------------


def test_uninstall_force_still_refuses_a_real_directory(root):
    """--force reaches the foreign-LINK branch and must never reach this one.
    `install` never creates a directory here, so one that exists is somebody
    else's -- possibly a hand-written skill. A one-line regression (`and not
    force`) would make `manage-precommit uninstall --force` rmtree it, and every
    other test in this file would still pass."""
    hand_written = root / "manage-precommit"
    hand_written.mkdir()
    (hand_written / "SKILL.md").write_text("mine\n")

    with pytest.raises(FileExistsError, match="is a directory, not a symlink"):
        cli.uninstall(root, force=True)
    assert (hand_written / "SKILL.md").read_text() == "mine\n"


def test_uninstall_force_refusal_survives_the_command_layer(root, capsys):
    hand_written = root / "manage-precommit"
    hand_written.mkdir()
    (hand_written / "SKILL.md").write_text("mine\n")

    assert cli.main(["uninstall", "--dest", str(root), "--force"]) == 1
    assert "is a directory, not a symlink" in capsys.readouterr().err
    assert (hand_written / "SKILL.md").is_file()


# -- more than one agent -------------------------------------------------------


def test_installing_for_codex_and_copilot_writes_one_link_not_two(tmp_path, monkeypatch, capsys):
    """They read the SAME directory. Two links of the same name cannot coexist
    there, and Copilot -- which reads both its own directory and the shared one
    -- would list the skill twice if we used its private path instead."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["install", "--agent", "codex", "--agent", "copilot"]) == 0
    shared = tmp_path / ".agents" / "skills" / "manage-precommit"
    assert shared.is_symlink()
    assert not (tmp_path / ".copilot").exists()
    out = capsys.readouterr().out
    assert out.count("Linked ") == 1, out
    # Both products are still named, and both reload hints given.
    assert "Codex" in out and "Copilot" in out
    assert "/skills reload" in out


def test_install_all_covers_every_known_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["install", "--all"]) == 0
    assert (tmp_path / ".claude" / "skills" / "manage-precommit").is_symlink()
    assert (tmp_path / ".agents" / "skills" / "manage-precommit").is_symlink()


def test_naming_an_agent_that_is_not_here_says_so_and_acts_anyway(tmp_path, monkeypatch, capsys):
    """An installer acting on a guess should be cheap to overrule -- including
    when the guess is "you do not have this"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["install", "--agent", "codex"]) == 0
    assert (tmp_path / ".agents" / "skills" / "manage-precommit").is_symlink()
    assert "not detected here -- acting anyway" in capsys.readouterr().out


def test_with_nothing_installed_it_refuses_to_guess(tmp_path, monkeypatch, capsys):
    """Linking into a directory nobody reads is worse than saying so: it looks
    like it worked."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["install"]) == 1
    err = capsys.readouterr().err
    assert "no supported agent found" in err
    assert "--agent" in err and "--all" in err and "--dest" in err


def test_uninstall_sweeps_every_directory_even_when_nothing_is_detected(tmp_path, monkeypatch):
    """A link outlives the product that read it, and that is exactly when
    leaving it behind is worst -- so uninstall sweeps rather than detects."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cli.main(["install", "--all"])
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["uninstall"]) == 0
    assert not (tmp_path / ".claude" / "skills" / "manage-precommit").exists()
    assert not (tmp_path / ".agents" / "skills" / "manage-precommit").exists()


def test_a_detected_agent_gets_its_own_reload_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["install", "--agent", "claude"]) == 0
    assert "restart Claude Code" in capsys.readouterr().out
