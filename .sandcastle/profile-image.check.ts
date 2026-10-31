/**
 * Runnable regression check for the sandbox image wiring.
 *
 * Run: `npx tsx .sandcastle/profile-image.check.ts`
 *
 * The image a project runs in is described in two places that must agree:
 *
 *   `.sandcastle/profile.ts`                what the sandbox asks for
 *   `.github/workflows/sandbox-image.yml`   what CI publishes, and with which uid
 *
 * They drifted apart once already, in the most expensive way available: the
 * published side did not exist at all, and the asking side named a tag only a
 * manual `docker build` could satisfy. Nothing noticed for two weeks, because
 * the mismatch is invisible until a review job starts a container — and then it
 * reports `Image ... not found locally` several steps in, which reads as a
 * runner or code problem.
 *
 * This check reads both files and fails on any disagreement. It deliberately
 * parses rather than importing `profile.ts`: importing it needs the project's
 * node_modules and the full provider graph, which a policy job should not pay
 * for. The trade-off is that the text shapes below are load-bearing — the check
 * says so, and fails loudly if a shape moves rather than silently passing.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

const root = join(import.meta.dirname, "..");
const profilePath = join(root, ".sandcastle/profile.ts");
const workflowPath = join(root, ".github/workflows/sandbox-image.yml");

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(`profile-image check failed: ${message}`);
}

const profile = readFileSync(profilePath, "utf8");

// 1. The image name is a published one. A `sandcastle:<repo>` value here means
//    the project is back on an artifact only this host can create.
const imageMatch = profile.match(/imageName:\s*\n?\s*process\.env\.AFK_IMAGE \?\?\s*\n?\s*"([^"]+)"/);
assert(imageMatch !== null, "profile.ts no longer has an `AFK_IMAGE ?? \"...\"` imageName");
const imageName = imageMatch![1];
assert(/^[a-z0-9./-]+:[A-Za-z0-9_.-]+$/.test(imageName), `imageName "${imageName}" is not a registry reference with a tag`);
assert(imageName.startsWith("ghcr.io/"), `imageName "${imageName}" is not published to GHCR`);

// 2. The uid is stated, not inherited from whoever runs the job. A published
//    image bakes one uid; `docker()` otherwise expects `process.getuid()`.
const uidMatch = profile.match(/containerUid:\s*(\d+)/);
const gidMatch = profile.match(/containerGid:\s*(\d+)/);
assert(uidMatch !== null, "profile.ts does not pass containerUid; the image's baked uid would be compared against process.getuid()");
assert(gidMatch !== null, "profile.ts does not pass containerGid");
assert(uidMatch![1] === gidMatch![1], `containerUid ${uidMatch![1]} and containerGid ${gidMatch![1]} differ`);

// 3. A workflow publishes that exact name. Skipped with a note rather than
//    failed when the file is absent, because a project may legitimately have
//    adopted profile.ts before the workflow in a staged migration — but then the
//    note must say so, not stay silent.
if (!existsSync(workflowPath)) {
  console.warn(
    "profile-image check: .github/workflows/sandbox-image.yml is absent, so nothing publishes " +
      `${imageName}. Add it, or the first sandbox start on a fresh host has no image to pull.`,
  );
} else {
  const workflow = readFileSync(workflowPath, "utf8");

  const envMatch = workflow.match(/^\s+IMAGE:\s*(\S+)\s*$/m);
  assert(envMatch !== null, "sandbox-image.yml has no `IMAGE:` env entry");

  // `profile.ts` names a tag; the workflow's IMAGE names the repository. Compare
  // the repository, and assert the workflow actually builds `:latest` — a
  // workflow that only published a content tag would leave `profile.ts` pointing
  // at a tag nobody publishes.
  const workflowRepo = envMatch![1];
  const profileRepo = imageName.split(":")[0];
  assert(
    workflowRepo === profileRepo,
    `profile.ts pulls "${profileRepo}" but sandbox-image.yml publishes "${workflowRepo}"`,
  );
  assert(
    /^\s*\$\{\{\s*env\.IMAGE\s*\}\}:latest\s*$/m.test(workflow),
    "sandbox-image.yml does not publish a `:latest` tag, which is the one profile.ts names",
  );

  // 4. The uid the workflow bakes is the uid profile.ts expects. These two
  //    numbers are the actual cause of a class of failure that reads like a
  //    container bug ("UID mismatch ... was built with UID X").
  const buildUid = workflow.match(/AGENT_UID=(\d+)/);
  const buildGid = workflow.match(/AGENT_GID=(\d+)/);
  assert(buildUid !== null, "sandbox-image.yml does not pass AGENT_UID to the build");
  assert(buildGid !== null, "sandbox-image.yml does not pass AGENT_GID to the build");
  assert(
    buildUid![1] === uidMatch![1],
    `sandbox-image.yml bakes AGENT_UID=${buildUid![1]} but profile.ts expects containerUid: ${uidMatch![1]}`,
  );
  assert(
    buildGid![1] === gidMatch![1],
    `sandbox-image.yml bakes AGENT_GID=${buildGid![1]} but profile.ts expects containerGid: ${gidMatch![1]}`,
  );
}

console.log(`profile-image check passed: ${imageName} (uid ${uidMatch![1]})`);
