/**
 * Context Reviewer — synthesis step between retrieval and MCP delivery.
 *
 * Takes the raw formatted context + query and asks the configured
 * personality's LLM to synthesize a focused, query-specific context
 * document. The LLM outputs natural language (markdown), not structured
 * JSON — no fragile parsing required.
 *
 * Always-on by default. Falls back to raw context on LLM/network errors.
 * Set `enabled: false` to explicitly disable.
 */

import { llmChat } from "../../llm/client.js";
import { loadPersonality, resolvePersonalityChatOptions, type PersonalityConfig } from "../../llm/personality-loader.js";
import type { RetrievedNote } from "../types.js";

// Fallback when no personality content is loaded
const CONTEXT_REVIEW_FALLBACK_SYSTEM_PROMPT =
  "You synthesize retrieved context into focused, query-relevant summaries.";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ContextReviewConfig {
  /** Whether review is enabled. Default: true (always-on). */
  enabled: boolean;

  /** Personality name to use for the review. Default: "context-synthesizer". */
  personality: string;

  /** Max time for the review call in ms. Default: 15000. */
  maxLatencyMs: number;

  /** Path to personalities directory. */
  personalitiesDir: string;

  /**
   * Minimum score required on the best seed note to proceed with synthesis.
   * Below this threshold the raw notes are too weakly matched for the LLM to
   * synthesize reliably — skip to prevent hallucination. Default: 0.45.
   */
  minSeedScore: number;
}

export interface ReviewResult {
  /** Synthesized context document. Null if review was skipped or failed. */
  synthesizedContext: string | null;

  /** Time taken for review in ms. */
  reviewMs: number;

  /** Whether the review was skipped. */
  skipped: boolean;

  /** Reason for skipping, if skipped. For errors this is prefixed with "error: " + the message. */
  skipReason?: "disabled" | "timeout" | "no_notes" | "low_relevance" | string;
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

const DEFAULTS: ContextReviewConfig = {
  enabled: true,
  personality: "context-synthesizer",
  maxLatencyMs: 20_000,
  personalitiesDir: "",
  minSeedScore: 0.45,
};

// ---------------------------------------------------------------------------
// ContextReviewer
// ---------------------------------------------------------------------------

export class ContextReviewer {
  private readonly config: ContextReviewConfig;

  constructor(config: Partial<ContextReviewConfig>) {
    this.config = { ...DEFAULTS, ...config };
  }

  /**
   * Review retrieved context and produce a synthesized, query-focused version.
   *
   * @param query              The user's original query.
   * @param notes              Retrieved notes (used for no-notes skip check).
   * @param rawFormattedContext Pre-formatted context from formatContext() — sent to the LLM as input.
   * @returns ReviewResult with synthesizedContext (the LLM's output) or null if skipped/failed.
   */
  async review(query: string, notes: RetrievedNote[], rawFormattedContext: string): Promise<ReviewResult> {
    const t0 = Date.now();

    // Skip if disabled
    if (!this.config.enabled) {
      return { synthesizedContext: null, reviewMs: 0, skipped: true, skipReason: "disabled" };
    }

    // Skip if no notes
    if (notes.length === 0) {
      return { synthesizedContext: null, reviewMs: 0, skipped: true, skipReason: "no_notes" };
    }

    // Skip if best seed score is below threshold
    const maxSeedScore = Math.max(
      ...notes.filter((n) => (n.depth ?? 0) === 0).map((n) => n.score),
      0,
    );
    if (maxSeedScore < this.config.minSeedScore) {
      return { synthesizedContext: null, reviewMs: 0, skipped: true, skipReason: "low_relevance" };
    }

    // Load personality for system prompt and model config
    const personality = loadPersonality(this.config.personality, this.config.personalitiesDir);

    try {
      const synthesized = await this.callSynthesizeLLM(query, rawFormattedContext, personality);
      return {
        synthesizedContext: synthesized,
        reviewMs: Date.now() - t0,
        skipped: false,
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        synthesizedContext: null,
        reviewMs: Date.now() - t0,
        skipped: true,
        skipReason: `error: ${msg}`,
      };
    }
  }

  /**
   * LLM call for context synthesis.
   */
  private async callSynthesizeLLM(
    query: string,
    rawContext: string,
    personality: PersonalityConfig | null,
  ): Promise<string> {
    const systemPrompt = personality?.content ?? CONTEXT_REVIEW_FALLBACK_SYSTEM_PROMPT;

    const userPrompt = [
      `## Query`,
      `"${query}"`,
      ``,
      `## Retrieved Context`,
      rawContext,
    ].join("\n");

    const result = await llmChat(
      [
        { role: "system", content: systemPrompt },
        { role: "user", content: userPrompt },
      ],
      {
        ...resolvePersonalityChatOptions(personality),
        timeout: this.config.maxLatencyMs,
      },
    );

    const content = result.content;
    if (!content.trim()) {
      throw new Error("Empty response from synthesis LLM");
    }

    return content.trim();
  }
}
