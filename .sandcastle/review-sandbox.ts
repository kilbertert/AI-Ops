import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseEnv } from "node:util";
import { createSandbox, type Sandbox, type SandboxProvider, type BindMountSandboxProviderConfig } from "@ai-hero/sandcastle";
import { claudeProfile } from "./profile.js";

// Only model transport is inherited; repository .env is not a credential grant.
const MODEL_ENV = new Set([
  "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
  "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
]);

// Sandcastle 0.12 exposes this shape at runtime but omits create/tag from its public type.
type DockerProvider = SandboxProvider & BindMountSandboxProviderConfig & { tag: "bind-mount" };

export function reviewProvider(input: SandboxProvider, modelEnv: Record<string, string> = {}): DockerProvider {
  const provider = input as DockerProvider;
  if (provider.tag !== "bind-mount" || typeof provider.create !== "function") throw new Error("Review requires the Docker bind-mount provider");
  return {
    ...provider,
    create: async (options) => {
      const handle = await provider.create({
        ...options,
        mounts: options.mounts.map((mount) => ({ ...mount, readonly: true })),
        env: {
          ...Object.fromEntries(Object.entries({ ...options.env, ...modelEnv }).filter(([key]) => MODEL_ENV.has(key))),
          ...(provider.env.AFK_PROFILE ? { AFK_PROFILE: provider.env.AFK_PROFILE } : {}),
          GH_TOKEN: "",
          GITHUB_TOKEN: "",
        },
      });
      const worktreePath = "/home/agent/review";
      try {
        // Candidate-controlled Git config must never be read by host Git either.
        const copied = await handle.exec(`set -eu
source=$PWD
base=$(git rev-parse refs/remotes/origin/main)
git clone --no-local --no-hardlinks --no-tags --single-branch --branch review-run "$source" ${worktreePath}
cd ${worktreePath}
git fetch --no-tags "$source" "$base"
git remote remove origin
git update-ref refs/remotes/origin/main "$base"`, { cwd: handle.worktreePath });
        if (copied.exitCode !== 0) throw new Error("Cannot copy reviewer repository inside container");
        return {
          ...handle,
          worktreePath,
          exec: (command, execOptions) => handle.exec(command, { cwd: worktreePath, ...execOptions }),
        };
      } catch (error) {
        await handle.close();
        throw error;
      }
    },
  };
}

export function cloneReviewCandidate(root: string, ref: string): { directory: string; sha: string } {
  const git = (args: string[], cwd = root) => execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
  const sha = git(["rev-parse", "--verify", `${ref}^{commit}`]);
  const base = git(["rev-parse", "--verify", "origin/main^{commit}"]);
  const parent = join(resolve(root), ".sandcastle", "reviews");
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const directory = mkdtempSync(join(parent, "candidate-"));
  try {
    // --no-local makes independent objects, with no hardlinks or alternates.
    git(["clone", "--no-local", "--no-checkout", "--no-tags", "--single-branch", "--branch", ref, "--", resolve(root), directory]);
    git(["fetch", "--no-tags", "--", resolve(root), base], directory);
    git(["remote", "remove", "origin"], directory);
    git(["update-ref", "refs/remotes/origin/main", base], directory);
    git(["checkout", "-b", "review-candidate", sha], directory);
    return { directory, sha };
  } catch (error) {
    rmSync(directory, { recursive: true, force: true });
    throw error;
  }
}

export async function withReviewSandbox<T>(
  root: string,
  ref: string,
  profile: string | undefined,
  work: (sandbox: Sandbox, agent: ReturnType<typeof claudeProfile>["agent"]) => Promise<T>,
): Promise<T> {
  const envFile = join(root, ".sandcastle", ".env");
  const configured = existsSync(envFile) ? parseEnv(readFileSync(envFile, "utf8")) : {};
  const modelEnv = Object.fromEntries([...MODEL_ENV].flatMap((key) => {
    const value = configured[key] || process.env[key];
    return value ? [[key, value]] : [];
  }));
  const candidate = cloneReviewCandidate(root, ref);
  let sandbox: Sandbox | undefined;
  try {
    const config = claudeProfile(profile, { AFK_AGENT_GH_TOKEN: "" });
    sandbox = await createSandbox({
      cwd: candidate.directory,
      branch: "review-run",
      baseBranch: candidate.sha,
      sandbox: reviewProvider(config.sandbox, modelEnv),
      hooks: config.hooks,
    });
    const result = await work(sandbox, config.agent);
    const status = await sandbox.exec("git status --porcelain --untracked-files=no");
    const head = await sandbox.exec("git rev-parse HEAD");
    if (status.exitCode !== 0 || status.stdout.trim() || head.exitCode !== 0 || head.stdout.trim() !== candidate.sha) {
      throw new Error("Reviewer changed the candidate; independent verification must be repeated");
    }
    const current = execFileSync("git", ["rev-parse", "--verify", `${ref}^{commit}`], { cwd: root, encoding: "utf8" }).trim();
    if (current !== candidate.sha) throw new Error("Candidate moved during review; verify the new revision");
    return result;
  } finally {
    try {
      await sandbox?.close();
    } finally {
      rmSync(candidate.directory, { recursive: true, force: true });
    }
  }
}
