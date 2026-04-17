/**
 * GitLab clone/pull operations for GitLab-backed workspaces.
 *
 * All git operations use child_process.spawn with an argv array — no shell
 * interpolation, no injection risk even if the project URL or token contains
 * special characters.
 *
 * Token resolution order:
 *   1. gitlabConfig.accessToken (per-workspace override in workspaces.json)
 *   2. CG_GITLAB_TOKEN environment variable
 *   3. CG_GITLAB_TOKEN entry in ~/.context-garden/.env
 */

import { spawn } from "child_process";
import { existsSync, readFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";

// ---------------------------------------------------------------------------
// Types (re-exported so registry.ts doesn't need to import from here)
// ---------------------------------------------------------------------------

export interface GitLabConfig {
  /** Full project URL, e.g. "https://gitlab.home.lab/org/repo". */
  projectUrl: string;
  /** Branch to track, e.g. "main". */
  branch: string;
  /** Per-workspace PAT override. Falls back to global CG_GITLAB_TOKEN. */
  accessToken?: string;
  /** Absolute path to the local clone. Set at registration time. */
  cloneDir: string;
  /**
   * Shared secret used to verify incoming GitLab webhooks.
   * Generated at registration time. Configure as the "Secret token" in
   * GitLab → Settings → Webhooks.
   */
  webhookSecret: string;
}

// ---------------------------------------------------------------------------
// Token resolution
// ---------------------------------------------------------------------------

/**
 * Resolve the GitLab access token for a workspace config.
 * Returns undefined for public repos where no token is needed.
 */
export function resolveToken(config: GitLabConfig): string | undefined {
  if (config.accessToken) return config.accessToken;
  if (process.env["CG_GITLAB_TOKEN"]) return process.env["CG_GITLAB_TOKEN"];

  const envPath = join(homedir(), ".context-garden", ".env");
  if (existsSync(envPath)) {
    for (const line of readFileSync(envPath, "utf-8").split("\n")) {
      const trimmed = line.trim();
      if (trimmed.startsWith("CG_GITLAB_TOKEN=")) {
        return trimmed.slice("CG_GITLAB_TOKEN=".length).trim() || undefined;
      }
    }
  }

  return undefined;
}

// ---------------------------------------------------------------------------
// URL construction
// ---------------------------------------------------------------------------

/**
 * Build a clone URL, embedding oauth2 credentials for HTTPS remotes.
 *
 * SSH URLs (git@host:org/repo or ssh://git@host/org/repo) are returned
 * unchanged — SSH auth is handled by the OS key agent, not by credentials
 * in the URL.
 *
 * HTTPS: "https://gitlab.home.lab/org/repo" → "https://oauth2:{token}@gitlab.home.lab/org/repo.git"
 * SSH:   "git@gitlab.home.lab:org/repo"     → "git@gitlab.home.lab:org/repo.git" (unchanged)
 */
export function buildAuthUrl(projectUrl: string, token: string | undefined): string {
  // SSH URL — pass through as-is (SCP-style git@ or ssh:// scheme).
  if (projectUrl.startsWith("git@") || projectUrl.startsWith("ssh://")) {
    return projectUrl.endsWith(".git") ? projectUrl : `${projectUrl}.git`;
  }

  const normalized = projectUrl.endsWith(".git") ? projectUrl : `${projectUrl}.git`;
  if (!token) return normalized;

  const url = new URL(normalized);
  url.username = "oauth2";
  url.password = token;
  return url.toString();
}

// ---------------------------------------------------------------------------
// Git helpers
// ---------------------------------------------------------------------------

/**
 * Spawn git with the given argv array and optional cwd.
 * Captures stderr; throws with it on non-zero exit.
 */
function runGit(args: string[], cwd?: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const proc = spawn("git", args, {
      cwd,
      stdio: ["ignore", "ignore", "pipe"],
    });

    let stderr = "";
    proc.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf-8");
    });

    proc.on("close", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`git ${args[0]} failed (exit ${code}): ${stderr.trim()}`));
      }
    });

    proc.on("error", (err) => {
      reject(new Error(`Failed to spawn git: ${(err as Error).message}`));
    });
  });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Clone a GitLab repo into config.cloneDir.
 *
 * Uses --depth=1 so the initial clone is fast, and --no-single-branch so that
 * git pull works correctly on a shallow clone.
 */
export async function cloneRepo(config: GitLabConfig, token: string | undefined): Promise<void> {
  const authUrl = buildAuthUrl(config.projectUrl, token);
  await runGit([
    "clone",
    "--depth=1",
    `--branch=${config.branch}`,
    "--no-single-branch",
    authUrl,
    config.cloneDir,
  ]);
}

/**
 * Converge the local clone to the remote branch state via fetch + reset --hard.
 *
 * Preferred over `git pull` for managed mirrors:
 *   - Idempotent: running it twice produces the same result.
 *   - No merge conflicts: always matches remote exactly.
 *   - Works correctly on shallow clones.
 *
 * Updates the remote URL first so a rotated token takes effect without
 * re-registering the workspace.
 */
export async function fetchAndReset(config: GitLabConfig, token: string | undefined): Promise<void> {
  const authUrl = buildAuthUrl(config.projectUrl, token);
  await runGit(["remote", "set-url", "origin", authUrl], config.cloneDir);
  await runGit(["fetch", "origin", config.branch], config.cloneDir);
  await runGit(["reset", "--hard", `origin/${config.branch}`], config.cloneDir);
}
