#!/usr/bin/env node
/**
 * cg-sync — trigger a GitLab workspace sync from the command line.
 *
 * Designed to be invoked via SSH from a GitLab post-receive hook:
 *
 *   # On the GitLab server, in the repo's custom_hooks/post-receive:
 *   #!/bin/sh
 *   while IFS=' ' read -r _old _new ref; do
 *     if [ "$ref" = "refs/heads/main" ]; then
 *       ssh -i /var/opt/gitlab/.ssh/cg_sync_ed25519 cg-user@cg-host "cg-sync my-repo"
 *     fi
 *   done
 *
 *   # On the CG machine, restrict the key to this command only:
 *   # ~/.ssh/authorized_keys:
 *   # command="cg-sync",restrict ssh-ed25519 AAAA... gitlab-cg-sync
 *
 * Does: git pull → re-mirror → daemon reindex.
 *
 * Usage:
 *   cg-sync <workspace-name> [--data-dir <path>]
 */

import { createStack } from "../src/stack.js";

async function main(): Promise<void> {
  let workspaceName: string | undefined;
  let dataDir: string | undefined;

  for (let i = 2; i < process.argv.length; i++) {
    const arg = process.argv[i];
    if (arg === "--data-dir" && process.argv[i + 1]) {
      dataDir = process.argv[++i];
    } else if (!arg.startsWith("-") && !workspaceName) {
      workspaceName = arg;
    }
  }

  if (!workspaceName) {
    process.stderr.write("Usage: cg-sync <workspace-name> [--data-dir <path>]\n");
    process.exit(1);
  }

  const stack = await createStack(dataDir);

  try {
    process.stderr.write(`[cg-sync] Syncing "${workspaceName}"...\n`);
    await stack.registry.sync(workspaceName);

    // Reindex the daemon — non-fatal if the daemon is temporarily unreachable;
    // it will pick up the changes via manifest diff detection on next boot.
    try {
      await stack.engine.reindexWorkspace(workspaceName);
      process.stderr.write(`[cg-sync] Reindex triggered for "${workspaceName}".\n`);
    } catch (err) {
      process.stderr.write(`[cg-sync] Warning: reindex failed (daemon may be down): ${err}\n`);
    }

    process.stderr.write(`[cg-sync] Done.\n`);
  } finally {
    await stack.shutdown();
  }

  process.exit(0);
}

main().catch((err) => {
  process.stderr.write(`[cg-sync] Fatal: ${err}\n`);
  process.exit(1);
});
