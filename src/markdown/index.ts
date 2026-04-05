/**
 * Shared markdown utilities — canonical handlers for frontmatter, wikilinks,
 * and token normalization used throughout the system.
 */

export { parseFrontmatter, buildFrontmatter, type FrontmatterResult } from "./frontmatter.js";
export { extractWikilinks, parseWikiLinks, extractSimpleTargets, isValidWikiLinkSyntax, type WikiLink, type ParsedLinks } from "./wikilinks.js";
export { normalizeToken, normalizeTokens, normalizeTagList, extractTags } from "./tokens.js";
export { normalizeMdDb, type NormalizationIssue, type NormalizationResult, type NormalizeOptions } from "./normalizer.js";
export {
  lintMdDb,
  lintFile,
  type LintIssue,
  type LintResult,
  type LintOptions,
  type SingleFileLintOptions,
} from "./markdown-linter.js";
