import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { strictSignal } from "./review-signal.js";
import { cloneReviewCandidate, reviewProvider, withReviewSandbox } from "./review-sandbox.js";
import { createBindMountSandboxProvider } from "@ai-hero/sandcastle";

const complete = "<promise>COMPLETE</promise>";
const blocked = "<promise>BLOCKED</promise>";
for (const [text, expected] of [
  [`原入口已验\n${complete}`, "COMPLETE"],
  [`未验\n${blocked}`, "BLOCKED"],
  [`示例是 \`${complete}\`。\n没有最终裁决`, undefined],
  [`未验。仅为格式示例：\n\`\`\`xml\n${complete}`, undefined],
  [`仅为格式示例：\n~~~\n${complete}`, undefined],
  [`\`\`\`\`xml\n\`\`\`\n${complete}`, undefined],
  [`\`\`\`xml\n${blocked}\n\`\`\`\n已验证\n${complete}`, "COMPLETE"],
  [`${complete}\n${blocked}`, undefined],
  [`已验证\r\n${complete}\r\n`, "COMPLETE"],
  [`已验证\n${complete}\n还有正文`, undefined],
] as const) assert.equal(strictSignal(text), expected, text);

let received: Record<string, string> = {};
let receivedMounts: { readonly?: boolean }[] = [];
const provider = reviewProvider(createBindMountSandboxProvider({
  name: "capture", env: { AFK_PROFILE: "claude-deepseek" },
  sandboxHomedir: undefined,
  async create(options) {
    received = options.env;
    receivedMounts = options.mounts;
    return { worktreePath: "/fixture", async exec() { return { stdout: "", stderr: "", exitCode: 0 }; },
      async close() {}, async copyFileIn() {}, async copyFileOut() {} };
  },
}));
if (provider.tag !== "bind-mount") throw new Error("Unexpected provider");
await provider.create({
  worktreePath: "/fixture", hostRepoPath: "/fixture", mounts: [{ hostPath: "/fixture", sandboxPath: "/fixture" }],
  env: { GH_TOKEN: "fake", GITHUB_TOKEN: "fake", GH_ENTERPRISE_TOKEN: "fake",
    GIT_ASKPASS: "fake", GH_CONFIG_DIR: "fake", GIT_SSH_COMMAND: "fake",
    AFK_PROFILE: "untrusted", ANTHROPIC_API_KEY: "fake-model-key" },
});
assert.deepEqual(received, { ANTHROPIC_API_KEY: "fake-model-key", AFK_PROFILE: "claude-deepseek", GH_TOKEN: "", GITHUB_TOKEN: "" });
assert.equal(receivedMounts.every((mount) => mount.readonly), true);

const parent = resolve(".sandcastle/reviews");
mkdirSync(parent, { recursive: true, mode: 0o700 });
const fixture = mkdtempSync(join(parent, "check-"));
const git = (args: string[], cwd = fixture) => execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
let clone: ReturnType<typeof cloneReviewCandidate> | undefined;
try {
  git(["init", "-b", "main"]);
  mkdirSync(join(fixture, ".sandcastle"));
  writeFileSync(join(fixture, "package.json"), JSON.stringify({ name: "boundary-fixture", version: "1.0.0", scripts: {
    postinstall: "node -e \"require('fs').writeFileSync('installation.json',JSON.stringify({container:require('fs').existsSync('/.dockerenv'),hostSecret:!!process.env.REVIEW_HOST_SENTINEL,github:!!process.env.GH_TOKEN,enterprise:!!process.env.GH_ENTERPRISE_TOKEN}))\"",
  } }));
  writeFileSync(join(fixture, "package-lock.json"), JSON.stringify({ name: "boundary-fixture", version: "1.0.0", lockfileVersion: 3, packages: { "": { name: "boundary-fixture", version: "1.0.0", hasInstallScript: true } } }));
  writeFileSync(join(fixture, ".sandcastle/sandbox-prepare.sh"), "#!/bin/sh\nset -eu\nnpm ci --no-audit --no-fund\n");
  git(["add", "."]);
  const tree = git(["write-tree"]);
  const sha = git(["-c", "user.name=Boundary Check", "-c", "user.email=check@example.invalid", "commit-tree", tree, "-m", "test(afk): synthetic boundary fixture"]);
  git(["update-ref", "refs/heads/main", sha]);
  git(["update-ref", "refs/remotes/origin/main", sha]);
  // A second commit lets the hostile reviewer move refs without altering files.
  const other = git(["-c", "user.name=Boundary Check", "-c", "user.email=check@example.invalid", "commit-tree", tree, "-p", sha, "-m", "test(afk): second synthetic commit"]);
  git(["update-ref", "refs/heads/main", other]);
  writeFileSync(join(fixture, ".sandcastle/.env"), "GH_TOKEN=fake-host-token\nGH_ENTERPRISE_TOKEN=fake-enterprise-token\nANTHROPIC_API_KEY=fake-required-model-token\n");
  clone = cloneReviewCandidate(fixture, "main");
  assert.equal(git(["rev-parse", "HEAD"], clone.directory), other);
  assert.equal(existsSync(join(clone.directory, ".sandcastle/.env")), false);
  assert.equal(existsSync(join(clone.directory, ".git/objects/info/alternates")), false);
  git(["update-ref", "refs/heads/main", sha], clone.directory);
  assert.equal(git(["rev-parse", "main"]), other, "copy must not share refs");
  assert.equal(git(["remote"], clone.directory), "");
  if (process.argv.includes("--docker")) {
    process.env.REVIEW_HOST_SENTINEL = "fake-host-secret";
    process.env.AFK_AGENT_GH_TOKEN = "fake-host-token";
    await withReviewSandbox(fixture, "main", "claude", async (sandbox) => {
      const auth = await sandbox.exec('[ "$ANTHROPIC_API_KEY" = "fake-required-model-token" ] && echo retained');
      assert.equal(auth.stdout.trim(), "retained", "default profile must retain allowed model authentication");
      const probe = await sandbox.exec("cat installation.json");
      assert.equal(probe.exitCode, 0, probe.stderr);
      assert.deepEqual(JSON.parse(probe.stdout), { container: true, hostSecret: false, github: false, enterprise: false });
      const mutation = await sandbox.exec(`git update-ref refs/heads/main ${sha}`);
      assert.equal(mutation.exitCode, 0, mutation.stderr);
      assert.equal(git(["rev-parse", "main"]), other, "container must not change source refs");
      assert.equal(existsSync(join(fixture, "installation.json")), false, "install must not run on host source");
      const hostMount = await sandbox.exec("touch /home/agent/workspace/host-write-probe");
      assert.notEqual(hostMount.exitCode, 0, "host workspace mount must be read-only");
      const run = await sandbox.run({
        name: "boundary-fixture", prompt: "Synthetic execution, no model call.", logging: { type: "stdout" },
        agent: {
          name: "deterministic-fixture", env: {}, captureSessions: false,
          buildPrintCommand: () => ({ command: `printf '%s\\n' '{"result":"container checked\\n<promise>COMPLETE</promise>"}'` }),
          parseStreamLine: (line) => [{ type: "result", result: JSON.parse(line).result }],
        },
        completionSignal: [complete, blocked],
      });
      assert.equal(strictSignal(run.stdout), "COMPLETE");
    });
    delete process.env.REVIEW_HOST_SENTINEL;
    delete process.env.AFK_AGENT_GH_TOKEN;
  }
  assert.equal(git(["rev-parse", "main"]), other);
  console.log(`review boundary checks passed${process.argv.includes("--docker") ? " (including real Docker install/ref isolation)" : ""}`);
} finally {
  if (clone) rmSync(clone.directory, { recursive: true, force: true });
  rmSync(fixture, { recursive: true, force: true });
}
