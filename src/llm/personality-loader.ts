/**
 * Personality loader — reads personality markdown files from disk.
 *
 * File format:
 * ```markdown
 * ---
 * type: personality
 * title: Deep Researcher
 * provider:
 *   model: llama3
 *   baseUrl: http://localhost:11434/v1
 * ---
 * # Deep Researcher
 * You are a deep researcher...
 * ```
 */

import { join } from "path";
import { readText, fileExists, listDir } from "../util/fs.js";
import { parseFrontmatter } from "../markdown/frontmatter.js";
import { getConfig } from "../config.js";
import type { ChatOptions } from "./client.js";

// ---------------------------------------------------------------------------
// PersonalityConfig (inlined — no external dependency)
// ---------------------------------------------------------------------------

export interface PersonalityConfig {
  /** Personality name (matches filename without .md extension). */
  name: string;

  /** Markdown body — injected into the system prompt. */
  content: string;

  /** LLM provider override for this personality. */
  provider?: {
    /** Provider type — overrides global config. */
    type?: "ollama" | "openai" | "anthropic";
    /** Model name. */
    model?: string;
    /** Base URL for ollama/openai providers. */
    baseUrl?: string;
    /** API key (literal or "env:VAR_NAME" to read from env). */
    apiKey?: string;
    /** Context window size (Ollama num_ctx). */
    numCtx?: number;
    /** Max output tokens. */
    maxTokens?: number;
    /** Sampling temperature (0–2). */
    temperature?: number;
    /** Enable/disable thinking mode (Ollama only, e.g. qwen3). */
    think?: boolean;
  };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Load a personality by name from the given directory.
 * Returns null if the file doesn't exist.
 */
export function loadPersonality(
  name: string,
  personalitiesDir: string,
): PersonalityConfig | null {
  const filePath = join(personalitiesDir, `${name}.md`);

  if (!fileExists(filePath)) {
    return null;
  }

  const raw = readText(filePath);
  const { frontmatter, body } = parseFrontmatter(raw);

  return {
    name,
    content: body,
    provider: extractProvider(frontmatter),
  };
}

/**
 * List all available personality names in the given directory.
 * Returns names without the .md extension.
 */
export function listPersonalities(personalitiesDir: string): string[] {
  if (!fileExists(personalitiesDir)) {
    return [];
  }

  return listDir(personalitiesDir)
    .filter((f) => f.endsWith(".md"))
    .map((f) => f.slice(0, -3));
}

/**
 * Resolve LLM chat options from personality config with cascade:
 * global config defaults → personality frontmatter overrides.
 */
export function resolvePersonalityChatOptions(
  personality: PersonalityConfig | null,
): ChatOptions {
  const defaults = getConfig().synthesizer;
  const p = personality?.provider;
  return {
    model: p?.model ?? defaults.model,
    temperature: p?.temperature,
    numCtx: p?.numCtx,
    maxTokens: p?.maxTokens ?? defaults.maxTokens,
    providerType: p?.type,
    apiKey: p?.apiKey,
    think: p?.think,
  };
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Extract provider config from parsed frontmatter.
 */
function extractProvider(
  fm: Record<string, unknown>,
): PersonalityConfig["provider"] | undefined {
  const provider = fm["provider"];
  if (!provider || typeof provider !== "object") return undefined;

  const p = provider as Record<string, unknown>;
  const type = typeof p["type"] === "string" ? p["type"] as "ollama" | "openai" | "anthropic" : undefined;
  const model = typeof p["model"] === "string" ? p["model"] : undefined;
  const baseUrl = typeof p["baseUrl"] === "string" ? p["baseUrl"] : undefined;
  const numCtx = typeof p["numCtx"] === "number" ? p["numCtx"] : undefined;
  const maxTokens = typeof p["maxTokens"] === "number" ? p["maxTokens"] : undefined;
  const temperature = typeof p["temperature"] === "number" ? p["temperature"] : undefined;

  // Resolve apiKey — supports "env:VAR_NAME" pattern
  let apiKey: string | undefined;
  if (typeof p["apiKey"] === "string") {
    const raw = p["apiKey"];
    apiKey = raw.startsWith("env:") ? process.env[raw.slice(4)] : raw;
  }

  const think = typeof p["think"] === "boolean" ? p["think"] : undefined;

  const hasAny = type || model || baseUrl || apiKey || numCtx !== undefined || maxTokens !== undefined || temperature !== undefined || think !== undefined;
  if (!hasAny) return undefined;

  return { type, model, baseUrl, apiKey, numCtx, maxTokens, temperature, think };
}
