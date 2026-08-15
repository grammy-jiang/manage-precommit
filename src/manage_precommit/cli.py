"""The `manage-precommit` console script: an installer, and nothing else.

    manage-precommit install     link the skill where your agents look for it
    manage-precommit uninstall   remove those links again

Both commands work out which agents are installed on this machine and act on
each one's skills directory; `agents.py` holds that table. `--agent`, `--all`
and `--dest` overrule the detection, because an installer acting on a guess
should be cheap to correct.

The work itself is not here. `skill/scripts/` holds it, beside the SKILL.md
that drives it, and those scripts are run by path and import each other by
plain module name -- so they need this package installed no more than any other
skill's bundled scripts do. Installing is the one job that cannot be done from
inside the skill directory, because it is what puts the skill directory where
an agent will look. That is the whole reason this package exists.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from manage_precommit import __version__
from manage_precommit.agents import (
    AGENTS,
    BY_KEY,
    Agent,
    Target,
    all_targets,
    detect,
    detect_all,
    targets_for,
)

SKILL_NAME = "manage-precommit"
DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills"

# These were subcommands here until the work moved into the skill. Anyone with
# the old call in a script or in their fingers gets told where it went, which is
# worth more than a bare "unknown command".
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


def skill_source() -> Path:
    """The packaged skill directory: SKILL.md plus references/.

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
    return Path(os.readlink(link))


def is_our_link(link: Path) -> bool:
    """True only for a symlink this package's `install` could have created.

    `uninstall` removes nothing else: a real directory, or a link to something
    else, belongs to somebody and is not ours to delete.
    """
    target = link_target(link)
    if target is None:
        return False
    resolved = target.resolve() if target.is_absolute() else (link.parent / target).resolve()
    return resolved.name == "skill" and (resolved / "SKILL.md").is_file()


def remove_link(link: Path) -> None:
    """Delete a symlink, whatever it points at, on any platform.

    POSIX has one call for this. Windows has two: a symlink to a directory is
    itself a directory entry, and `unlink` refuses it -- `rmdir` is what removes
    the link. Getting this wrong makes `uninstall` fail on the very platform
    where `install` had to work hardest, and makes reinstalling over an existing
    link fail too.

    `os.path.isdir` follows the link on purpose: a dangling link is not a
    directory to Windows either, and `unlink` is right for it.
    """
    if os.name == "nt" and os.path.isdir(link):  # pragma: no cover - Windows only
        os.rmdir(link)  # removes the link, never its target
    else:
        link.unlink()


def install_refusal(dest: Path, *, force: bool) -> str | None:
    """Why `install` would refuse to write `dest`, or None if it would proceed.

    Split out from `install` so that `--dry-run` answers the same question the
    real run does. A dry run that reports work it would refuse to do is worse
    than no dry run: it is the one output a person checks *because* they do not
    want to find out the hard way.
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
    no second step and no chance of the two drifting. Every agent this installer
    knows about follows a symlinked skill directory.
    """
    source = skill_source()
    dest = dest_root / SKILL_NAME

    refusal = install_refusal(dest, force=force)
    if refusal is not None:
        raise FileExistsError(refusal)
    if dest.is_symlink():
        remove_link(dest)
    elif dest.exists():
        shutil.rmtree(dest)

    dest_root.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        # ERROR_PRIVILEGE_NOT_HELD. Windows refuses symlinks to an unprivileged
        # process unless Developer Mode is on, and says so in a number. The link
        # is not optional here -- a copy would stop tracking the package on the
        # next upgrade -- so this reports what to turn on rather than falling
        # back to something that looks like success.
        if getattr(exc, "winerror", None) == 1314:
            raise OSError(
                f"{dest}: Windows will not let this process create a symlink. Turn on "
                "Developer Mode in Settings, or run this once from an elevated prompt. "
                "(The setting is called Developer Mode on both Windows 10 and 11; the two "
                "keep it in different places, so search Settings for the name.) A copy is "
                "not offered: the link is what makes upgrading the package upgrade the skill."
            ) from exc
        raise
    return dest


def uninstall_refusal(dest: Path, *, force: bool) -> str | None:
    """Why `uninstall` would refuse to remove `dest`, or None if it would not.

    None also covers "there is nothing there", which is not a refusal --
    `uninstall` reports that and succeeds. Shared with `--dry-run` for the same
    reason as `install_refusal`.
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
    """Remove what `install` created. Returns the path removed, or None.

    Refuses anything else: a real directory was never made by `install`, and a
    link pointing elsewhere is not this package's to remove.
    """
    dest = dest_root / SKILL_NAME
    refusal = uninstall_refusal(dest, force=force)
    if refusal is not None:
        raise FileExistsError(refusal)
    if not dest.is_symlink():
        return None
    remove_link(dest)  # the link only; whatever it pointed at is untouched
    return dest


# --- choosing what to act on ------------------------------------------------


class NoTargets(Exception):
    """Nothing to act on, and no default worth guessing at."""


def _named_agents(keys: list[str]) -> list[Agent]:
    """The agents `--agent` asked for, in table order, without duplicates."""
    wanted = set(keys)
    return [agent for agent in AGENTS if agent.key in wanted]


def resolve_targets(args: argparse.Namespace, *, sweep: bool) -> tuple[list[Target], list[str]]:
    """Which skills directories this run touches, and what to say about them.

    Returns `(targets, notes)`. `sweep` is what separates the two commands:
    with nothing named, `install` acts only where an agent was actually found,
    while `uninstall` acts on every directory it could ever have written to. A
    link we made outlives the product that read it, and that is precisely the
    case where leaving it behind would be worst.
    """
    home = Path.home()
    notes: list[str] = []

    if args.dest is not None:
        return [Target(Path(args.dest), ())], notes

    if args.all:
        return targets_for(AGENTS, home=home), notes

    if args.agent:
        chosen = _named_agents(args.agent)
        for agent in chosen:
            if not detect(agent, home=home).found:
                notes.append(f"{agent.label} was not detected here -- acting anyway, as asked")
        return targets_for(chosen, home=home), notes

    if sweep:
        return all_targets(home=home), notes

    found = [d for d in detect_all(home=home) if d.found]
    if not found:
        looked = ", ".join(f"`{a.binary}` or {a.config_path(home)}" for a in AGENTS)
        raise NoTargets(
            f"no supported agent found on this machine. Looked for: {looked}.\n"
            "Install one, or name where the skill should go with --agent NAME, --all, or --dest."
        )
    for detection in found:
        notes.append(f"{detection.agent.label} detected: {detection.evidence}")
    return targets_for([d.agent for d in found], home=home), notes


# --- the two commands -------------------------------------------------------


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description, allow_abbrev=False)
    parser.add_argument(
        "--agent",
        action="append",
        choices=sorted(BY_KEY),
        metavar="NAME",
        help=f"act for this agent, detected or not (repeatable): {', '.join(sorted(BY_KEY))}",
    )
    parser.add_argument("--all", action="store_true", help="act for every known agent")
    parser.add_argument("--dest", help="one skills directory, instead of detecting anything")
    parser.add_argument("--force", action="store_true", help="act even on something not ours")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="change nothing; print what would happen, refusals included",
    )
    return parser


def _for_label(target: Target) -> str:
    return f"  ({target.label})" if target.agents else ""


def cmd_install(argv: list[str]) -> int:
    args = _parser(
        "manage-precommit install", "Link the skill where your agents look for it."
    ).parse_args(argv)

    try:
        targets, notes = resolve_targets(args, sweep=False)
    except NoTargets as exc:
        print(f"manage-precommit: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(note)

    failed = False
    linked: list[Target] = []
    for target in targets:
        if args.dry_run:
            dest = target.path / SKILL_NAME
            refusal = install_refusal(dest, force=args.force)
            if refusal is not None:
                print(f"manage-precommit: {refusal}", file=sys.stderr)
                failed = True
                continue
            print(f"Would link {dest} -> {skill_source()}{_for_label(target)}")
            continue
        try:
            dest = install(target.path, force=args.force)
        except (FileExistsError, FileNotFoundError, OSError) as exc:
            print(f"manage-precommit: {exc}", file=sys.stderr)
            failed = True
            continue
        linked.append(target)
        print(f"Linked {dest} -> {skill_source()}{_for_label(target)}")

    if linked:
        hints = dict.fromkeys(hint for target in linked for hint in target.reload_hints)
        print("Upgrading the package now upgrades the skill.")
        if hints:
            print(f"To pick it up: {'; '.join(hints)}.")
    return 1 if failed else 0


def cmd_uninstall(argv: list[str]) -> int:
    args = _parser(
        "manage-precommit uninstall", "Remove the links that `install` created."
    ).parse_args(argv)

    try:
        targets, notes = resolve_targets(args, sweep=True)
    except NoTargets as exc:  # pragma: no cover - sweep always has targets
        print(f"manage-precommit: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(note)

    failed = False
    removed: list[Path] = []
    for target in targets:
        if args.dry_run:
            dest = target.path / SKILL_NAME
            refusal = uninstall_refusal(dest, force=args.force)
            if refusal is not None:
                print(f"manage-precommit: {refusal}", file=sys.stderr)
                failed = True
                continue
            state = "Would remove" if dest.is_symlink() else "Nothing at"
            print(f"{state} {dest}{_for_label(target)}")
            continue
        try:
            gone = uninstall(target.path, force=args.force)
        except (FileExistsError, OSError) as exc:
            print(f"manage-precommit: {exc}", file=sys.stderr)
            failed = True
            continue
        if gone is not None:
            removed.append(gone)
            print(f"Removed {gone}{_for_label(target)}")

    if not removed and not failed and not args.dry_run:
        where = ", ".join(str(target.path) for target in targets)
        print(f"Nothing to remove: no {SKILL_NAME} in {where}")
    if removed:
        print("The package itself is untouched -- `pipx uninstall` removes that.")
    return 1 if failed else 0


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
            f"The scripts do that work, and are run by path -- for example:\n"
            f"    python3 {DEFAULT_SKILLS_DIR / SKILL_NAME / 'scripts' / script} ...",
            file=sys.stderr,
        )
        return 2

    print(f"manage-precommit: unknown command {command!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
