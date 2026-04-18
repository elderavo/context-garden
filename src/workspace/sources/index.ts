import type { WorkspaceSourceType } from "../registry.js";
import type { WorkspaceSource } from "./source.js";
import { LocalSource } from "./local-source.js";
import { GitLabSource } from "./gitlab-source.js";

const SOURCES: Record<WorkspaceSourceType, WorkspaceSource> = {
  local: new LocalSource(),
  gitlab: new GitLabSource(),
};

export function getSource(type: WorkspaceSourceType = "local"): WorkspaceSource {
  const s = SOURCES[type];
  if (!s) throw new Error(`Unknown workspace source type: "${type}"`);
  return s;
}

export type { WorkspaceSource, RegistryContext } from "./source.js";
