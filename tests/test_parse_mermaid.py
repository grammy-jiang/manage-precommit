"""The browser-free mermaid hook, driven the way pre-commit drives it.

`parse-mermaid.mjs` is a program this skill copies into other people's
repositories and wires up as a hook, so it is tested as one: run by `node`,
with the files on argv and its dependencies where pre-commit puts them -- a
NODE_PATH pointing into an environment of its own.

Nothing here touches the network. `mermaid` and `linkedom` are stand-ins that
record what they were asked and fail on cue. The contract under test is the
hook's -- which fences it extracts, when it loads Mermaid at all, what it
reports and with which exit code -- not Mermaid's parser, which has its own
suite. The real pair is exercised by this repository's own pre-commit run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import SKILL

ASSET = SKILL / "assets" / "parse-mermaid.mjs"
NODE = shutil.which("node")

if NODE is None and os.environ.get("CI"):  # pragma: no cover - a runner without node
    raise RuntimeError("node is not on PATH; on CI that is a broken runner, not a skip")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# A DOM-shaped nothing. The hook needs `parseHTML` to hand back a window with a
# document, which it installs as globals before importing mermaid; what those
# objects can do is Mermaid's concern, and the stand-in mermaid has none.
FAKE_LINKEDOM = "exports.parseHTML = () => ({ window: { document: {} } });\n"

# Resolved the way the real package is: no `main`, an `exports` map whose
# `default` is the ES module -- so the CommonJS resolver the hook uses to find
# it is exercised against the same shape.
MERMAID_MANIFEST = {
    "name": "mermaid",
    "version": "0.0.0-test",
    "type": "module",
    "exports": {".": {"import": "./index.mjs", "default": "./index.mjs"}},
}


def fake_mermaid(log: Path) -> str:
    """A `mermaid` that logs every call and rejects any diagram saying BAD."""
    return (
        'import { appendFileSync } from "node:fs";\n'
        f"const LOG = {json.dumps(str(log))};\n"
        'const note = (entry) => appendFileSync(LOG, JSON.stringify(entry) + "\\n");\n'
        'note({ event: "loaded", window: typeof globalThis.window, document: typeof globalThis.document });\n'
        "export default {\n"
        '  initialize(options) { note({ event: "initialize", options }); },\n'
        "  async parse(text) {\n"
        '    note({ event: "parse", text });\n'
        '    if (text.includes("BAD")) {\n'
        "      throw new Error(\"Parse error on line 2:\\n...BAD\\n---^\\nExpecting 'SEMI', got 'BAD'\");\n"
        "    }\n"
        '    return { diagramType: "flowchart-v2" };\n'
        "  },\n"
        "};\n"
    )


def plant(modules: Path, log: Path, mermaid_source: str | None = None) -> None:
    """A `mermaid` and a `linkedom` under `modules`, as `npm install -g` lays them out."""
    linkedom = modules / "linkedom"
    linkedom.mkdir(parents=True)
    (linkedom / "package.json").write_text(
        json.dumps({"name": "linkedom", "version": "0.0.0-test", "main": "index.js"})
    )
    (linkedom / "index.js").write_text(FAKE_LINKEDOM)
    mermaid = modules / "mermaid"
    mermaid.mkdir(parents=True)
    (mermaid / "package.json").write_text(json.dumps(MERMAID_MANIFEST))
    (mermaid / "index.mjs").write_text(mermaid_source or fake_mermaid(log))


class Env:
    """pre-commit's node environment for the hook: `<env>/lib/node_modules` on NODE_PATH."""

    def __init__(self, root: Path) -> None:
        self.modules = root / "node_env" / "lib" / "node_modules"
        self.log = root / "mermaid.log"
        plant(self.modules, self.log)

    def events(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def parsed(self) -> list[str]:
        return [e["text"] for e in self.events() if e["event"] == "parse"]

    def loaded(self) -> bool:
        return any(e["event"] == "loaded" for e in self.events())


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    return d


def hook(
    *files: str, cwd: Path, node_path: str | None, script: Path = ASSET
) -> subprocess.CompletedProcess[str]:
    """Run the hook as pre-commit would: `node <script> <files...>` with NODE_PATH set."""
    environ = dict(os.environ)
    environ.pop("NODE_PATH", None)
    if node_path is not None:
        environ["NODE_PATH"] = node_path
    return subprocess.run(
        [NODE or "node", str(script), *files],
        cwd=cwd,
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )


# -- when mermaid is loaded at all --------------------------------------------


def test_no_arguments_is_a_pass_that_loads_nothing(docs, env):
    proc = hook(cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert not env.loaded()


def test_markdown_without_a_diagram_never_pays_for_mermaid(docs, env):
    """Most commits that touch Markdown carry no diagram. Loading Mermaid is
    the whole cost of this hook, so those commits must not pay it."""
    (docs / "notes.md").write_text(
        "# notes\n\nSome prose about `mermaid`, and a fence that is not one:\n\n"
        "```python\nprint('```mermaid')\n```\n"
    )
    proc = hook("notes.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert not env.loaded()


def test_an_unclosed_fence_alone_is_reported_without_loading_mermaid(docs, env):
    """Nothing complete to parse, so nothing to load -- and still an error."""
    (docs / "doc.md").write_text("# t\n\n```mermaid\nflowchart TD\n  A --> B\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert "\u2716 doc.md:3" in proc.stderr
    assert "never closed" in proc.stderr
    assert not env.loaded()


def test_mermaid_is_loaded_once_for_any_number_of_diagrams(docs, env):
    (docs / "a.md").write_text("```mermaid\nflowchart TD\n  A --> B\n```\n")
    (docs / "b.md").write_text('```mermaid\npie\n  "x": 1\n```\n\n```mermaid\ngantt\n```\n')
    proc = hook("a.md", "b.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert [e["event"] for e in env.events()].count("loaded") == 1
    assert len(env.parsed()) == 3


def test_the_dom_is_in_place_before_mermaid_is_imported(docs, env):
    """Mermaid reads `window` and `document` at import time, not only at parse
    time; installed afterwards they would be too late."""
    (docs / "a.md").write_text("```mermaid\nflowchart TD\n  A --> B\n```\n")
    hook("a.md", cwd=docs, node_path=str(env.modules))
    loaded = next(e for e in env.events() if e["event"] == "loaded")
    assert loaded["window"] == "object"
    assert loaded["document"] == "object"
    initialised = next(e for e in env.events() if e["event"] == "initialize")
    assert initialised["options"]["startOnLoad"] is False
    assert initialised["options"]["suppressErrorRendering"] is True


# -- which fences are diagrams -------------------------------------------------


def test_backtick_and_tilde_fences_are_both_diagrams(docs, env):
    (docs / "doc.md").write_text(
        "# d\n\n```mermaid\nflowchart TD\n  A --> B\n```\n\n"
        "~~~mermaid\nsequenceDiagram\n  A->>B: hi\n~~~\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B", "sequenceDiagram\n  A->>B: hi"]


def test_the_language_is_the_first_word_of_the_info_string_in_any_case(docs, env):
    (docs / "doc.md").write_text(
        "```Mermaid title=x\nflowchart TD\n  A --> B\n```\n\n"
        "```mermaidjs\nnot ours\n```\n\n"
        "``` mermaid\nspaced\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B", "spaced"]


def test_an_example_inside_another_fence_is_text_not_a_diagram(docs, env):
    """The CommonMark rule that matters most here: only a run of the same
    character at least as long closes a fence. A README showing HOW to write a
    mermaid block, inside a ````markdown block, must not have its example
    parsed -- and the same for a tilde fence around a backtick one."""
    (docs / "doc.md").write_text(
        "````markdown\n```mermaid\nflowchart TD\n  BAD example\n```\n````\n\n"
        "~~~\n```mermaid\nBAD too\n```\n~~~\n\n"
        "```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_a_shorter_run_does_not_close_the_fence(docs, env):
    """A ```` fence holds a ``` line as content, so a diagram can contain one."""
    (docs / "doc.md").write_text("````mermaid\nflowchart TD\n```\n  A --> B\n````\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n```\n  A --> B"]


def test_a_backtick_fence_whose_info_string_holds_a_backtick_is_not_a_fence(docs, env):
    (docs / "doc.md").write_text(
        "``` mermaid `inline`\nthis line is prose\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_a_diagram_in_a_list_item_loses_the_fence_indentation(docs, env):
    """CommonMark strips the opening fence's indentation from the content, and
    a diagram nested under a list item is the ordinary way to meet that."""
    (docs / "doc.md").write_text(
        "1. Step one\n\n   ```mermaid\n   flowchart LR\n     X --> Y\n   ```\n\n2. Step two\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart LR\n  X --> Y"]


def test_crlf_and_a_byte_order_mark_do_not_hide_a_fence(docs, env):
    (docs / "doc.md").write_bytes(
        "\ufeff```mermaid\r\nflowchart TD\r\n  A --> B\r\n```\r\n".encode()
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_an_unclosed_fence_of_another_language_is_not_this_hooks_concern(docs, env):
    (docs / "doc.md").write_text("```python\nprint(1)\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert not env.loaded()


def test_an_unreadable_path_is_skipped_not_reported(docs, env):
    """Deleted between staging and the run, or not a file: pre-commit's list is
    taken as given, and a path this hook cannot read is nothing it can judge."""
    (docs / "ok.md").write_text("```mermaid\nflowchart TD\n  A --> B\n```\n")
    proc = hook("ok.md", "gone.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


# -- what it reports -----------------------------------------------------------


def test_an_invalid_diagram_fails_the_run_and_names_its_fence(docs, env):
    (docs / "doc.md").write_text(
        "# t\n\nintro\n\n```mermaid\nflowchart TD\n  A --> B\n```\n\n"
        "```mermaid\nflowchart TD\n  BAD\n```\n"
    )
    (docs / "fine.md").write_text("```mermaid\nflowchart TD\n  C --> D\n```\n")
    proc = hook("doc.md", "fine.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert "\u2716 doc.md:10" in proc.stderr
    assert "Parse error on line 2" in proc.stderr
    assert "Expecting 'SEMI', got 'BAD'" in proc.stderr
    assert "1 problem(s) in 1 file(s)" in proc.stderr
    # The count is relative to the diagram, and the footer says so once.
    assert "counts from the top of that diagram" in proc.stderr
    # Every diagram is still parsed; the first failure does not end the run.
    assert env.parsed() == [
        "flowchart TD\n  A --> B",
        "flowchart TD\n  BAD",
        "flowchart TD\n  C --> D",
    ]
    assert "fine.md" not in proc.stderr


def test_an_unclosed_fence_and_a_bad_diagram_are_both_reported_in_file_order(docs, env):
    (docs / "doc.md").write_text(
        "```mermaid\nflowchart TD\n  BAD\n```\n\nprose\n\n```mermaid\nflowchart TD\n  A --> B\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    first = proc.stderr.index("\u2716 doc.md:1\n")
    second = proc.stderr.index("\u2716 doc.md:8\n")
    assert first < second
    assert "2 problem(s) in 1 file(s)" in proc.stderr


def test_an_empty_diagram_is_handed_to_mermaid_not_skipped(docs, env):
    """An empty fence renders as an error on GitHub, and Mermaid's own answer
    to it -- "No diagram type detected" -- is the one to relay. Deciding here
    that empty is fine would be a second parser with a different opinion."""
    (docs / "doc.md").write_text("```mermaid\n```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr  # the stand-in accepts it; the point is the call
    assert env.parsed() == [""]


# -- where mermaid comes from ---------------------------------------------------


def test_the_pinned_copy_on_node_path_beats_the_repositorys_own(tmp_path, env):
    """The version that checks the diagrams is the one .pre-commit-config.yaml
    pins, not whatever the project happens to depend on. Installed the way it
    is shipped -- at <repo>/scripts/ -- the hook sits under the repository's
    node_modules, and ordinary resolution would find that copy first."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "parse-mermaid.mjs"
    shutil.copyfile(ASSET, script)
    plant(
        repo / "node_modules",
        tmp_path / "unused.log",
        mermaid_source='throw new Error("the repository\'s own mermaid was imported");\n',
    )
    (repo / "doc.md").write_text("```mermaid\nflowchart TD\n  A --> B\n```\n")

    proc = hook("doc.md", cwd=repo, node_path=str(env.modules), script=script)
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]

    # Run by hand, with no NODE_PATH, ordinary resolution applies and the
    # repository's copy is the one found -- which is what `npm install mermaid
    # linkedom` in a repository is for.
    proc = hook("doc.md", cwd=repo, node_path=None, script=script)
    assert proc.returncode == 2
    assert "the repository's own mermaid was imported" in proc.stderr


def test_missing_dependencies_are_an_environment_error_not_a_diagram_error(tmp_path):
    """Exit 2, and it says so: a diagram that was never parsed is not invalid,
    and sending somebody to rewrite one over a broken hook environment is the
    failure lint-mermaid.mjs already refuses to produce."""
    empty = tmp_path / "node_env" / "lib" / "node_modules"
    empty.mkdir(parents=True)
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    script = work / "scripts" / "parse-mermaid.mjs"
    shutil.copyfile(ASSET, script)
    (work / "doc.md").write_text("```mermaid\nflowchart TD\n  A --> B\n```\n")
    proc = hook("doc.md", cwd=work, node_path=str(empty), script=script)
    assert proc.returncode == 2
    assert "could not load mermaid and linkedom" in proc.stderr
    assert "additional_dependencies" in proc.stderr
    assert "never parsed" in proc.stderr


# -- containers: block quotes, list items, indented code -------------------------


def test_a_top_level_indented_code_block_is_text_not_a_fence(docs, env):
    """Four spaces of indentation make an indented code block, and a literal
    ```mermaid example inside one is prose about diagrams, not a diagram.
    Left to a whitespace-tolerant match it was parsed -- and an incomplete
    example failed every commit."""
    (docs / "doc.md").write_text(
        "Write it like this:\n\n    ```mermaid\n    flowchart TD\n      BAD\n    ```\n\nand so on.\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert not env.loaded()


def test_indentation_is_relative_to_the_enclosing_list_item(docs, env):
    """The same four spaces under a `- ` item are two past its content column,
    which is a fence; six are four past it, which is indented code."""
    (docs / "doc.md").write_text(
        "- one\n  - two\n\n    ```mermaid\n    flowchart LR\n      X --> Y\n    ```\n\n"
        "- three\n\n      ```mermaid\n      BAD\n      ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart LR\n  X --> Y"]


def test_a_list_ends_at_a_line_indented_short_of_its_content(docs, env):
    """After the list, four spaces are indented code again."""
    (docs / "doc.md").write_text(
        "1. step\n\n   ```mermaid\n   flowchart TD\n     A --> B\n   ```\n\n"
        "Back at the top level:\n\n    ```mermaid\n    BAD\n    ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_a_fence_may_open_on_the_list_marker_line(docs, env):
    (docs / "doc.md").write_text(
        "- ```mermaid\n  flowchart TD\n    A --> B\n  ```\n"
        '10) ```mermaid\n    pie\n      "a": 1\n    ```\n'
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B", 'pie\n  "a": 1']


def test_a_diagram_inside_a_block_quote_is_read_without_its_markers(docs, env):
    """GitHub's alert syntax is a block quote, and a diagram inside a note is
    an ordinary thing to write. The `> ` comes off every line, the fence closes
    inside the quote, and a broken one is reported like any other."""
    (docs / "doc.md").write_text(
        "> [!NOTE]\n> ```mermaid\n> flowchart TD\n>   A --> B\n> ```\n\n"
        "> > ```mermaid\n> > flowchart TD\n> >   BAD\n> > ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert env.parsed() == ["flowchart TD\n  A --> B", "flowchart TD\n  BAD"]
    assert "✖ doc.md:7" in proc.stderr


def test_a_block_quote_that_ends_before_the_closing_fence_leaves_it_unclosed(docs, env):
    """CommonMark closes the fence with its container. For a diagram that is a
    missing closing fence, and the prose after the quote is not diagram text."""
    (docs / "doc.md").write_text("> ```mermaid\n> flowchart TD\n>   A --> B\n\nordinary prose\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert "✖ doc.md:1" in proc.stderr
    assert "never closed" in proc.stderr
    assert not env.loaded()


def test_a_fence_inside_a_quoted_list_item(docs, env):
    (docs / "doc.md").write_text(
        "> - item\n>\n>   ```mermaid\n>   flowchart TD\n>     A --> B\n>   ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_leading_tabs_count_as_four_columns(docs, env):
    (docs / "doc.md").write_text("\t```mermaid\n\tflowchart TD\n\t  A --> B\n\t```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert not env.loaded()


# -- review round 2: closing fences and raw HTML -------------------------------


def test_a_closing_fence_may_be_followed_by_tabs(docs, env):
    (docs / "doc.md").write_text("```mermaid\nflowchart TD\n  A --> B\n```\t\t\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_the_closing_fence_is_judged_against_the_container_not_the_opener(docs, env):
    """An opener indented two columns permits a closer at three, not at five:
    CommonMark allows three past the enclosing block. Dedenting by the opener's
    column first let a deeper fence-looking content line end the block early,
    and the remaining -- possibly broken -- content was never parsed."""
    (docs / "doc.md").write_text(
        "  ```mermaid\n  flowchart TD\n     ```\n    A --> B\n   ```\n\nprose\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n   ```\n  A --> B"]


def test_a_diagram_commented_out_with_html_is_not_checked(docs, env):
    """The ordinary way to park a broken diagram is an HTML comment, and the
    one inside it is the one that does not parse. Markdown is suspended inside
    an HTML block, so it is text."""
    (docs / "doc.md").write_text(
        "<!--\n```mermaid\nflowchart TD\n  BAD\n```\n-->\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_a_one_line_html_block_ends_on_its_own_line(docs, env):
    (docs / "doc.md").write_text(
        "<!-- diagram below -->\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_pre_and_script_blocks_end_at_their_closing_tag_not_at_a_blank_line(docs, env):
    (docs / "doc.md").write_text(
        "<pre>\n\n```mermaid\nBAD\n```\n\n</pre>\n\n<script>\n```mermaid\nBAD\n```\n</script>\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert not env.loaded()


def test_a_block_tag_ends_at_a_blank_line_so_a_details_diagram_is_checked(docs, env):
    """`<details>` around a diagram is common README practice; the blank line
    after `<summary>` ends the HTML block and the fence that follows is a
    fence. Without the blank line the fence is raw HTML, which is what GitHub
    renders too -- text, not a diagram."""
    (docs / "doc.md").write_text(
        "<details>\n<summary>Flow</summary>\n\n```mermaid\nflowchart TD\n  A --> B\n```\n\n</details>\n\n"
        "<div>\n```mermaid\nBAD\n```\n</div>\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_a_lone_tag_after_a_blank_line_opens_an_html_block(docs, env):
    """CommonMark's seventh kind: any complete tag alone on its line, where no
    paragraph is in progress. The fence right under an `<img>` is raw HTML
    until the blank line; the one after it is a diagram."""
    (docs / "doc.md").write_text(
        'Intro.\n\n<img src="x.png" alt="x">\n```mermaid\nBAD\n```\n\n```mermaid\nflowchart TD\n  A --> B\n```\n'
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_a_lone_tag_inside_a_paragraph_does_not_open_an_html_block(docs, env):
    """The same tag directly under a line of prose cannot interrupt the
    paragraph, so the fence after it is a fence."""
    (docs / "doc.md").write_text("Some prose\n<b>\n```mermaid\nflowchart TD\n  A --> B\n```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_an_html_block_inside_a_quote_ends_with_the_quote(docs, env):
    (docs / "doc.md").write_text(
        "> <!--\n> ```mermaid\n> BAD\n> ```\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


# -- review round 3: a fence ends with its list item ---------------------------


def test_a_closer_indented_short_of_the_list_item_does_not_close_the_fence(docs, env):
    """An unindented ``` under `- item` is outside the item, so the item ends
    and the nested fence with it -- unclosed. The stray ``` then opens a new
    fence at the top level, which is what GitHub renders too: a diagram that
    ran to the end of the item, and a code block after it. Reported as the
    missing closing fence it is, rather than as a diagram that parsed."""
    (docs / "doc.md").write_text("- item\n\n  ```mermaid\n  flowchart TD\n    A --> B\n```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert "✖ doc.md:3" in proc.stderr
    assert "never closed" in proc.stderr
    assert not env.loaded()


def test_an_under_indented_content_line_ends_the_list_item_and_the_fence(docs, env):
    (docs / "doc.md").write_text(
        "- item\n\n  ```mermaid\n  flowchart TD\nA --> B\n  ```\n\n```mermaid\nflowchart TD\n  C --> D\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert "✖ doc.md:3" in proc.stderr
    # The stray `  ``` ` opened a fence of its own, with no language, which
    # swallowed the second diagram: nothing after line 3 is a mermaid block.
    assert env.parsed() == []


def test_a_blank_line_inside_a_list_item_fence_is_content_not_an_exit(docs, env):
    (docs / "doc.md").write_text("- item\n\n  ```mermaid\n  flowchart TD\n\n    A --> B\n  ```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n\n  A --> B"]


# -- review round 4: deeper items, and HTML that ends with its item -------------


def test_a_marker_is_read_relative_to_the_enclosing_item(docs, env):
    """A third-level item sits six columns from the margin and is still a list
    marker; read against the margin it was not, so a fence opened on it was
    text. Likewise under a two-digit ordered marker."""
    (docs / "doc.md").write_text(
        "- a\n  - b\n    - ```mermaid\n      flowchart TD\n        A --> B\n      ```\n\n"
        '10. ten\n    - sub\n\n      ```mermaid\n      pie\n        "x": 1\n      ```\n'
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B", 'pie\n  "x": 1']


def test_an_html_block_ends_with_the_list_item_it_started_in(docs, env):
    """`- <div>` opens a blank-line-terminated HTML block inside the item; an
    unindented fence on the next line is outside the item, so the item ends,
    the block with it, and the fence is a fence. The same for a comment the
    item never closed. Indented, the fence is still inside both."""
    (docs / "doc.md").write_text(
        "- <div>\n```mermaid\nflowchart TD\n  A --> B\n```\n\n"
        "- <!--\n```mermaid\nflowchart TD\n  C --> D\n```\n\n"
        "- <div>\n  ```mermaid\n  BAD\n  ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B", "flowchart TD\n  C --> D"]


# -- review round 5: what may interrupt a paragraph, tabs and empty items -----------


def test_a_tab_after_a_list_marker_puts_the_content_at_the_next_tab_stop(docs, env):
    (docs / "doc.md").write_text("-\t```mermaid\n\tflowchart TD\n\t  A --> B\n\t```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_an_empty_list_item_still_encloses_the_fence_under_it(docs, env):
    """`-` alone is an item whose content starts two columns in, so the fence
    under it is inside the item -- and an unindented closer is outside it,
    which is the missing closing fence it always was."""
    (docs / "doc.md").write_text(
        "-\n  ```mermaid\n  flowchart TD\n  ```\n\n-\n  ```mermaid\n  flowchart TD\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert env.parsed() == ["flowchart TD"]
    assert "✖ doc.md:7" in proc.stderr
    assert "never closed" in proc.stderr


def test_only_a_one_may_start_an_ordered_list_under_prose(docs, env):
    """`2. ` directly under a paragraph is prose, so the fence-looking text
    after it is prose too; `1. ` interrupts the paragraph and opens an item,
    and after a blank line any number will do."""
    (docs / "doc.md").write_text(
        # No closer after BAD on purpose: a bare ``` there would open a code
        # block of its own and swallow the next lines, as CommonMark says.
        "Some prose\n2. ```mermaid\nBAD\n\n"
        "More prose\n1. ```mermaid\n   flowchart TD\n   ```\n\n"
        "2. ```mermaid\n   pie\n   ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD", "pie"]


def test_an_empty_item_cannot_interrupt_a_paragraph_either(docs, env):
    (docs / "doc.md").write_text("Some prose\n-\n  ```mermaid\n  flowchart TD\n  ```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    # `-` under prose is prose; the two-column fence after it is then a fence at
    # the top level, closed by the two-column closer -- a diagram either way.
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD"]


def test_a_lone_tag_after_a_heading_opens_an_html_block(docs, env):
    """A heading leaves no paragraph open, so a complete tag right under it
    starts a kind-7 HTML block without a blank line -- and the fence under
    that is raw HTML until the block ends."""
    (docs / "doc.md").write_text(
        '# Title\n<img src="x.png">\n```mermaid\nBAD\n```\n\n```mermaid\nflowchart TD\n  A --> B\n```\n'
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_a_thematic_break_ends_a_paragraph_and_is_not_a_list(docs, env):
    (docs / "doc.md").write_text(
        "Prose\n* * *\n<b>\n```mermaid\nBAD\n```\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


# -- review round 6: containers in either order, and real tab stops ---------------


def test_a_block_quote_inside_a_list_item_is_a_container_too(docs, env):
    """`- > ```mermaid` nests a quote inside an item; `> - ```mermaid` the other
    way round. Containers are a stack, so both orders read the same way and a
    broken diagram in either is found."""
    (docs / "doc.md").write_text(
        "- > ```mermaid\n  > flowchart TD\n  >   A --> B\n  > ```\n\n"
        "> - ```mermaid\n>   flowchart TD\n>     BAD\n>   ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert env.parsed() == ["flowchart TD\n  A --> B", "flowchart TD\n  BAD"]
    assert "✖ doc.md:6" in proc.stderr


def test_a_quote_inside_an_item_ends_with_the_item(docs, env):
    (docs / "doc.md").write_text("- > ```mermaid\n  > flowchart TD\n```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 1
    assert "never closed" in proc.stderr
    assert not env.loaded()


def test_leading_tabs_expand_to_tab_stops_not_to_four_spaces_each(docs, env):
    """Two spaces and a tab reach column four, not six -- so under a bare `-`
    item (content column two) that line is a fence two columns in, not
    indented code four columns in."""
    (docs / "doc.md").write_text("-\n  \t```mermaid\n  \tflowchart TD\n  \t  A --> B\n  \t```\n")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


# -- review round 7: tabs after a quote marker -------------------------------------


def test_a_tab_after_a_quote_marker_is_expanded_at_its_column(docs, env):
    """`>` then a tab: the tab reaches column four, its first column is the
    marker's optional space, and the rest is two columns of indentation -- a
    fence, as the spec has it. Two tabs put the content six columns in, which
    is indented code inside the quote."""
    (docs / "doc.md").write_text(
        ">\t```mermaid\n>\tflowchart TD\n>\t  A --> B\n>\t```\n\n>\t\t```mermaid\n>\t\tBAD\n>\t\t```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


# -- review round 8: setext headings ---------------------------------------------


def test_a_setext_underline_closes_the_paragraph_above_it(docs, env):
    """`Title` over `=====` is a heading, so the `2. ` right under it is not
    interrupting anything and opens an item. A line of `=` with no paragraph
    above it is text, and the `2. ` under that one is prose."""
    (docs / "doc.md").write_text(
        "Title\n=====\n2. ```mermaid\n   flowchart TD\n   ```\n\n=====\n2. ```mermaid\nBAD\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD"]


def test_a_short_hyphen_underline_is_a_setext_heading_too(docs, env):
    """One or two hyphens under a paragraph are a setext underline, not an
    empty list item -- an empty item cannot interrupt a paragraph -- so the
    `2. ` beneath opens an item."""
    (docs / "doc.md").write_text(
        "Title\n-\n2. ```mermaid\n   flowchart TD\n   ```\n\nOther\n--\n2. ```mermaid\n   pie\n   ```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD", "pie"]


# -- review round 10: bare carriage returns, and hgroup ----------------------------


def test_bare_carriage_returns_are_line_endings_too(docs, env):
    (docs / "doc.md").write_bytes(b"# old mac\r\r```mermaid\rflowchart TD\r  A --> B\r```\r")
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]


def test_hgroup_is_a_block_level_tag_that_may_interrupt_a_paragraph(docs, env):
    """A kind-6 tag interrupts a paragraph where a kind-7 one cannot, so the
    fence right under `<hgroup>` beneath prose is raw HTML until the blank
    line."""
    (docs / "doc.md").write_text(
        "Prose\n<hgroup>\n```mermaid\nBAD\n```\n</hgroup>\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    )
    proc = hook("doc.md", cwd=docs, node_path=str(env.modules))
    assert proc.returncode == 0, proc.stderr
    assert env.parsed() == ["flowchart TD\n  A --> B"]
