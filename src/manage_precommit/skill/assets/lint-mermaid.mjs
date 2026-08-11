#!/usr/bin/env node
/**
 * Validate embedded Mermaid diagrams in Markdown files.
 *
 * Why a script: Mermaid ships no lightweight offline linter. Its real parser
 * (flowchart, sequence, class, ...) only runs in a browser, so we shell out to
 * mermaid-cli (`mmdc`), which renders each fenced ```mermaid block with headless
 * Chromium and exits non-zero on a parse/render error. Rendering goes to a
 * throwaway temp dir -- nothing is written into the repo.
 *
 * Usage: node scripts/lint-mermaid.mjs <file.md> [more.md ...]
 * Wired up as a local pre-commit hook (see .pre-commit-config.yaml); mmdc is
 * provided there via `additional_dependencies`. Override the binary with $MMDC.
 *
 * Set MERMAID_LINT_NO_SANDBOX=1 where Chromium's sandbox cannot start -- CI
 * runners, containers, and distros that restrict unprivileged user namespaces
 * (Ubuntu 23.10+ under AppArmor). Opt-in, never automatic: the sandbox is a
 * real boundary around a browser rendering text out of the repository, and
 * dropping it silently on everyone's machine to make one environment work is
 * not a trade this hook gets to make for you.
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const files = process.argv.slice(2);
if (files.length === 0) process.exit(0);

// Only files that actually contain a mermaid fence -- avoids launching Chromium
// for Markdown with no diagrams.
const FENCE = /^[ \t]*(`{3,}|~{3,})\s*mermaid\b/im;
const targets = files.filter((file) => {
  try {
    return FENCE.test(readFileSync(file, "utf8"));
  } catch {
    return false; // deleted/unreadable -- not this hook's concern
  }
});
if (targets.length === 0) process.exit(0);

// Keep the human-readable Mermaid parse error; drop mmdc's JS stack trace.
function tidy(stderr) {
  const lines = stderr.split("\n");
  const cut = lines.findIndex(
    (l) => /^\s+at\s/.test(l) || /Parser\.parseError\s*\(/.test(l),
  );
  return (cut === -1 ? lines : lines.slice(0, cut)).join("\n").trim();
}

// Chromium never started. That is not a diagram error, and reporting it as one
// sends somebody to rewrite a diagram that was always valid.
const LAUNCH_FAILED =
  /Failed to launch the browser process|No usable sandbox|Could not find (Chrome|Chromium)|Browser was not found|Running as root without --no-sandbox/i;

const mmdc = process.env.MMDC || "mmdc";
const work = mkdtempSync(join(tmpdir(), "mermaid-lint-"));
const args = ["--quiet"];

if (process.env.MERMAID_LINT_NO_SANDBOX === "1") {
  const config = join(work, "puppeteer.json");
  writeFileSync(
    config,
    JSON.stringify({ args: ["--no-sandbox", "--disable-setuid-sandbox"] }),
  );
  args.push("--puppeteerConfigFile", config);
}

const failures = [];
let launchError = null;

for (const file of targets) {
  try {
    execFileSync(
      mmdc,
      [...args, "--input", file, "--output", join(work, "out.md")],
      { stdio: ["ignore", "ignore", "pipe"] },
    );
  } catch (err) {
    if (err.code === "ENOENT") {
      rmSync(work, { recursive: true, force: true });
      console.error(
        `mermaid-lint: could not run "${mmdc}". Install @mermaid-js/mermaid-cli ` +
          "or set $MMDC. (Under pre-commit this is provided automatically.)",
      );
      process.exit(2);
    }
    const detail = tidy(err.stderr?.toString() ?? "") || err.message;
    if (LAUNCH_FAILED.test(detail)) {
      // Every file would fail the same way, so stop rather than report the
      // same environment problem once per Markdown file.
      launchError = detail;
      break;
    }
    failures.push({ file, detail });
  }
}

rmSync(work, { recursive: true, force: true });

if (launchError !== null) {
  console.error(`\nmermaid-lint: the browser could not start.\n${launchError}`);
  console.error(
    "\nThe diagrams were never parsed, so this says nothing about them.\n" +
      (process.env.MERMAID_LINT_NO_SANDBOX === "1"
        ? "MERMAID_LINT_NO_SANDBOX is already set, so this is something else -- " +
          "usually a missing Chromium. `npx puppeteer browsers install chrome` installs one."
        : "In CI, a container, or on a distro that restricts unprivileged user " +
          "namespaces, set MERMAID_LINT_NO_SANDBOX=1 to run Chromium without its " +
          "sandbox. That weakens a real boundary around untrusted repository " +
          "content, which is why it is opt-in."),
  );
  process.exit(2);
}

if (failures.length > 0) {
  for (const { file, detail } of failures) {
    console.error(`\n✖ ${file}\n${detail}`);
  }
  console.error(
    `\nmermaid-lint: ${failures.length} file(s) with invalid diagram(s).`,
  );
  process.exit(1);
}
