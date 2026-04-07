/**
 * ContextGarden configuration system.
 *
 * Cascade: defaults → config.json (canonical, no env-var override for provider fields).
 *
 * Config file:   <dataDir>/.context-garden/config.json
 * Secrets file:  ~/.context-garden/.env   (KEY=VALUE, one per line)
 *
 * API keys are never stored as plaintext in config.json. Instead, config.json
 * stores a reference:
 *
 *   "apiKeyRef": "env:CG_EMBED_API_KEY"
 *
 * and the actual value lives in ~/.context-garden/.env:
 *
 *   CG_EMBED_API_KEY=sk-...
 *
 * Auto-migration: if a legacy raw apiKey is found in config.json on load, it is
 * silently moved to ~/.context-garden/.env and replaced with an apiKeyRef.
 */

import { join } from "path";
import { homedir } from "os";
import { readText, writeText, fileExists, ensureDir } from "./util/fs.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EmbedProvider = "ollama" | "openai" | "local";
export type LlmProvider = "ollama" | "openai" | "anthropic";

export interface EmbedConfig {
  provider: EmbedProvider;
  model: string;
  host: string;
  /** Resolved API key (for runtime use — never persisted). */
  apiKey?: string;
  /** Ref to the env var holding the secret, e.g. "env:CG_EMBED_API_KEY". */
  apiKeyRef?: string;
  contextLength: number;
}

export interface LlmConfig {
  provider: LlmProvider;
  model: string;
  host: string;
  /** Resolved API key (for runtime use — never persisted). */
  apiKey?: string;
  /** Ref to the env var holding the secret, e.g. "env:CG_LLM_API_KEY". */
  apiKeyRef?: string;
  contextWindow: number;
  maxTokens: number;
}

export interface ContextGardenConfig {
  embedding: EmbedConfig;
  synthesizer: LlmConfig;
  dataDir: string;
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

const DEFAULTS: Omit<ContextGardenConfig, "dataDir"> = {
  embedding: {
    provider: "ollama",
    model: "nomic-embed-text:latest",
    host: "http://localhost:11434",
    contextLength: 512,
  },
  synthesizer: {
    provider: "ollama",
    model: "cogito:8b",
    host: "http://localhost:11434",
    contextWindow: 32768,
    maxTokens: 4096,
  },
};

// ---------------------------------------------------------------------------
// Singleton
// ---------------------------------------------------------------------------

let _config: ContextGardenConfig | null = null;

/**
 * Get the current config. Must call `initConfig()` first.
 */
export function getConfig(): ContextGardenConfig {
  if (!_config) {
    throw new Error("ContextGarden config not initialized. Call initConfig() first.");
  }
  return _config;
}

/**
 * Initialize the config singleton.
 * Cascade: defaults → config.json.
 * Env vars are NOT applied for provider fields (config.json is authoritative).
 */
export function initConfig(dataDir?: string): ContextGardenConfig {
  const resolvedDataDir = dataDir ?? process.cwd();
  const configPath = join(resolvedDataDir, ".context-garden", "config.json");

  const config: ContextGardenConfig = {
    dataDir: resolvedDataDir,
    embedding: { ...DEFAULTS.embedding },
    synthesizer: { ...DEFAULTS.synthesizer },
  };

  if (fileExists(configPath)) {
    try {
      const raw = JSON.parse(readText(configPath)) as Partial<PersistedConfig>;
      applyPersisted(config, raw, configPath);
    } catch {
      // Invalid config file — skip
    }
  }

  _config = config;
  return config;
}

/**
 * Runtime override from MCP `configure` tool.
 * When an API key is provided it is written to ~/.context-garden/.env
 * and an apiKeyRef is set in the config. Optionally persists to config.json.
 */
export function updateConfig(
  overrides: Partial<ConfigOverrides>,
  persist?: boolean,
): ContextGardenConfig {
  const config = getConfig();

  if (overrides.embedProvider !== undefined) config.embedding.provider = overrides.embedProvider;
  if (overrides.embedModel !== undefined) config.embedding.model = overrides.embedModel;
  if (overrides.embedHost !== undefined) config.embedding.host = overrides.embedHost;
  if (overrides.embedContextLength !== undefined) config.embedding.contextLength = overrides.embedContextLength;
  if (overrides.llmProvider !== undefined) config.synthesizer.provider = overrides.llmProvider;
  if (overrides.llmModel !== undefined) config.synthesizer.model = overrides.llmModel;
  if (overrides.llmHost !== undefined) config.synthesizer.host = overrides.llmHost;
  if (overrides.llmContextWindow !== undefined) config.synthesizer.contextWindow = overrides.llmContextWindow;
  if (overrides.llmMaxTokens !== undefined) config.synthesizer.maxTokens = overrides.llmMaxTokens;

  // API keys: write to .env, set ref + resolved value in memory
  if (overrides.embedApiKey !== undefined) {
    _writeToDotEnv("CG_EMBED_API_KEY", overrides.embedApiKey);
    config.embedding.apiKey = overrides.embedApiKey;
    config.embedding.apiKeyRef = "env:CG_EMBED_API_KEY";
  }
  if (overrides.llmApiKey !== undefined) {
    _writeToDotEnv("CG_LLM_API_KEY", overrides.llmApiKey);
    config.synthesizer.apiKey = overrides.llmApiKey;
    config.synthesizer.apiKeyRef = "env:CG_LLM_API_KEY";
  }

  if (persist) {
    persistConfig(config);
  }

  return config;
}

/**
 * Get the current config as a flat overrides object (for MCP response).
 * Raw API key values are never included — only whether they are set.
 */
export function getConfigSnapshot(): ConfigOverrides {
  const c = getConfig();
  return {
    embedProvider: c.embedding.provider,
    embedModel: c.embedding.model,
    embedHost: c.embedding.host,
    embedApiKey: c.embedding.apiKey ? "***" : undefined,
    embedContextLength: c.embedding.contextLength,
    llmProvider: c.synthesizer.provider,
    llmModel: c.synthesizer.model,
    llmHost: c.synthesizer.host,
    llmApiKey: c.synthesizer.apiKey ? "***" : undefined,
    llmContextWindow: c.synthesizer.contextWindow,
    llmMaxTokens: c.synthesizer.maxTokens,
  };
}

// ---------------------------------------------------------------------------
// MCP configure tool overrides shape
// ---------------------------------------------------------------------------

export interface ConfigOverrides {
  embedProvider?: EmbedProvider;
  embedModel?: string;
  embedHost?: string;
  embedApiKey?: string;
  embedContextLength?: number;
  llmProvider?: LlmProvider;
  llmModel?: string;
  llmHost?: string;
  llmApiKey?: string;
  llmContextWindow?: number;
  llmMaxTokens?: number;
}

// ---------------------------------------------------------------------------
// Persisted config shape (config.json)
// ---------------------------------------------------------------------------

interface PersistedConfig {
  embedding?: Partial<PersistedEmbedConfig>;
  synthesizer?: Partial<PersistedLlmConfig>;
}

interface PersistedEmbedConfig {
  provider: EmbedProvider;
  model: string;
  host: string;
  /** Never written; triggers auto-migration if found in an old config.json. */
  apiKey?: string;
  /** Canonical: "env:CG_EMBED_API_KEY" */
  apiKeyRef?: string;
  contextLength: number;
}

interface PersistedLlmConfig {
  provider: LlmProvider;
  model: string;
  host: string;
  /** Never written; triggers auto-migration if found in an old config.json. */
  apiKey?: string;
  /** Canonical: "env:CG_LLM_API_KEY" */
  apiKeyRef?: string;
  contextWindow: number;
  maxTokens: number;
}

function applyPersisted(
  config: ContextGardenConfig,
  raw: Partial<PersistedConfig>,
  configPath: string,
): void {
  let needsResave = false;

  if (raw.embedding) {
    const e = raw.embedding;
    if (e.provider) config.embedding.provider = e.provider;
    if (e.model) config.embedding.model = e.model;
    if (e.host) config.embedding.host = e.host;
    if (e.contextLength) config.embedding.contextLength = e.contextLength;

    if (e.apiKeyRef) {
      config.embedding.apiKeyRef = e.apiKeyRef;
      config.embedding.apiKey = _resolveRef(e.apiKeyRef);
    } else if (e.apiKey) {
      // Auto-migrate legacy plaintext key → .env
      _writeToDotEnv("CG_EMBED_API_KEY", e.apiKey);
      config.embedding.apiKey = e.apiKey;
      config.embedding.apiKeyRef = "env:CG_EMBED_API_KEY";
      needsResave = true;
    }
  }

  if (raw.synthesizer) {
    const s = raw.synthesizer;
    if (s.provider) config.synthesizer.provider = s.provider;
    if (s.model) config.synthesizer.model = s.model;
    if (s.host) config.synthesizer.host = s.host;
    if (s.contextWindow) config.synthesizer.contextWindow = s.contextWindow;
    if (s.maxTokens) config.synthesizer.maxTokens = s.maxTokens;

    if (s.apiKeyRef) {
      config.synthesizer.apiKeyRef = s.apiKeyRef;
      config.synthesizer.apiKey = _resolveRef(s.apiKeyRef);
    } else if (s.apiKey) {
      // Auto-migrate legacy plaintext key → .env
      _writeToDotEnv("CG_LLM_API_KEY", s.apiKey);
      config.synthesizer.apiKey = s.apiKey;
      config.synthesizer.apiKeyRef = "env:CG_LLM_API_KEY";
      needsResave = true;
    }
  }

  // Rewrite config.json without the plaintext key
  if (needsResave) {
    try {
      persistConfig(config, configPath);
    } catch {
      // Non-fatal — config is valid in memory
    }
  }
}

function persistConfig(config: ContextGardenConfig, overridePath?: string): void {
  const configDir = join(config.dataDir, ".context-garden");
  ensureDir(configDir);

  const persisted: PersistedConfig = {
    embedding: {
      provider: config.embedding.provider,
      model: config.embedding.model,
      host: config.embedding.host,
      contextLength: config.embedding.contextLength,
      // Write ref, never raw key
      ...(config.embedding.apiKeyRef ? { apiKeyRef: config.embedding.apiKeyRef } : {}),
    },
    synthesizer: {
      provider: config.synthesizer.provider,
      model: config.synthesizer.model,
      host: config.synthesizer.host,
      contextWindow: config.synthesizer.contextWindow,
      maxTokens: config.synthesizer.maxTokens,
      // Write ref, never raw key
      ...(config.synthesizer.apiKeyRef ? { apiKeyRef: config.synthesizer.apiKeyRef } : {}),
    },
  };

  const path = overridePath ?? join(configDir, "config.json");
  writeText(path, JSON.stringify(persisted, null, 2));
}

// ---------------------------------------------------------------------------
// .env helpers
// ---------------------------------------------------------------------------

function _dotEnvPath(): string {
  return join(homedir(), ".context-garden", ".env");
}

function _loadDotEnv(): Record<string, string> {
  const path = _dotEnvPath();
  const secrets: Record<string, string> = {};
  if (!fileExists(path)) return secrets;
  for (const line of readText(path).split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const eq = trimmed.indexOf("=");
    const k = trimmed.slice(0, eq).trim();
    let v = trimmed.slice(eq + 1).trim();
    // Strip surrounding quotes
    if (v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === '"' || v[0] === "'")) {
      v = v.slice(1, -1);
    }
    secrets[k] = v;
  }
  return secrets;
}

function _writeToDotEnv(key: string, value: string): void {
  const path = _dotEnvPath();
  ensureDir(join(homedir(), ".context-garden"));

  let lines: string[] = [];
  if (fileExists(path)) {
    lines = readText(path).split("\n").filter((l) => !l.trim().startsWith(key + "="));
  }
  lines.push(`${key}=${value}`);
  // Ensure trailing newline
  const content = lines.join("\n").replace(/\n+$/, "") + "\n";
  writeText(path, content);
}

function _resolveRef(ref: string): string | undefined {
  if (!ref.startsWith("env:")) return undefined;
  const varName = ref.slice(4);
  const secrets = _loadDotEnv();
  return secrets[varName] || process.env[varName] || undefined;
}

// ---------------------------------------------------------------------------
// Path resolution helpers
// ---------------------------------------------------------------------------

export interface ContextGardenPaths {
  rootDir: string;
  mdDbPath: string;
  graphDbPath: string;
  personalitiesDir: string;
  workspacesPath: string;
}

export function resolvePaths(rootDir?: string): ContextGardenPaths {
  const root = rootDir ?? process.cwd();
  return {
    rootDir: root,
    mdDbPath: join(root, "md_db"),
    graphDbPath: join(root, ".context-garden", "graph.db"),
    personalitiesDir: join(root, "src", "data"),
    workspacesPath: join(root, ".context-garden", "workspaces.json"),
  };
}
