"""Which agents are installed on this machine, and where each looks for skills.

One table, because this is a fact about three products rather than a policy of
ours. `SKILL.md` follows the Agent Skills specification <https://agentskills.io>,
which all three read; what differs is only the directory each one scans:

    Claude Code   ~/.claude/skills
    Codex         ~/.agents/skills          (its only user-scope location)
    Copilot CLI   ~/.agents/skills          (it also reads ~/.copilot/skills)

Copilot is pointed at the shared `~/.agents/skills` rather than its own private
directory on purpose. Codex has no alternative -- `~/.agents/skills` is the only
user-scope path it scans -- so a machine with both products would otherwise get
two links with the same skill name, and Copilot, which reads both directories,
would list the skill twice. One link in the directory the open standard names is
the same install for either product, and for both at once.

Detection is deliberately shallow: a binary on PATH, or the product's own
configuration directory. Neither runs anything. `install` acting on a guess is
the failure mode to avoid, so a wrong guess must be cheap to overrule, which is
what `--agent`, `--all` and `--dest` are for.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Agent:
    """One product: how to spot it, and where it reads skills from."""

    key: str
    label: str
    binary: str
    # Both are relative to the home directory rather than absolute, so that a
    # test can point HOME at a temporary tree and get the whole table with it.
    config_dir: str
    skills_dir: str
    reload_hint: str

    def config_path(self, home: Path) -> Path:
        return home / self.config_dir

    def skills_path(self, home: Path) -> Path:
        return home / self.skills_dir


AGENTS: tuple[Agent, ...] = (
    Agent(
        key="claude",
        label="Claude Code",
        binary="claude",
        config_dir=".claude",
        skills_dir=".claude/skills",
        reload_hint="restart Claude Code",
    ),
    Agent(
        key="codex",
        label="Codex",
        binary="codex",
        config_dir=".codex",
        skills_dir=".agents/skills",
        reload_hint="restart Codex",
    ),
    Agent(
        key="copilot",
        label="GitHub Copilot CLI",
        binary="copilot",
        config_dir=".copilot",
        skills_dir=".agents/skills",
        reload_hint="run `/skills reload` in Copilot, or restart it",
    ),
)

BY_KEY: dict[str, Agent] = {agent.key: agent for agent in AGENTS}


@dataclass(frozen=True)
class Detection:
    """An agent, and what -- if anything -- gave it away."""

    agent: Agent
    evidence: str | None

    @property
    def found(self) -> bool:
        return self.evidence is not None


def detect(agent: Agent, *, home: Path | None = None) -> Detection:
    """Look for one agent. The evidence is reported, not just the verdict.

    Two signals, either of which is enough. The binary on PATH is the stronger
    one; the configuration directory catches an editor extension or an IDE
    install that never put a launcher on PATH.
    """
    home = Path.home() if home is None else home
    on_path = shutil.which(agent.binary)
    if on_path:
        return Detection(agent, f"{agent.binary} on PATH at {on_path}")
    config = agent.config_path(home)
    if config.is_dir():
        return Detection(agent, f"{config} exists")
    return Detection(agent, None)


def detect_all(*, home: Path | None = None) -> list[Detection]:
    """Every known agent, found or not, in table order."""
    home = Path.home() if home is None else home
    return [detect(agent, home=home) for agent in AGENTS]


@dataclass(frozen=True)
class Target:
    """A skills directory, and the agents that read it."""

    path: Path
    agents: tuple[Agent, ...]

    @property
    def label(self) -> str:
        return " and ".join(agent.label for agent in self.agents) or str(self.path)

    @property
    def reload_hints(self) -> list[str]:
        return [agent.reload_hint for agent in self.agents]


def targets_for(agents: Sequence[Agent], *, home: Path | None = None) -> list[Target]:
    """Group agents by the directory they read, keeping first-seen order.

    The grouping is the point: Codex and Copilot share one directory, so asking
    for both yields one link, not two entries with the same name.
    """
    home = Path.home() if home is None else home
    grouped: dict[Path, list[Agent]] = {}
    for agent in agents:
        grouped.setdefault(agent.skills_path(home), []).append(agent)
    return [Target(path, tuple(members)) for path, members in grouped.items()]


def all_targets(*, home: Path | None = None) -> list[Target]:
    """Every directory this installer can write to, whatever is installed.

    `uninstall` sweeps these rather than the detected ones: a link we made is
    still ours to remove after the product that read it has been uninstalled,
    and that is exactly when it would otherwise be left behind.
    """
    return targets_for(AGENTS, home=home)
