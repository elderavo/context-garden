/**
 * Provider-agnostic LLM chat client.
 *
 * Routes to Ollama, OpenAI, or Anthropic based on config.
 * Throws `ProviderUnreachableError` for network failures so callers can
 * distinguish "server down" from "bad auth" or "model not found".
 */

import axios, { type AxiosError } from "axios";
import { getConfig, type LlmConfig } from "../config.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatOptions {
  /** Override model from config. */
  model?: string;
  /** Sampling temperature (0–2). */
  temperature?: number;
  /** Max tokens to generate (OpenAI max_tokens / Ollama num_predict). */
  maxTokens?: number;
  /** Context window size (Ollama num_ctx). */
  numCtx?: number;
  /** Request timeout in ms. */
  timeout?: number;
  /** Override provider type (from personality config). */
  providerType?: "ollama" | "openai" | "anthropic";
  /** Override API key (from personality config). */
  apiKey?: string;
  /** Enable/disable thinking mode (Ollama only, e.g. qwen3). Default: undefined (model default). */
  think?: boolean;
}

export interface ChatResult {
  content: string;
  model: string;
  provider: string;
}

// ---------------------------------------------------------------------------
// ProviderUnreachableError
// ---------------------------------------------------------------------------

const NETWORK_CODES = new Set([
  "ECONNREFUSED",
  "ETIMEDOUT",
  "ECONNRESET",
  "EHOSTUNREACH",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EPIPE",
  "ERR_SOCKET_CONNECTION_TIMEOUT",
]);

export class ProviderUnreachableError extends Error {
  readonly provider: string;
  readonly host: string;
  override readonly cause: unknown;

  constructor(provider: string, host: string, cause: unknown) {
    const msg = cause instanceof Error ? cause.message : String(cause);
    super(`LLM provider "${provider}" unreachable at ${host}: ${msg}`);
    this.name = "ProviderUnreachableError";
    this.provider = provider;
    this.host = host;
    this.cause = cause;
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Send a chat completion request to the configured LLM provider.
 *
 * @throws {ProviderUnreachableError} if the provider cannot be reached (network error).
 * @throws {Error} for auth failures, model-not-found, etc.
 */
export async function llmChat(
  messages: ChatMessage[],
  opts?: ChatOptions,
): Promise<ChatResult> {
  const config = getConfig().synthesizer;
  const model = opts?.model ?? config.model;
  const timeout = opts?.timeout ?? 120_000;

  try {
    const effectiveProvider = opts?.providerType ?? config.provider;
    switch (effectiveProvider) {
      case "anthropic":
        return await callAnthropic(config, model, messages, opts, timeout);
      case "openai":
        return await callOpenAI(config, model, messages, opts, timeout);
      default:
        return await callOllama(config, model, messages, opts, timeout);
    }
  } catch (err) {
    if (err instanceof ProviderUnreachableError) throw err;
    if (isNetworkError(err)) {
      throw new ProviderUnreachableError(config.provider, config.host, err);
    }
    throw err;
  }
}

/**
 * Quick check whether the LLM provider is reachable.
 *
 * For Ollama: GETs /api/tags — model-agnostic, no inference cost.
 * For other providers: minimal 1-token chat call.
 *
 * @returns `true` if the server responds, `false` on any error.
 */
export async function isLlmReachable(): Promise<boolean> {
  const config = getConfig().synthesizer;
  try {
    if ((config.provider ?? "ollama") === "ollama") {
      await axios.get(`${config.host}/api/tags`, { timeout: 5000 });
    } else {
      await llmChat([{ role: "user", content: "ping" }], { maxTokens: 1, timeout: 5000 });
    }
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Ollama
// ---------------------------------------------------------------------------

async function callOllama(
  config: LlmConfig,
  model: string,
  messages: ChatMessage[],
  opts: ChatOptions | undefined,
  timeout: number,
): Promise<ChatResult> {
  const url = `${config.host}/api/chat`;

  const ollamaOpts: Record<string, unknown> = {};
  if (opts?.temperature !== undefined) ollamaOpts.temperature = opts.temperature;
  if (opts?.numCtx !== undefined) ollamaOpts.num_ctx = opts.numCtx;
  if (opts?.maxTokens !== undefined) ollamaOpts.num_predict = opts.maxTokens;

  const body: Record<string, unknown> = {
    model,
    messages,
    stream: false,
    options: Object.keys(ollamaOpts).length > 0 ? ollamaOpts : undefined,
  };
  if (opts?.think !== undefined) body.think = opts.think;

  const response = await axios.post(url, body, {
    timeout,
    signal: AbortSignal.timeout(timeout),
  });

  return {
    content: response.data?.message?.content ?? "",
    model,
    provider: "ollama",
  };
}

// ---------------------------------------------------------------------------
// OpenAI
// ---------------------------------------------------------------------------

async function callOpenAI(
  config: LlmConfig,
  model: string,
  messages: ChatMessage[],
  opts: ChatOptions | undefined,
  timeout: number,
): Promise<ChatResult> {
  // Normalize host to avoid common misconfigurations
  const rawHost = config.host || "https://api.openai.com";
  const baseHost = rawHost.replace(/\/$/, "").replace(/\/v1(?:\/chat\/completions)?\/?$/, "");
  const url = `${baseHost}/v1/chat/completions`;

  const body: Record<string, unknown> = {
    model,
    messages,
  };
  if (opts?.temperature !== undefined) body.temperature = opts.temperature;
  if (opts?.maxTokens !== undefined) body.max_tokens = opts.maxTokens;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const apiKey = opts?.apiKey ?? config.apiKey ?? process.env["OPENAI_API_KEY"];
  if (apiKey) {
    headers["Authorization"] = `Bearer ${apiKey}`;
  }

  let response;
  try {
    response = await axios.post(url, body, {
      headers,
      timeout,
      signal: AbortSignal.timeout(timeout),
    });
  } catch (err) {
    if (axios.isAxiosError(err)) {
      const status = err.response?.status ?? err.code ?? "network";
      const msg = (err.response?.data as any)?.error?.message ?? err.message;
      throw new Error(`OpenAI request failed (${status}): ${msg} — url=${url}`);
    }
    throw err;
  }

  const choice = response.data?.choices?.[0];
  return {
    content: choice?.message?.content ?? "",
    model: response.data?.model ?? model,
    provider: "openai",
  };
}

// ---------------------------------------------------------------------------
// Anthropic
// ---------------------------------------------------------------------------

async function callAnthropic(
  config: LlmConfig,
  model: string,
  messages: ChatMessage[],
  opts: ChatOptions | undefined,
  timeout: number,
): Promise<ChatResult> {
  const apiKey = opts?.apiKey ?? config.apiKey ?? process.env["ANTHROPIC_API_KEY"];
  if (!apiKey) {
    throw new Error("Anthropic API key not configured. Set ANTHROPIC_API_KEY or provider.apiKey in personality.");
  }

  // Separate system messages from conversation messages
  const systemParts: string[] = [];
  const convMessages: { role: string; content: string }[] = [];
  for (const msg of messages) {
    if (msg.role === "system") {
      systemParts.push(msg.content);
    } else {
      convMessages.push({ role: msg.role, content: msg.content });
    }
  }

  const body: Record<string, unknown> = {
    model,
    max_tokens: opts?.maxTokens ?? config.maxTokens ?? 4096,
    messages: convMessages,
  };
  if (systemParts.length > 0) body.system = systemParts.join("\n\n");
  if (opts?.temperature !== undefined) body.temperature = opts.temperature;

  const response = await axios.post(
    "https://api.anthropic.com/v1/messages",
    body,
    {
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
      },
      timeout,
      signal: AbortSignal.timeout(timeout),
    },
  );

  const content = response.data?.content;
  const text = Array.isArray(content)
    ? content.filter((b: { type: string }) => b.type === "text").map((b: { text: string }) => b.text).join("")
    : "";

  return {
    content: text,
    model: response.data?.model ?? model,
    provider: "anthropic",
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isNetworkError(err: unknown): boolean {
  if (!(err instanceof Error)) return false;

  // Axios error with a network code
  const axErr = err as AxiosError;
  if (axErr.code && NETWORK_CODES.has(axErr.code)) return true;

  // Axios error: no response received (network level)
  if (axErr.isAxiosError && !axErr.response) return true;

  // AbortSignal timeout
  if (err.name === "AbortError" || err.name === "TimeoutError") return true;

  // Check nested cause
  if ((err as { cause?: unknown }).cause) {
    return isNetworkError((err as { cause: unknown }).cause);
  }

  return false;
}
