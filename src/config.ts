/**
 * ContextGarden configuration system.
 *
 * Cascade: defaults → config.json → env vars → MCP `configure` tool (runtime).
 *
 * Config file: <dataDir>/.context-garden/config.json
 * Env vars: CG_EMBED_PROVIDER, CG_EMBED_MODEL, CG_EMBED_HOST,
 *           CG_LLM_PROVIDER, CG_LLM_MODEL, CG_LLM_HOST,
 *           OPENAI_API_KEY, ANTHROPIC_API_KEY
 */

import { join } from "path";
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
  apiKey?: string;
  contextLength: number;
}

export interface LlmConfig {
  provider: LlmProvider;
  model: string;
  host: string;
  apiKey?: string;
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
 * Cascade: defaults → config.json → env vars.
 */
export function initConfig(dataDir?: string): ContextGardenConfig {
  const resolvedDataDir = dataDir ?? process.cwd();
  const configPath = join(resolvedDataDir, ".context-garden", "config.json");

  // Start with defaults
  const config: ContextGardenConfig = {
    dataDir: resolvedDataDir,
    embedding: { ...DEFAULTS.embedding },
    synthesizer: { ...DEFAULTS.synthesizer },
  };

  // Layer 2: config.json
  if (fileExists(configPath)) {
    try {
      const raw = JSON.parse(readText(configPath)) as Partial<PersistedConfig>;
      applyPersisted(config, raw);
    } catch {
      // Invalid config file — skip
    }
  }

  // Layer 3: env vars
  applyEnv(config);

  _config = config;
  return config;
}

/**
 * Runtime override from MCP `configure` tool.
 * Optionally persists to config.json.
 */
export function updateConfig(
  overrides: Partial<ConfigOverrides>,
  persist?: boolean,
): ContextGardenConfig {
  const config = getConfig();

  if (overrides.embedProvider !== undefined) config.embedding.provider = overrides.embedProvider;
  if (overrides.embedModel !== undefined) config.embedding.model = overrides.embedModel;
  if (overrides.embedHost !== undefined) config.embedding.host = overrides.embedHost;
  if (overrides.embedApiKey !== undefined) config.embedding.apiKey = overrides.embedApiKey;
  if (overrides.embedContextLength !== undefined) config.embedding.contextLength = overrides.embedContextLength;
  if (overrides.llmProvider !== undefined) config.synthesizer.provider = overrides.llmProvider;
  if (overrides.llmModel !== undefined) config.synthesizer.model = overrides.llmModel;
  if (overrides.llmHost !== undefined) config.synthesizer.host = overrides.llmHost;
  if (overrides.llmApiKey !== undefined) config.synthesizer.apiKey = overrides.llmApiKey;
  if (overrides.llmContextWindow !== undefined) config.synthesizer.contextWindow = overrides.llmContextWindow;
  if (overrides.llmMaxTokens !== undefined) config.synthesizer.maxTokens = overrides.llmMaxTokens;

  if (persist) {
    persistConfig(config);
  }

  return config;
}

/**
 * Get the current config as a flat overrides object (for MCP response).
 */
export function getConfigSnapshot(): ConfigOverrides {
  const c = getConfig();
  return {
    embedProvider: c.embedding.provider,
    embedModel: c.embedding.model,
    embedHost: c.embedding.host,
    embedApiKey: c.embedding.apiKey,
    embedContextLength: c.embedding.contextLength,
    llmProvider: c.synthesizer.provider,
    llmModel: c.synthesizer.model,
    llmHost: c.synthesizer.host,
    llmApiKey: c.synthesizer.apiKey,
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
  embedding?: Partial<EmbedConfig>;
  synthesizer?: Partial<LlmConfig>;
}

function applyPersisted(config: ContextGardenConfig, raw: Partial<PersistedConfig>): void {
  if (raw.embedding) {
    if (raw.embedding.provider) config.embedding.provider = raw.embedding.provider;
    if (raw.embedding.model) config.embedding.model = raw.embedding.model;
    if (raw.embedding.host) config.embedding.host = raw.embedding.host;
    if (raw.embedding.apiKey) config.embedding.apiKey = raw.embedding.apiKey;
    if (raw.embedding.contextLength) config.embedding.contextLength = raw.embedding.contextLength;
  }
  if (raw.synthesizer) {
    if (raw.synthesizer.provider) config.synthesizer.provider = raw.synthesizer.provider;
    if (raw.synthesizer.model) config.synthesizer.model = raw.synthesizer.model;
    if (raw.synthesizer.host) config.synthesizer.host = raw.synthesizer.host;
    if (raw.synthesizer.apiKey) config.synthesizer.apiKey = raw.synthesizer.apiKey;
    if (raw.synthesizer.contextWindow) config.synthesizer.contextWindow = raw.synthesizer.contextWindow;
    if (raw.synthesizer.maxTokens) config.synthesizer.maxTokens = raw.synthesizer.maxTokens;
  }
}

function applyEnv(config: ContextGardenConfig): void {
  const env = process.env;

  if (env["CG_EMBED_PROVIDER"]) config.embedding.provider = env["CG_EMBED_PROVIDER"] as EmbedProvider;
  if (env["CG_EMBED_MODEL"]) config.embedding.model = env["CG_EMBED_MODEL"];
  if (env["CG_EMBED_HOST"]) config.embedding.host = env["CG_EMBED_HOST"];
  if (env["CG_LLM_PROVIDER"]) config.synthesizer.provider = env["CG_LLM_PROVIDER"] as LlmProvider;
  if (env["CG_LLM_MODEL"]) config.synthesizer.model = env["CG_LLM_MODEL"];
  if (env["CG_LLM_HOST"]) {
    const normalized = env["CG_LLM_HOST"]
      .replace(/\/$/, "")
      .replace(/\/v1(?:\/chat\/completions)?\/?$/, "");
    config.synthesizer.host = normalized;
  }

  // API keys — check CG_ prefixed first, then fallback to bare keys
  if (env["CG_EMBED_API_KEY"] || env["OPENAI_API_KEY"]) {
    config.embedding.apiKey = env["CG_EMBED_API_KEY"] ?? env["OPENAI_API_KEY"];
  }
  if (env["CG_LLM_API_KEY"] || env["OPENAI_API_KEY"] || env["ANTHROPIC_API_KEY"]) {
    config.synthesizer.apiKey = env["CG_LLM_API_KEY"] ?? env["ANTHROPIC_API_KEY"] ?? env["OPENAI_API_KEY"];
  }
}

function persistConfig(config: ContextGardenConfig): void {
  const configDir = join(config.dataDir, ".context-garden");
  ensureDir(configDir);

  const persisted: PersistedConfig = {
    embedding: {
      provider: config.embedding.provider,
      model: config.embedding.model,
      host: config.embedding.host,
      apiKey: config.embedding.apiKey,
      contextLength: config.embedding.contextLength,
    },
    synthesizer: {
      provider: config.synthesizer.provider,
      model: config.synthesizer.model,
      host: config.synthesizer.host,
      apiKey: config.synthesizer.apiKey,
      contextWindow: config.synthesizer.contextWindow,
      maxTokens: config.synthesizer.maxTokens,
    },
  };

  writeText(join(configDir, "config.json"), JSON.stringify(persisted, null, 2));
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
