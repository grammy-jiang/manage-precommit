"""The `manage-precommit` console script: an installer, and nothing else.

    manage-precommit install     link the skill where Claude Code looks for it
    manage-precommit uninstall   remove that link again

The work itself is not here. `skill/scripts/` holds it, beside the SKILL.md
that drives it, and those scripts are run by path and import each other by
plain module name -- so they need this package installed no more than any other
skill's bundled scripts do. Installing is the one job that cannot be done from
inside the skill directory, because it is what puts the skill directory where
an agent will look. That is the whole reason this package exists.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from manage_precommit import __version__

SKILL_NAME = "manage-precommit"

# Where Claude Code reads personal skills from. `--dest` overrules it, because
# an installer acting on a guess should be cheap to correct.
DEFAULT_SKILLS_DIR = Path(".claude") / "skills"

# These are not commands of the installer and never were, but they are the
# obvious things to type. Say where the work actually lives rather than "unknown
# command", which sends someone to `--help` to find out it is not there either.
MOVED_TO_SCRIPTS = {
    "detect": "precommit.py",
    "recommend": "precommit.py",
    "generate": "precommit.py",
    "verify": "precommit.py",
    "git": "gitwork.py",
    "commit": "gitwork.py",
    "push": "gitwork.py",
    "summary": "summary.py",
}


def skills_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / DEFAULT_SKILLS_DIR


def skill_source() -> Path:
    """The packaged skill directory: SKILL.md plus everything it drives.

    One path, not a search: the skill sits beside this module in the checkout
    and in site-packages alike, because nothing remaps it at build time. A file
    found under either root is therefore at the same path relative to the
    package, which is what makes a path in a traceback traceable to the repo.
    """
    source = Path(__file__).resolve().parent / "skill"
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(f"packaged skill files not found at {source}")
    return source


def link_target(link: Path) -> Path | None:
    """What `link` points at, or None if it is not a symlink at all."""
    if not link.is_symlink():
        return None
    return Path(link.readlink())


def is_our_link(link: Path) -> bool:
    """True only for a symlink this package's `install` could have created.

    `uninstall` removes nothing else: a real directory, or a link to something
    else, belongs to somebody and is not ours to delete.
    """
    target = link_target(link)
    if target is None:
        return False
    resolved = target if target.is_absolute() else (link.parent / target)
    resolved = resolved.resolve()
    return resolved.name == "skill" and (resolved / "SKILL.md").is_file()


def install_refusal(dest: Path, *, force: bool) -> str | None:
    """Why `install` would refuse to write `dest`, or None if it would proceed.

    Split out from `install` so `--dry-run` answers the same question the real
    run does. A dry run that reports work it would refuse to do is worse than no
    dry run: it is the one output a person checks *because* they do not want to
    find out the hard way.
    """
    if dest.is_symlink():
        if is_our_link(dest) or force:
            return None  # installing over our own link is idempotent
        return f"{dest} is a symlink to something else -- re-run with --force to replace it"
    if dest.exists() and not force:
        # Never silently delete a real directory: it may be a hand-written skill,
        # or an older copy holding files this package did not put there.
        return (
            f"{dest} already exists and is not a symlink -- inspect it, then either "
            "remove it yourself or re-run with --force"
        )
    return None


def install(dest_root: Path, *, force: bool) -> Path:
    """Symlink the packaged skill into one skills directory.

    A link rather than a copy, so upgrading the package upgrades the skill with
    no second step and no chance of the two drifting.
    """
    source = skill_source()
    dest = dest_root / SKILL_NAME

    refusal = install_refusal(dest, force=force)
    if refusal is not None:
        raise FileExistsError(refusal)
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)

    dest_root.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source, target_is_directory=True)
    return dest


def uninstall_refusal(dest: Path, *, force: bool) -> str | None:
    """Why `uninstall` would refuse to remove `dest`, or None if it would not.

    None also covers "there is nothing there", which is not a refusal --
    `uninstall` reports that and succeeds.
    """
    if not dest.is_symlink():
        if not dest.exists():
            return None
        return (
            f"{dest} is a directory, not a symlink -- `install` never creates one, so this "
            "is not ours to remove. Delete it yourself if you no longer want it."
        )
    if not is_our_link(dest) and not force:
        return (
            f"{dest} points at {link_target(dest)}, which is not a packaged skill -- "
            "re-run with --force if you are sure"
        )
    return None


def uninstall(dest_root: Path, *, force: bool) -> Path | None:
    """Remove what `install` created. Returns the path removed, or None."""
    dest = dest_root / SKILL_NAME
    refusal = uninstall_refusal(dest, force=force)
    if refusal is not None:
        raise FileExistsError(refusal)
    if not dest.is_symlink():
        return None
    dest.unlink()  # the link only; whatever it pointed at is untouched
    return dest


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description, allow_abbrev=False)
    parser.add_argument(
        "--dest", help=f"skills directory to act on (default: ~/{DEFAULT_SKILLS_DIR})"
    )
    parser.add_argument("--force", action="store_true", help="act even on something not ours")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="change nothing; print what would happen, refusals included",
    )
    return parser


def cmd_install(argv: list[str]) -> int:
    args = _parser(
        "manage-precommit install", "Link the skill where Claude Code looks for it."
    ).parse_args(argv)
    root = Path(args.dest) if args.dest else skills_dir()
    dest = root / SKILL_NAME

    if args.dry_run:
        refusal = install_refusal(dest, force=args.force)
        if refusal is not None:
            print(f"manage-precommit: {refusal}", file=sys.stderr)
            return 1
        print(f"Would link {dest} -> {skill_source()}")
        return 0

    try:
        linked = install(root, force=args.force)
    except (FileExistsError, FileNotFoundError, OSError) as exc:
        print(f"manage-precommit: {exc}", file=sys.stderr)
        return 1

    print(f"Linked {linked} -> {skill_source()}")
    print("Upgrading the package now upgrades the skill.")
    print("To pick it up: restart Claude Code.")
    return 0


def cmd_uninstall(argv: list[str]) -> int:
    args = _parser(
        "manage-precommit uninstall", "Remove the link that `install` created."
    ).parse_args(argv)
    root = Path(args.dest) if args.dest else skills_dir()
    dest = root / SKILL_NAME

    if args.dry_run:
        refusal = uninstall_refusal(dest, force=args.force)
        if refusal is not None:
            print(f"manage-precommit: {refusal}", file=sys.stderr)
            return 1
        print(f"{'Would remove' if dest.is_symlink() else 'Nothing at'} {dest}")
        return 0

    try:
        removed = uninstall(root, force=args.force)
    except (FileExistsError, OSError) as exc:
        print(f"manage-precommit: {exc}", file=sys.stderr)
        return 1

    if removed is None:
        print(f"Nothing to remove: no {SKILL_NAME} in {root}")
        return 0
    print(f"Removed {removed}")
    print("The package itself is untouched -- `pipx uninstall` removes that.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2

    command, rest = argv[0], argv[1:]
    if command == "install":
        return cmd_install(rest)
    if command == "uninstall":
        return cmd_uninstall(rest)

    if command in MOVED_TO_SCRIPTS:
        script = MOVED_TO_SCRIPTS[command]
        print(
            f"manage-precommit: {command!r} is not a command of this installer.\n"
            f"The skill's scripts do that work, and are run by path -- for example:\n"
            f"    python3 {skills_dir() / SKILL_NAME / 'scripts' / script} --help",
            file=sys.stderr,
        )
        return 2

    print(f"manage-precommit: unknown command {command!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
