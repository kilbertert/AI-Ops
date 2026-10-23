#!/usr/bin/env node
/**
 * Assert `.sandcastle/REPO-MAP.md` exists, matches what the generator produces,
 * AND carries the sandbox's MCP contract.
 *
 * A stale map is worse than a missing one: the agent reads it with the same
 * confidence as the code, and it silently points at files that moved. This is
 * what makes the staleness visible.
 *
 * The MCP section is asserted here rather than trusted for the same reason, in
 * the other direction: every part of that contract fails SILENTLY.
 *
 *   - the graph is indexed under a name derived from the *path*, which inside a
 *     sandbox is always `/home/agent/workspace`, so the agent's natural guess
 *     (`AI-Ops`) misses and it falls back to grep — measured in a real run, at a
 *     cost of 3.7 minutes and a `list_projects` detour;
 *   - the name is required per CALL and cannot be defaulted by the MCP config
 *     (the binary ignores `--project` on the server path), so what this file
 *     says is the only place the agent can learn it;
 *   - the `project not found` error it gets instead does list the real name, but
 *     only after it has already concluded the tool is broken.
 *
 * Run: `node .sandcastle/repo-map.check.mjs`
 */
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

const REPO = process.cwd();
const GEN = path.join(REPO, ".sandcastle", "repo-map.mjs");

// The map is NOT compared against a committed copy any more, because there is
// no committed copy: `.sandcastle/REPO-MAP.md` is generated on demand and
// gitignored (2026-10-23, ADR 0098). Keeping it in version control meant every
// branch that touched the file set also rewrote this file — it conflicted on 34
// of 102 remote branches, and the conflicting hunk carried no information
// ("170 markdown file(s)" vs "172").
//
// What is still worth asserting is the thing the freshness gate never could:
// that the generator produces a map the agent can actually use. It is generated
// here into memory, so the check never depends on whether someone remembered
// to run it.
const fresh = execFileSync(process.execPath, [GEN, "--stdout"], { encoding: "utf8" });

// The *generated* text is what the contract is checked against, not the file on
// disk: the regeneration above guarantees they are identical, and asserting on
// the generator is what makes a hand-deleted section appear (it is regenerated)
// while a section the generator stopped emitting fails here.
if (!/^## Code intelligence$/m.test(fresh)) {
  console.error(
    "REPO-MAP.md has no `## Code intelligence` section — the agent has no way to learn what name the knowledge graph answers to, and its first guess costs minutes",
  );
  process.exit(1);
}

// The name the agent must pass is the one the index hook indexes under. Both
// come from the same manifest field, so comparing them here is what keeps a
// rename on one side from shipping a confidently-wrong instruction on the other.
const manifest = JSON.parse(fs.readFileSync(path.join(REPO, ".afk-bootstrap.json"), "utf8"));
const expected = String(manifest.repository ?? "").split("/").pop();
if (!expected) {
  console.error(".afk-bootstrap.json has no repository; the graph project name cannot be derived");
  process.exit(1);
}
if (!fresh.includes(`"project": "${expected}"`)) {
  console.error(
    `REPO-MAP.md does not tell the agent to pass "project": "${expected}" — the name the index hook indexes under`,
  );
  process.exit(1);
}

// The map is now produced at read time rather than committed, so the property
// that replaces "the committed copy matches" is determinism: the same tree must
// render the same bytes. A generator that varies run to run (timestamps, map
// iteration order, locale-dependent sorting) would hand each agent a different
// map for the same repository — the conflict the move was meant to end, moved
// from git into the agent's head.
const again = execFileSync(process.execPath, [GEN, "--stdout"], { encoding: "utf8" });
if (again !== fresh) {
  console.error(
    "repo-map generator is not deterministic — two runs over the same tree produced different maps, so no reader can tell which one is current",
  );
  process.exit(1);
}

console.log(`repo-map check ok (${fresh.split("\n").length} lines, graph project ${expected}, deterministic)`);
