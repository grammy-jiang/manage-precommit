#!/usr/bin/env node
/**
 * Check that the fenced Mermaid diagrams in Markdown files parse -- no browser.
 *
 * Mermaid's documented syntax check is `mermaid.parse()`
 * (https://mermaid.js.org/config/usage#syntax-validation-without-rendering).
 * It needs a DOM only to sanitise diagram text, and LinkeDOM supplies one
 * in-process, so nothing here starts Chromium, downloads one, or writes a
 * file. The trade is coverage: a diagram that parses can still fail when it is
 * RENDERED -- a layout error, a shape one renderer rejects -- and this hook
 * cannot see that. The `mermaid` catalog entry (scripts/lint-mermaid.mjs)
 * renders every diagram with mermaid-cli, and does.
 *
 * Usage: node scripts/parse-mermaid.mjs <file.md> [more.md ...]
 * Wired up as a local pre-commit hook (see .pre-commit-config.yaml); `mermaid`
 * and `linkedom` come from its `additional_dependencies`.
 *
 * Exit 0: every diagram parsed, or there were none. Exit 1: a diagram did not
 * parse, or a mermaid fence is never closed. Exit 2: the checker itself could
 * not run, which says nothing about the diagrams.
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { delimiter, dirname } from "node:path";
import { pathToFileURL } from "node:url";

const files = process.argv.slice(2);
if (files.length === 0) process.exit(0);

// -- fences -------------------------------------------------------------------
// CommonMark fenced code blocks, as far as a diagram needs them.
//
// A run of three or more backticks or tildes opens one; only a run of the SAME
// character at least as LONG closes it; every line between is content. That
// last rule is what keeps a ```mermaid example inside a ````markdown block from
// being read as a diagram. The info string's first word is the language, and a
// backtick fence whose info string contains a backtick is not a fence at all.
// A closing fence may be indented up to three columns past the enclosing
// container -- past the container, not past the opening fence -- and followed
// only by spaces or tabs.
//
// Containers are a stack, walked the way CommonMark walks it: each line first
// has to continue every open container, innermost last -- a block quote by its
// `>` marker, a list item by being indented to its content column or blank --
// and a container the line does not continue closes, with everything inside
// it. Then the remainder may open new containers, in any nesting (`> - `, and
// `- > ` too), and what is left is the leaf: a fence, a raw HTML block, a
// heading, a thematic break, or text. A fence or HTML block closed by its
// container is what CommonMark says it is -- for a diagram, a missing closing
// fence, and it is reported as one rather than swallowing the prose after it.
//
// Two consequences worth naming. A line indented four or more columns past its
// container is an indented code block, so `    ```mermaid` at the top level is
// the literal example it looks like, while the same line under `1.` is the
// diagram. And Markdown is suspended inside a raw HTML block, so a ```mermaid
// there is text -- most often a diagram commented out with `<!-- -->`, which is
// exactly the one that is broken. See HTML_BLOCKS.
//
// Paragraphs are tracked as far as one question needs: whether the line being
// read would have to INTERRUPT one to start a block. An empty list item, an
// ordered item not numbered 1, and a kind-7 HTML tag cannot; everything else
// here can. Not modelled, and accepted: lazy paragraph continuation (a list
// item is taken to end at the first non-blank line indented short of its
// content column), link reference definitions, and tab stops inside content.
const FENCE = /^( *)(`{3,}|~{3,})(.*)$/;
const CLOSING = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
const QUOTE = /^ {0,3}>/;
// A list marker with content after it, or a bare one ending the line: an empty
// item is an item too, and the indented fence under it is inside it.
const MARKER = /^( {0,3})([-*+]|\d{1,9}[.)])(?:([ \t]+)(?=\S)|[ \t]*$)/;
// The leaf blocks that end a paragraph without opening one. A thematic break
// is tried before a list marker, since `* * *` would read as either. A setext
// underline -- a run of `=` or of `-`, one character is enough -- closes the
// paragraph above it into a heading; a lone `-` there is not an empty list
// item, since an empty item cannot interrupt a paragraph. With no paragraph
// open, a line of `=` is text and a line of `-` is whatever else it reads as.
const HEADING = /^ {0,3}#{1,6}(?:[ \t]|$)/;
const THEMATIC = /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const SETEXT = /^ {0,3}(?:=+|-+)[ \t]*$/;

// CommonMark's seven kinds of HTML block, by what ends them: the first five end
// at a marker, which may sit on the opening line; the last two end at a blank
// line. Kind 7 -- any other complete tag alone on its line -- is the one kind
// that cannot interrupt a paragraph.
const HTML_BLOCKS = [
  [/^ {0,3}<(?:pre|script|style|textarea)(?=[\s>]|$)/i, /<\/(?:pre|script|style|textarea)>/i],
  [/^ {0,3}<!--/, /-->/],
  [/^ {0,3}<\?/, /\?>/],
  [/^ {0,3}<![A-Za-z]/, />/],
  [/^ {0,3}<!\[CDATA\[/, /\]\]>/],
  [
    /^ {0,3}<\/?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hgroup|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?=[\s/>]|$)/i,
    null,
  ],
];
const HTML_TAG_LINE =
  /^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*\s*\/?>|<\/[A-Za-z][A-Za-z0-9-]*\s*>)\s*$/;

function htmlBlockStart(text, interrupting) {
  for (const [opens, end] of HTML_BLOCKS) {
    if (opens.test(text)) return { end };
  }
  return !interrupting && HTML_TAG_LINE.test(text) ? { end: null } : null;
}

// Leading whitespace as columns, a tab reaching the next stop of four -- so
// two spaces and a tab are four columns, not six. Tabs inside content stay.
function expandTabs(line) {
  const ws = /^[ \t]*/.exec(line)[0];
  return " ".repeat(widthOf(ws, 0)) + line.slice(ws.length);
}

// Columns a run of spaces and tabs spans when it starts at column `col`.
function widthOf(ws, col) {
  let width = 0;
  for (const ch of ws) width += ch === "\t" ? 4 - ((col + width) % 4) : 1;
  return width;
}

function indentOf(text) {
  return text.length - text.trimStart().length;
}

// A block-quote marker taken off the front of `text`, which starts at column
// `col`: the `>` and then its optional space. Whitespace after the marker is
// expanded at the column it really sits at, and the first column of a tab is
// the optional space while the rest of it stays as indentation -- so
// `>\t```mermaid` is a fence two columns into the quote, as the spec has it.
// Null when the text does not start with a marker.
function unquote(text, col) {
  const m = QUOTE.exec(text);
  if (m === null) return null;
  let rest = text.slice(m[0].length);
  let consumed = m[0].length;
  const ws = /^[ \t]*/.exec(rest)[0];
  rest = " ".repeat(widthOf(ws, col + consumed)) + rest.slice(ws.length);
  if (rest.startsWith(" ")) {
    rest = rest.slice(1);
    consumed += 1;
  }
  return { rest, consumed };
}

function fenceOf(text) {
  const m = FENCE.exec(text);
  if (m === null) return null;
  const [, indent, run, info] = m;
  if (run[0] === "`" && info.includes("`")) return null;
  return {
    char: run[0],
    length: run.length,
    lang: (info.trim().split(/\s+/)[0] ?? "").toLowerCase(),
    indent: indent.length,
  };
}

function closes(text, open) {
  const m = CLOSING.exec(text);
  return m !== null && m[1][0] === open.char && m[1].length >= open.length;
}

function dedent(text, width) {
  let n = 0;
  while (n < width && text[n] === " ") n++;
  return text.slice(n);
}

// Every mermaid block in `text` as {line, body}: `line` is the 1-based line of
// the opening fence, and `body` is null when that fence is never closed.
// CommonMark closes an unclosed fence at the end of its container, which for a
// diagram means "everything to the end" -- a missing closing fence, not a
// diagram anybody meant. Reported rather than parsed.
function mermaidBlocks(text) {
  // A line ends at LF, CRLF or a bare CR, as CommonMark has it.
  const lines = text.replace(/^\uFEFF/, "").split(/\r\n|\r|\n/);
  const blocks = [];
  const containers = []; // open containers, innermost last: {kind: "quote"} or {kind: "item", column}
  let open = null; // the fence being read: char, length, lang, indent, depth (containers around it), line, body
  let html = null; // the raw HTML block being skipped: what ends it, and its depth
  let paragraph = false; // whether a paragraph may be open at this line

  const leave = () => {
    if (open.lang === "mermaid") {
      blocks.push({ line: open.line, body: open.body === null ? null : open.body.join("\n") });
    }
    open = null;
    paragraph = false;
  };

  // Every container beyond the first `kept` has ended, and so has anything that
  // was open inside one of them.
  const closeBeyond = (kept) => {
    if (containers.length === kept) return;
    containers.length = kept;
    if (open !== null && open.depth > kept) {
      open.body = null;
      leave();
    }
    if (html !== null && html.depth > kept) html = null;
    paragraph = false;
  };

  lines.forEach((rawLine, i) => {
    let text = expandTabs(rawLine);
    let offset = 0; // columns of `rawLine` consumed so far, for tab stops after a marker

    // 1. The open containers this line continues, outermost first.
    let kept = 0;
    for (const container of containers) {
      if (container.kind === "quote") {
        const q = unquote(text, offset);
        if (q === null) break;
        text = q.rest;
        offset += q.consumed;
      } else if (text.trim() !== "") {
        // Lazy continuation does not reach into a code block, and is not
        // modelled elsewhere either: short of the content column, the item ends.
        if (indentOf(text) < container.column) break;
        text = text.slice(container.column);
        offset += container.column;
      }
      kept++;
    }
    closeBeyond(kept);

    if (open !== null) {
      // The closing fence is judged against the container, the content against
      // the opening fence: an opener indented two columns permits a closer at
      // three, not at five.
      if (closes(text, open)) leave();
      else open.body.push(dedent(text, open.indent));
      return;
    }
    const blank = text.trim() === "";
    if (html !== null) {
      if (html.end === null ? blank : html.end.test(text)) html = null;
      paragraph = false;
      return;
    }
    if (blank) {
      paragraph = false;
      return;
    }

    // 2. The containers this line opens, in any nesting.
    let interrupting = paragraph;
    for (;;) {
      // Indented code if no paragraph is open, a lazy continuation line if one
      // is: neither opens anything, and neither changes which of the two it was.
      if (indentOf(text) >= 4) return;
      const quote = unquote(text, offset);
      if (quote !== null) {
        containers.push({ kind: "quote" });
        text = quote.rest;
        offset += quote.consumed;
        interrupting = false;
        continue;
      }
      if (THEMATIC.test(text)) break;
      const marker = MARKER.exec(text);
      if (marker === null) break;
      const bare = marker[3] === undefined;
      const ordered = /^\d/.test(marker[2]);
      // Only an item with content may interrupt a paragraph, and an ordered
      // one only when it starts at 1; otherwise `2. ` under prose is prose.
      if (interrupting && (bare || (ordered && parseInt(marker[2], 10) !== 1))) break;
      const markerEnd = marker[1].length + marker[2].length;
      const spanned = bare ? 1 : widthOf(marker[3], offset + markerEnd);
      // Five or more columns after a marker are content, not part of it.
      const gap = spanned >= 5 ? 1 : spanned;
      containers.push({ kind: "item", column: markerEnd + gap });
      interrupting = false;
      paragraph = false;
      if (bare) return; // an empty item: nothing else on the line, and no paragraph
      if (spanned - gap >= 4) return; // indented code on the marker line
      text = " ".repeat(spanned - gap) + text.slice(marker[0].length);
      offset += markerEnd + gap;
    }

    // 3. The leaf.
    paragraph = true; // an ordinary line of text, unless something below says otherwise
    if (HEADING.test(text) || THEMATIC.test(text) || (interrupting && SETEXT.test(text))) {
      paragraph = false;
      return;
    }
    const block = htmlBlockStart(text, interrupting);
    if (block !== null) {
      if (block.end === null || !block.end.test(text)) html = { ...block, depth: containers.length };
      paragraph = false;
      return;
    }
    const fence = fenceOf(text);
    if (fence !== null) {
      open = { ...fence, depth: containers.length, line: i + 1, body: [] };
      paragraph = false;
    }
  });

  if (open !== null) {
    open.body = null;
    leave();
  }
  return blocks;
}

// -- mermaid ------------------------------------------------------------------
// pre-commit installs a node hook's dependencies into an environment of its own
// and exposes them through NODE_PATH -- which ES module `import` ignores, so
// they are found with a CommonJS resolver built for this file and then imported
// by absolute URL. NODE_PATH is searched FIRST, ahead of anything the
// repository itself keeps in node_modules: the version that checks the diagrams
// should be the one .pre-commit-config.yaml pins, not whatever the project
// happens to depend on. Outside pre-commit, with no NODE_PATH, ordinary
// resolution applies and `npm install mermaid linkedom` is enough.
function locate(request) {
  const require = createRequire(import.meta.url);
  const roots = (process.env.NODE_PATH ?? "")
    .split(delimiter)
    .filter(Boolean)
    .map((dir) => dirname(dir));
  return require.resolve(request, roots.length > 0 ? { paths: roots } : undefined);
}

async function loadMermaid() {
  const require = createRequire(import.meta.url);
  const { parseHTML } = require(locate("linkedom"));
  const { window } = parseHTML("<!doctype html><html><head></head><body></body></html>");
  // Mermaid sanitises labels through DOMPurify, which wants a window and a
  // document to exist. Nothing is drawn into them: `suppressErrorRendering`
  // keeps a failed parse from being rendered into the body as an error box.
  globalThis.window = window;
  globalThis.document = window.document;
  const { default: mermaid } = await import(pathToFileURL(locate("mermaid")).href);
  mermaid.initialize({ startOnLoad: false, suppressErrorRendering: true });
  return mermaid;
}

// -- run ----------------------------------------------------------------------
const work = [];
for (const file of files) {
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch {
    continue; // deleted/unreadable -- not this hook's concern
  }
  const blocks = mermaidBlocks(text);
  if (blocks.length > 0) work.push({ file, blocks });
}
if (work.length === 0) process.exit(0);

const failures = [];
let mermaid = null;
for (const { file, blocks } of work) {
  for (const { line, body } of blocks) {
    if (body === null) {
      failures.push({ file, line, detail: "the mermaid fence opened here is never closed" });
      continue;
    }
    if (mermaid === null) {
      // Loaded once, and only once a complete diagram exists: Markdown with no
      // diagrams, or only an unclosed fence, never pays the startup cost.
      try {
        mermaid = await loadMermaid();
      } catch (err) {
        console.error(`mermaid-parse: could not load mermaid and linkedom: ${err?.message ?? err}`);
        console.error(
          "\nThe diagrams were never parsed, so this says nothing about them. Under " +
            "pre-commit both packages come from this hook's additional_dependencies, and " +
            "`pre-commit clean` rebuilds that environment. Run by hand, they have to be " +
            "installed where node can find them: `npm install mermaid linkedom`.",
        );
        process.exit(2);
      }
    }
    try {
      await mermaid.parse(body);
    } catch (err) {
      const detail = String(err?.message ?? err).trim();
      failures.push({ file, line, detail: detail || "mermaid rejected the diagram without saying why" });
    }
  }
}

if (failures.length > 0) {
  for (const { file, line, detail } of failures) {
    console.error(`\n✖ ${file}:${line}\n${detail.replace(/^/gm, "  ")}`);
  }
  const touched = new Set(failures.map((f) => f.file)).size;
  console.error(
    `\nmermaid-parse: ${failures.length} problem(s) in ${touched} file(s). ` +
      "A line number inside a Mermaid message counts from the top of that diagram, not of the file.",
  );
  process.exit(1);
}
process.exit(0);
