#!/usr/bin/env node
/**
 * Report every tracked file that more than one branch has to write, worst first.
 *
 * Why this exists: "we fixed the conflicts" is a claim that cannot be checked
 * by reading a diff. What can be checked is the shape that *causes* a conflict
 * — a file that many independent branches modify — so this enumerates that
 * shape against the live remote branches instead of against a memory of them.
 *
 * It is a report, not a gate. A file with two writers is not automatically
 * wrong: `README.md` legitimately changes when a milestone changes how the
 * project is used. The point is that the list is *visible and short*, and that
 * every entry has a stated reason it must be shared. A gate would force the
 * honest entries to be waived, and a waivered gate stops being read.
 *
 * Usage:
 *   node .sandcastle/conflict-surface.mjs              # live remote branches
 *   node .sandcastle/conflict-surface.mjs --base main  # explicit base
 *   node .sandcastle/conflict-surface.mjs --json
 *   node .sandcastle/conflict-surface.mjs --check      # exit 1 if a zero-writer
 *                                                      # file has become shared
 *
 * Exit codes: 0 report produced (--check: no new structural conflicts),
 *             1 --check found a file that should have zero writers with more,
 *             2 cannot run (no git, no remote, bad argument).
 */
import { execFileSync } from "node:child_process";

const args = process.argv.slice(2);
const BASE = valueOf("--base") ?? "origin/main";
const AS_JSON = args.includes("--json");
const CHECK = args.includes("--check");

/**
 * Every path that must never have two concurrent writers, with the disposition
 * that makes it so. These are the entries `--check` gates on. A path is listed
 * here only when the reason is structural — generated from the tree, or a
 * single-valued fact. "We would prefer it weren't shared" does not belong here;
 * that is what the report half is for.
 *
 * `disposition` is what was actually done about it:
 *   removed     — out of version control, so there is no copy to diverge
 *   exclusive   — stays committed, but only one kind of change may write it
 *                 (a build, a version bump), and that change is serial by nature
 */
const STRUCTURAL = new Map([
  [
    ".sandcastle/REPO-MAP.md",
    {
      disposition: "removed",
      why: "a pure function of the tracked file set; gitignored and generated on demand",
      since: "2026-10-23",
    },
  ],
  [
    "deploy/m2-live-03/images.lock.json",
    {
      disposition: "exclusive",
      why: "records the identity of a built artifact; only `build-image.sh --update-lock` writes it, and a build is already serial",
      since: "2026-10-23",
    },
  ],
  [
    ".afk-bootstrap.json",
    {
      disposition: "exclusive",
      why: "one template version per repository; a bump is a single deliberate act, and two queued bumps must not race",
      since: "2026-10-23",
    },
  ],
]);

/**
 * Hand-written files that more than one branch legitimately writes. Each needs a
 * stated reason, because "it is shared" and "it should be shared" are different
 * claims and only the second one is acceptable.
 *
 * A path absent from this map that shows up with two or more writers is
 * reported as `undecided` — the list is meant to be exhaustive, and an
 * undecided entry is the thing that says it currently is not.
 */
const EXPLAINED_SHARED = new Map([
  ["docs/validation.md", { why: "frozen archive; no new entries are written to it since 2026-10-23" }],
  ["docs/开发进度.md", { why: "frozen archive; no new entries are written to it since 2026-10-23" }],
  [
    "docs/agents/current-delivery-state.md",
    {
      why: "the one page that answers \"where are we now\"; it is an index by design, so it is the last hand-written top-insertion file left",
      undecided: true,
    },
  ],
  [
    ".sandcastle/implement.md",
    {
      why: "written only by an AFK scaffold upgrade, which is version-anchored and applied one version at a time",
      serial: "afk-upgrade",
    },
  ],
  [
    ".sandcastle/implement-prompt.md",
    {
      why: "written only by an AFK scaffold upgrade, which is version-anchored and applied one version at a time",
      serial: "afk-upgrade",
    },
  ],
  [
    ".sandcastle/implement/prompt.md",
    {
      why: "written only by an AFK scaffold upgrade, which is version-anchored and applied one version at a time",
      serial: "afk-upgrade",
    },
  ],
  [
    ".sandcastle/implement-prd/prompt.md",
    {
      why: "written only by an AFK scaffold upgrade, which is version-anchored and applied one version at a time",
      serial: "afk-upgrade",
    },
  ],
  [
    ".sandcastle/implement-pr/prompt.md",
    {
      why: "written only by an AFK scaffold upgrade, which is version-anchored and applied one version at a time",
      serial: "afk-upgrade",
    },
  ],
  ["README.md", { why: "a milestone that changes a user entry point, config or deployment must update it" }],
  [
    "CHANGELOG.md",
    {
      why: "generated from Conventional Commits, but still hand-edited and committed — the next candidate for `removed`, and the only file here whose conflicts have no information value at all",
      undecided: true,
    },
  ],
]);

const HOTSPOT_THRESHOLD = 3;

/**
 * Classify a hand-written path by *why* two branches can land on it.
 *
 * This is the axis that decides whether anything can be done:
 *
 *   `prose`    — narrative files whose writers add different facts at the same
 *                place (an entry list, a runbook section, a feature file step).
 *                Splitting per entry removes the shared write entirely.
 *   `source`   — a module two changes both had to edit. Not a defect: the
 *                branches are not disagreeing, they are completing each other.
 *                A source file is one concept with one home; splitting it to
 *                dodge conflicts would be inventing boundaries for git.
 *
 * A prose file with a high writer count is a hotspot — its conflicts are about
 * *format*, and format is fixable. Source is left to scheduling: at most one
 * author may own a module's rewrite, but two additive edits are just a rebase.
 */
function classify(path) {
  // Lockfiles are generated wholesale by the package manager, so two branches
  // that both resolve dependencies always rewrite the same region. They behave
  // like source for the purpose of "you cannot split it", not like prose.
  if (path === "uv.lock" || path.endsWith("package-lock.json") || path.endsWith("pnpm-lock.yaml")) {
    return "source";
  }
  if (path.endsWith(".md") || path.endsWith(".feature") || path.endsWith(".json")) return "prose";
  return "source";
}

/**
 * Prose files that are live scenario and acceptance documents rather than
 * append-only logs: milestones extend them in place, anywhere in the file.
 *
 * These are the hardest case and are recorded honestly rather than fixed.
 * `acceptance.feature` took 17 commits in the last seven days spread through the
 * file, not stacked at one end, so there is no insertion point to split on —
 * splitting a scenario file per entry would break the thing that makes it
 * useful (one readable end-to-end suite). The disposition is `serial`: a
 * change that reworks a scenario gets the file to itself.
 */
const IN_PLACE_DOCS = new Map([
  ["acceptance.feature", { why: "one end-to-end scenario suite, extended in place; split per entry would cost the readability that is its point", serial: "scenario" }],
  ["qa-plan.md", { why: "mirrors the scenario suite one-to-one; same reasoning", serial: "scenario" }],
  ["docs/agents/env-41-runbook.md", { why: "an operational manual read top to bottom; a new section belongs where it is read, not in a separate file", serial: "runbook" }],
  ["GLOSSARY.md", { why: "one definition per term, sorted; two renames of different terms rarely land on the same line", serial: "glossary" }],
  [
    "ops/dify-console-bridge/README.md",
    {
      why: "one component's operator README; one commit in the last seven days, so the conflicts are finished branches, not live contention",
      serial: "component-readme",
    },
  ],
  [
    "docs/adr/0033-consumer-charging-status.md",
    {
      why: "its 落地情况 sections append at the same point, so it conflicts exactly like the two evidence logs did — the fix is to move further 落地 into new evidence records, not to keep appending here",
      serial: "adr",
    },
  ],
]);

// ── Self-check ───────────────────────────────────────────────────────────────
// The report's whole claim is "every shared path has been decided". That claim
// is only worth reading if the three tables below are disjoint and complete, so
// a path listed in two of them would be silently double-counted and a
// disposition nobody handles would vanish from the tally. This runs before any
// git call so a broken table fails in a second rather than after a full sweep.
if (process.argv.includes("--self-check")) {
  const names = (map) => new Set(map.keys());
  const overlap = (a, b) =>
    [...names(a)].filter((key) => names(b).has(key));
  const collisions = [
    ...overlap(STRUCTURAL, EXPLAINED_SHARED),
    ...overlap(STRUCTURAL, IN_PLACE_DOCS),
    ...overlap(EXPLAINED_SHARED, IN_PLACE_DOCS),
  ];
  if (collisions.length > 0) {
    console.error(`a path is classified twice: ${collisions.join(", ")}`);
    process.exit(1);
  }
  for (const [name, map] of [
    ["STRUCTURAL", STRUCTURAL],
    ["EXPLAINED_SHARED", EXPLAINED_SHARED],
    ["IN_PLACE_DOCS", IN_PLACE_DOCS],
  ]) {
    for (const [path, entry] of map) {
      if (!entry.why) {
        console.error(`${name}: ${path} has no \`why\` — an unexplained disposition is an opinion`);
        process.exit(1);
      }
    }
  }
  console.log(
    `conflict-surface self-check ok (${STRUCTURAL.size} structural, ${EXPLAINED_SHARED.size} shared, ${IN_PLACE_DOCS.size} serial)`,
  );
  process.exit(0);
}

/** Largest writer count a hand-written file may have before it counts as a hotspot. */

function valueOf(flag) {
  const index = args.indexOf(flag);
  return index === -1 ? undefined : args[index + 1];
}

function git(argv) {
  return execFileSync("git", argv, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

let baseSha;
try {
  baseSha = git(["rev-parse", "--verify", `${BASE}^{commit}`]);
} catch {
  console.error(`cannot resolve base \`${BASE}\` — fetch the remote first`);
  process.exit(2);
}

// Every remote branch that still exists. Local-only branches are excluded
// deliberately: an abandoned local branch is not a concurrent writer.
let branches;
try {
  branches = git(["for-each-ref", "--format=%(refname:short)", "refs/remotes"])
    .split("\n")
    .filter((name) => name && !name.endsWith("/HEAD"));
} catch {
  console.error("cannot list remote branches");
  process.exit(2);
}

/** path -> Set(branch) for every branch whose merge against the base conflicts. */
const conflicts = new Map();
let examined = 0;
let unreadable = 0;

for (const branch of branches) {
  let out;
  try {
    // `--write-tree` exits non-zero on conflict and prints the conflicted
    // paths after the tree id. The hooks are disabled because a repository
    // policy hook has no business running during a read-only measurement.
    out = execFileSync("git", ["-c", "core.hooksPath=/dev/null", "-c", "core.quotepath=off", "merge-tree", "--write-tree", "--name-only", baseSha, branch], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    out = String(error.stdout ?? "");
    if (!out) {
      unreadable += 1;
      continue;
    }
  }
  examined += 1;

  // The first line is the tree oid; the conflicted paths follow, one per line,
  // terminated by a blank line. `--name-only` still prints `Auto-merging`
  // notices for the files it resolved, and those are NOT conflicts.
  const lines = out.split("\n");
  let started = false;
  for (const line of lines) {
    if (!started) {
      started = true; // the tree oid
      continue;
    }
    if (line === "") break;
    const path = line.trim();
    if (!path || path.startsWith("Auto-merging") || path.startsWith("CONFLICT")) continue;
    if (!conflicts.has(path)) conflicts.set(path, new Set());
    conflicts.get(path).add(branch);
  }
}

const ranked = [...conflicts.entries()]
  .map(([path, set]) => ({ path, branches: set.size }))
  .sort((a, b) => b.branches - a.branches || a.path.localeCompare(b.path));

const structural = ranked.filter((entry) => STRUCTURAL.has(entry.path));

if (CHECK) {
  // The question this gate asks is NOT "is the repository conflict-free" — it
  // never is, and a check that says so is a check nobody can act on. It asks
  // the narrower thing a `removed` disposition actually promises:
  //
  //   is a path that was taken out of version control still out of it?
  //
  // A generated file that is back in the tree at the merge base means the
  // removal was reverted, a copy was committed again, or the gitignore entry
  // was dropped — all three are regressions of a decision, and all three are
  // fixable by the person reading the failure.
  //
  // Conflicting *branches* are deliberately not part of this. A branch created
  // before the removal keeps its tracked copy until it is merged or deleted;
  // that is a fact about that branch's age, not about the repository today.
  // Measured on the PR that introduced this gate: gating on live branches made
  // it fail the very change that removes the file.
  // The question is asked of the tree that is about to be merged, not of the
  // merge base. Asking the base would fail every pull request for as long as
  // the base predates the removal, which is exactly backwards: the branch that
  // carries the removal is the one that should pass.
  const stillTracked = [];
  for (const [path, entry] of STRUCTURAL) {
    if (entry.disposition !== "removed") continue;
    let tracked;
    try {
      // `git ls-files` reads the index of the checkout being tested — the
      // branch under review in CI. Non-zero exit means "not tracked", which is
      // the state a removed path is supposed to be in.
      tracked = execFileSync("git", ["ls-files", "--error-unmatch", "--", path], {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
    } catch {
      tracked = "";
    }
    if (tracked) stillTracked.push({ path, why: entry.why });
  }

  if (stillTracked.length > 0) {
    for (const { path, why } of stillTracked) {
      console.error(`${path} is still tracked in this tree but was removed from version control — ${why}`);
    }
    process.exit(1);
  }

  console.log(
    `conflict surface: every removed path is still untracked (${STRUCTURAL.size} disposition(s),
     ${[...STRUCTURAL.values()].filter((e) => e.disposition === "removed").length} removed)`,
  );
  process.exit(0);
}

if (AS_JSON) {
  console.log(
    JSON.stringify(
      {
        examined,
        unreadable,
        total: ranked.length,
        ranked: ranked.map((entry) => ({
          ...entry,
          disposition: STRUCTURAL.get(entry.path)?.disposition ?? (EXPLAINED_SHARED.has(entry.path) ? "shared" : "undecided"),
        })),
        structural,
      },
      null,
      2,
    ),
  );
  process.exit(0);
}

// The report is a table with one row per conflicting path and an explicit
// disposition, because the claim being made is "every shared path has been
// decided", and a claim like that is only checkable if it is literal.
console.log(`conflict surface over ${examined} remote branch(es) against ${BASE} (${unreadable} unreadable)`);
console.log(`${ranked.length} path(s) conflict on at least one branch\n`);

const tally = { removed: 0, exclusive: 0, shared: 0, source: 0, serial: 0, hotspot: 0 };
const hotspots = [];

/** One line per path: what it is, who writes it, and what was decided. */
for (const entry of ranked) {
  const structuralEntry = STRUCTURAL.get(entry.path);
  const explained = EXPLAINED_SHARED.get(entry.path);
  const inPlace = IN_PLACE_DOCS.get(entry.path);

  let disposition;
  let note;
  if (structuralEntry) {
    disposition = structuralEntry.disposition;
    note = structuralEntry.why;
  } else if (inPlace) {
    // Recorded as serial rather than fixed, and that is the honest answer: the
    // conflict is real, the file earns its shape, so the cost is paid in
    // scheduling instead of in file layout.
    disposition = "serial";
    note = inPlace.why;
  } else if (explained) {
    disposition = "shared";
    note = explained.why;
  } else if (classify(entry.path) === "source") {
    // Two branches edited the same module because both had work in it. That is
    // concurrency, not a format defect, and there is nowhere else for the code
    // to live. Kept out of the hotspot count on purpose: counting it would make
    // the number grow with the repository and stop meaning anything.
    disposition = "source";
    note = "additive edits to one module; resolved by rebasing, not by splitting";
  } else if (entry.branches >= HOTSPOT_THRESHOLD) {
    disposition = "hotspot";
    note = `${entry.branches} writers all inserting into the same place — split per entry or freeze it`;
    hotspots.push(entry.path);
  } else {
    disposition = "shared";
    note = "two or fewer writers; below the hotspot threshold";
  }

  tally[disposition] += 1;
  console.log(`${disposition.padEnd(10)} ${String(entry.branches).padStart(3)}  ${entry.path}`);
  console.log(`           └ ${note}`);
}

console.log(
  `\n${tally.removed} removed from version control · ${tally.exclusive} single-writer by design · ` +
    `${tally.shared} shared below the threshold · ${tally.serial} serial by design`,
);
console.log(
  `${tally.source} source modules with concurrent edits (rebase, never split) · ` +
    `${tally.hotspot} prose hotspots at ${HOTSPOT_THRESHOLD}+ writers`,
);
if (hotspots.length > 0) {
  console.log("\nprose hotspots still to decide:");
  for (const path of hotspots) console.log(`  ${path}`);
}
process.exit(0);
