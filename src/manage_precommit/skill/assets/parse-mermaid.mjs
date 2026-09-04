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
// Containers are followed as far as they change what a fence is:
//
// - Block quotes. `> ` markers are stripped before a line is read, and a fence
//   opened inside a quote lives inside it: a line with fewer markers ends the
//   quote, and CommonMark closes the fence with its container -- which for a
//   diagram is a missing closing fence, reported as such.
// - List items. A marker line sets the column its content starts at, and a
//   line indented four or more columns past that is an indented code block, not
//   a fence. So `    ```mermaid` at the top level is a literal example and
//   stays text, while the same line under `1.` is the diagram it looks like. A
//   fence may also open on the marker line itself (`- ```mermaid`). Lazy
//   paragraph continuation is not modelled: a list is taken to end at the
//   first non-blank line indented short of its content column.
// - Raw HTML blocks. Markdown is suspended inside one, so a ```mermaid there is
//   text -- most often a diagram commented out with `<!-- -->`, which is
//   exactly the one that is broken. See HTML_BLOCKS.
//
// The opening fence's indentation -- or the marker's content column -- is
// removed from each content line, as CommonMark removes it.
const FENCE = /^( *)(`{3,}|~{3,})(.*)$/;
const CLOSING = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
const QUOTE = /^ {0,3}> ?/;
const MARKER = /^( {0,3})([-*+]|\d{1,9}[.)])( +)(?=\S)/;

// CommonMark's seven kinds of HTML block, by what ends them: the first five end
// at a marker, which may sit on the opening line; the last two end at a blank
// line. Kind 7 -- any other complete tag alone on its line -- cannot interrupt
// a paragraph, and paragraphs are not tracked here, so it is honoured only
// where no paragraph can be in progress: after a blank line, or at the start
// of the document or container.
const HTML_BLOCKS = [
  [/^ {0,3}<(?:pre|script|style|textarea)(?=[\s>]|$)/i, /<\/(?:pre|script|style|textarea)>/i],
  [/^ {0,3}<!--/, /-->/],
  [/^ {0,3}<\?/, /\?>/],
  [/^ {0,3}<![A-Za-z]/, />/],
  [/^ {0,3}<!\[CDATA\[/, /\]\]>/],
  [
    /^ {0,3}<\/?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?=[\s/>]|$)/i,
    null,
  ],
];
const HTML_TAG_LINE =
  /^ {0,3}(?:<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*\s*\/?>|<\/[A-Za-z][A-Za-z0-9-]*\s*>)\s*$/;

function htmlBlockStart(text, afterBlank) {
  for (const [opens, end] of HTML_BLOCKS) {
    if (opens.test(text)) return { end };
  }
  return afterBlank && HTML_TAG_LINE.test(text) ? { end: null } : null;
}

// Leading tabs count as four columns; nothing here needs real tab stops.
function expandTabs(line) {
  return line.replace(/^[ \t]*/, (ws) => ws.replace(/\t/g, "    "));
}

function indentOf(text) {
  return text.length - text.trimStart().length;
}

// Up to `limit` block-quote markers taken off the front of a line (all of them
// when `limit` is Infinity), and how many that was.
function unquote(line, limit) {
  let text = line;
  let depth = 0;
  while (depth < limit) {
    const m = QUOTE.exec(text);
    if (m === null) break;
    text = text.slice(m[0].length);
    depth++;
  }
  return { text, depth };
}

function fenceOf(text) {
  const m = FENCE.exec(text);
  if (m === null) return null;
  const [, , run, info] = m;
  if (run[0] === "`" && info.includes("`")) return null;
  return {
    char: run[0],
    length: run.length,
    lang: (info.trim().split(/\s+/)[0] ?? "").toLowerCase(),
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
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/);
  const blocks = [];
  let open = null; // the fence being read: char, length, lang, depth, container, column, line, body
  let html = null; // the raw HTML block being skipped, and what ends it
  let columns = []; // content columns of the list items enclosing this line
  let quoted = 0; // the quote depth those columns were measured at
  let afterBlank = true; // whether a paragraph could be in progress

  const leave = () => {
    if (open.lang === "mermaid") {
      blocks.push({ line: open.line, body: open.body === null ? null : open.body.join("\n") });
    }
    open = null;
  };

  lines.forEach((rawLine, i) => {
    const raw = expandTabs(rawLine);
    if (open !== null) {
      const { text: inner, depth } = unquote(raw, open.depth);
      // Still inside the containers the fence was opened under: as many quote
      // markers, and -- unless blank -- indented at least to the list item's
      // content column. Lazy continuation does not reach into a code block.
      const inside =
        depth === open.depth && (inner.trim() === "" || indentOf(inner) >= open.container);
      if (inside) {
        // The closing fence is judged against the container, the content
        // against the opening fence: an opener indented two columns permits a
        // closer at three, not at five.
        if (closes(dedent(inner, open.container), open)) {
          leave();
        } else {
          open.body.push(dedent(inner, open.column));
        }
        return;
      }
      // Fewer quote markers than the fence was opened under, or indented short
      // of the list item it lives in: the container has ended, and the fence
      // with it, unclosed -- an unindented ``` under `- item` opens a new fence
      // at the top level rather than closing the nested one, which is what
      // GitHub renders too. The line is then read afresh.
      open.body = null;
      leave();
    }

    const { text: unquoted, depth } = unquote(raw, Infinity);
    if (depth !== quoted) {
      columns = [];
      html = null;
      quoted = depth;
      afterBlank = true;
    }
    const blank = unquoted.trim() === "";
    if (html !== null) {
      if (html.end === null ? blank : html.end.test(unquoted)) html = null;
      afterBlank = blank;
      return;
    }
    if (blank) {
      afterBlank = true; // blank lines leave lists as they are
      return;
    }

    const indent = indentOf(unquoted);
    while (columns.length > 0 && indent < columns[columns.length - 1]) columns.pop();
    const container = columns.length > 0 ? columns[columns.length - 1] : 0;
    const wasAfterBlank = afterBlank;
    afterBlank = false;
    if (indent - container >= 4) return; // an indented code block, whatever it contains

    // The line relative to its container: its own indentation (at most three
    // columns) and the text, with any list marker it opens taken off first.
    let column = indent;
    let relative = dedent(unquoted, container);
    const marker = MARKER.exec(unquoted);
    if (marker !== null) {
      // Five or more spaces after a marker are content, not part of it.
      const gap = marker[3].length >= 5 ? 1 : marker[3].length;
      column = indent + marker[2].length + gap;
      columns.push(column);
      relative = unquoted.slice(column);
      if (indentOf(relative) >= 4) return; // indented code on the marker line
    }
    const block = htmlBlockStart(relative, wasAfterBlank);
    if (block !== null) {
      html = block.end !== null && block.end.test(relative) ? null : block;
      return;
    }
    const fence = fenceOf(relative);
    if (fence !== null) {
      open = { ...fence, depth, container: marker !== null ? column : container, column, line: i + 1, body: [] };
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
