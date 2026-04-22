#!/usr/bin/env npx tsx
/**
 * mirror-codebase-c.ts
 *
 * Generates "code mirror" markdown notes for every C source file (.c) under a
 * scan directory. Mirrors are stored under md_db/code, matching the source
 * directory layout.
 *
 * Extracts from each .c file (best-effort regex parsing, no full compiler):
 *   - Includes (#include <...> / "...") classified as system vs local
 *   - Top-level function definitions
 *     - Exported when not declared static and name does not start with '_'
 *   - In-repo call graph (name-based, across mirrored files only)
 *   - Call-ins (inverse edges)
 *
 * Mirrors follow the same tiering as TS/PY scripts:
 *   - Tier 2: file notes (one per .c file)
 *   - Tier 1: symbol notes (exported functions)
 *   - Tier 3: module notes (one per directory)
 *
 * Usage:
 *   npx tsx automation/scripts/mirror-codebase-c.ts [--scan-dir DIR] [--mirror-dir DIR] [--omit PATTERNS] [--force]
 */

import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);

const DEFAULT_OMIT = [
  "build",
  "dist",
  "out",
  "cmake-build-debug",
  "cmake-build-release",
  "node_modules",
  ".git",
  ".context-garden",
];

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface CliArgs {
  scanDir: string;
  mirrorDir: string;
  omitPatterns: string[];
  force: boolean;
}

interface IncludeInfo {
  specifier: string;
  isSystem: boolean;
}

interface ExportInfo {
  name: string;
  signature: string;
  isStatic: boolean;
}

interface FunctionInfo {
  name: string;
  signature: string;
  isExported: boolean;
  isStatic: boolean;
  bodyStart: number;
  bodyEnd: number;
}

interface CallSite {
  name: string;
  position: number;
}

interface InRepoCall {
  calleeName: string;
  sourceFile: string;
  position: number;
}

interface CallIn {
  callerFile: string;
  calledName: string;
  callerName?: string;
}

interface FileData {
  absolutePath: string;
  relativePath: string;
  mirrorPath: string;
  includes: IncludeInfo[];
  exports: ExportInfo[];
  functions: FunctionInfo[];
  callSites: CallSite[];
  inRepoCalls: InRepoCall[];
  callIns: CallIn[];
}

export interface MirrorCOptions {
  scanDir: string;
  mirrorDir: string;
  omitPatterns: string[];
  force: boolean;
  workspace?: string;
  wikilinkPrefix?: string;
}

export interface MirrorCResult {
  written: number;
  skipped: number;
  validPaths: Set<string>;
}

// ---------------------------------------------------------------------------
// CLI helpers
// ---------------------------------------------------------------------------

function parseCliArgs(): CliArgs {
  let scanDir = process.cwd();
  let mirrorDir = path.join(scanDir, "md_db", "code");
  let omitPatterns = DEFAULT_OMIT;
  const force = process.argv.includes("--force");

  for (let i = 2; i < process.argv.length; i++) {
    const arg = process.argv[i];
    if (arg === "--scan-dir" && process.argv[i + 1]) {
      scanDir = path.resolve(process.argv[++i]);
    } else if (arg === "--mirror-dir" && process.argv[i + 1]) {
      mirrorDir = path.resolve(process.argv[++i]);
    } else if (arg === "--omit" && process.argv[i + 1]) {
      omitPatterns = process.argv[++i].split(",").map((s) => s.trim()).filter(Boolean);
    }
  }

  return { scanDir, mirrorDir, omitPatterns, force };
}

// ---------------------------------------------------------------------------
// File discovery
// ---------------------------------------------------------------------------

function mirrorStem(relPath: string): string {
  const stem = path.posix.basename(relPath).replace(/\.c$/, "");
  if (stem === "main") {
    const parentDir = path.posix.dirname(relPath);
    const parent = parentDir === "." ? "root" : path.posix.basename(parentDir);
    return `${parent}-main`;
  }
  return stem;
}

function shouldOmit(relPath: string, patterns: string[]): boolean {
  const parts = relPath.split("/");
  // Skip dot-prefixed directories (Obsidian ignores them)
  if (parts.some((p) => p.startsWith(".") && p !== ".")) return true;
  for (const pattern of patterns) {
    if (pattern.startsWith("*.")) {
      const suffix = pattern.slice(1);
      if (relPath.endsWith(suffix)) return true;
    } else {
      if (parts.some((p) => p === pattern)) return true;
    }
  }
  return false;
}

function collectFiles(
  scanDir: string,
  mirrorDir: string,
  omitPatterns: string[],
): { absolutePath: string; relativePath: string; mirrorPath: string }[] {
  const results: { absolutePath: string; relativePath: string; mirrorPath: string }[] = [];

  function walk(dir: string) {
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      const absPath = path.join(dir, entry.name);
      const relPath = path.relative(scanDir, absPath).replace(/\\/g, "/");
      if (shouldOmit(relPath, omitPatterns)) continue;

      if (entry.isDirectory()) {
        walk(absPath);
      } else if (entry.isFile() && entry.name.endsWith(".c")) {
        const mirrorRel = path.posix.join(path.posix.dirname(relPath), mirrorStem(relPath) + ".md");
        const mirrorPath = path.join(mirrorDir, mirrorRel);
        results.push({ absolutePath: absPath, relativePath: relPath, mirrorPath });
      }
    }
  }

  walk(scanDir);
  return results;
}

// ---------------------------------------------------------------------------
// Extraction helpers
// ---------------------------------------------------------------------------

const C_KEYWORDS = new Set([
  "auto",
  "break",
  "case",
  "char",
  "const",
  "continue",
  "default",
  "do",
  "double",
  "else",
  "enum",
  "extern",
  "float",
  "for",
  "goto",
  "if",
  "inline",
  "int",
  "long",
  "register",
  "restrict",
  "return",
  "short",
  "signed",
  "sizeof",
  "static",
  "struct",
  "switch",
  "typedef",
  "union",
  "unsigned",
  "void",
  "volatile",
  "while",
  "_Alignas",
  "_Alignof",
  "_Atomic",
  "_Bool",
  "_Complex",
  "_Generic",
  "_Imaginary",
  "_Noreturn",
  "_Static_assert",
  "_Thread_local",
]);

const CONTROL_KEYWORDS = new Set(["if", "for", "while", "switch", "do", "else"]);

function extractIncludes(source: string): IncludeInfo[] {
  const includes: IncludeInfo[] = [];
  const includeRe = /^\s*#\s*include\s*[<"]([^>"]+)[>"]/gm;
  let match: RegExpExecArray | null;
  while ((match = includeRe.exec(source)) !== null) {
    const specifier = match[1].trim();
    const isSystem = source[match.index + match[0].indexOf("<")] === "<";
    includes.push({ specifier, isSystem });
  }
  return includes;
}

function normalizeWhitespace(text: string): string {
  return text
    .split("\n")
    .map((line) => line.trim())
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

function findMatchingBrace(text: string, startIndex: number): number {
  let depth = 0;
  for (let i = startIndex; i < text.length; i++) {
    const char = text[i];
    if (char === "{") depth++;
    else if (char === "}") {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return text.length;
}

function extractFunctions(source: string): FunctionInfo[] {
  const functions: FunctionInfo[] = [];

  const functionRegex = /(^|\n)\s*((?:static\s+)?(?:inline\s+)?(?:[_A-Za-z][\w\s\*]*?))\s+([A-Za-z_][\w]*)\s*\(([^;]*)\)\s*\{/gm;
  let match: RegExpExecArray | null;
  while ((match = functionRegex.exec(source)) !== null) {
    const returnPart = normalizeWhitespace(match[2]);
    if (CONTROL_KEYWORDS.has(returnPart.split(" ")[0] ?? "")) continue;

    const name = match[3];
    const signatureStart = match.index + (match[1]?.length ?? 0);
    const braceIndex = source.indexOf("{", functionRegex.lastIndex - 1);
    if (braceIndex === -1) continue;
    const bodyEnd = findMatchingBrace(source, braceIndex);

    const rawSignature = source.slice(signatureStart, braceIndex).trim();
    const signature = normalizeWhitespace(rawSignature) + ")";

    const modifiers = returnPart.split(" ");
    const isStatic = modifiers.includes("static");
    const isExported = !isStatic && !name.startsWith("_");

    functions.push({
      name,
      signature,
      isExported,
      isStatic,
      bodyStart: signatureStart,
      bodyEnd,
    });
  }

  return functions;
}

function extractExports(functions: FunctionInfo[]): ExportInfo[] {
  const exports: ExportInfo[] = [];
  for (const fn of functions) {
    if (fn.isExported) {
      exports.push({ name: fn.name, signature: fn.signature, isStatic: fn.isStatic });
    }
  }
  return exports;
}

function extractCallSites(source: string): CallSite[] {
  const sites: CallSite[] = [];
  const callRe = /\b([A-Za-z_][\w]*)\s*\(/g;
  let match: RegExpExecArray | null;
  while ((match = callRe.exec(source)) !== null) {
    const name = match[1];
    if (C_KEYWORDS.has(name)) continue;
    const prevChar = source[match.index - 1];
    if (prevChar && /[A-Za-z0-9_]/.test(prevChar)) continue; // part of identifier like fooBar(
    sites.push({ name, position: match.index });
  }
  return sites;
}

function extractFileData(
  absolutePath: string,
  relativePath: string,
  mirrorPath: string,
): FileData {
  const source = fs.readFileSync(absolutePath, "utf8");
  const functions = extractFunctions(source);
  const exports = extractExports(functions);
  return {
    absolutePath,
    relativePath,
    mirrorPath,
    includes: extractIncludes(source),
    exports,
    functions,
    callSites: extractCallSites(source),
    inRepoCalls: [],
    callIns: [],
  };
}

// ---------------------------------------------------------------------------
// Call graph construction
// ---------------------------------------------------------------------------

function buildCallGraph(files: FileData[]): void {
  const symbolMap = new Map<string, FileData[]>();
  for (const file of files) {
    for (const exp of file.exports) {
      if (!symbolMap.has(exp.name)) symbolMap.set(exp.name, []);
      symbolMap.get(exp.name)!.push(file);
    }
  }

  for (const file of files) {
    for (const site of file.callSites) {
      const targets = symbolMap.get(site.name);
      if (!targets) continue;
      for (const target of targets) {
        if (target.relativePath === file.relativePath) continue;

        if (!file.inRepoCalls.some((c) => c.calleeName === site.name && c.sourceFile === target.relativePath)) {
          file.inRepoCalls.push({ calleeName: site.name, sourceFile: target.relativePath, position: site.position });
        }

        let callerName: string | undefined;
        const callerFn = file.functions.find((fn) => site.position >= fn.bodyStart && site.position <= fn.bodyEnd);
        if (callerFn && callerFn.isExported) {
          callerName = callerFn.name;
        }

        if (!target.callIns.some((c) => c.callerFile === file.relativePath && c.calledName === site.name && c.callerName === callerName)) {
          target.callIns.push({ callerFile: file.relativePath, calledName: site.name, callerName });
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Markdown helpers
// ---------------------------------------------------------------------------

const MIRROR_PREFIX = "code";

function toWikiLink(relPath: string, prefix = MIRROR_PREFIX): string {
  const dir = path.posix.dirname(relPath);
  const stem = mirrorStem(relPath);
  return dir === "." ? `${prefix}/${stem}` : `${prefix}/${dir}/${stem}`;
}

function sanitizeSymbolName(name: string): string {
  return name.replace(/[^A-Za-z0-9_]+/g, "_").replace(/_{2,}/g, "_").replace(/^_|_$/g, "");
}

function toSymbolWikiLink(relPath: string, symbolName: string, prefix = MIRROR_PREFIX): string {
  const fileLink = toWikiLink(relPath, prefix);
  return `${fileLink}/${sanitizeSymbolName(symbolName)}`;
}

function symbolNotePath(mirrorDir: string, relPath: string, symbolName: string): string {
  const dirRel = path.posix.dirname(relPath);
  const stem = mirrorStem(relPath);
  const subdir = dirRel === "." ? stem : `${dirRel}/${stem}`;
  return path.join(mirrorDir, subdir, sanitizeSymbolName(symbolName) + ".md");
}

function baseModuleNotePath(mirrorDir: string, dirRel: string): string {
  return dirRel === "."
    ? path.join(mirrorDir, "root_module.md")
    : path.join(mirrorDir, dirRel + "_module.md");
}

function readFrontmatterLanguage(notePath: string): string | null {
  let content: string;
  try {
    content = fs.readFileSync(notePath, "utf8");
  } catch {
    return null;
  }
  const match = content.match(/^---\n([\s\S]*?)\n---/);
  if (!match) return null;
  const lang = match[1].match(/^language:\s*(\S+)\s*$/m);
  return lang ? lang[1] : null;
}

function moduleNotePath(mirrorDir: string, dirRel: string, language = "c"): string {
  const basePath = baseModuleNotePath(mirrorDir, dirRel);
  const existingLanguage = readFrontmatterLanguage(basePath);
  if (!fs.existsSync(basePath) || existingLanguage === language || existingLanguage === null) {
    return basePath;
  }
  return basePath.replace(/\.md$/, `_${language}.md`);
}

function moduleWikiLink(mirrorDir: string, dirRel: string, prefix: string, language = "c"): string {
  const rel = path.relative(mirrorDir, moduleNotePath(mirrorDir, dirRel, language)).replace(/\\/g, "/").replace(/\.md$/, "");
  return `${prefix}/${rel}`;
}

function buildDirectoryEntries(files: FileData[]): Map<string, { relPath: string; stem: string }[]> {
  const byDir = new Map<string, { relPath: string; stem: string }[]>();
  if (files.length === 0) return byDir;
  const ensureDir = (dirRel: string) => {
    if (!byDir.has(dirRel)) byDir.set(dirRel, []);
  };

  ensureDir(".");
  for (const file of files) {
    const dirRel = path.posix.dirname(file.relativePath);
    ensureDir(dirRel);
    byDir.get(dirRel)!.push({ relPath: file.relativePath, stem: mirrorStem(file.relativePath) });

    let current = dirRel;
    while (current !== ".") {
      current = path.posix.dirname(current);
      ensureDir(current);
    }
  }

  return byDir;
}

function extractSummarySection(mirrorPath: string): string | null {
  let content: string;
  try {
    content = fs.readFileSync(mirrorPath, "utf8");
  } catch {
    return null;
  }
  const idx = content.indexOf("\n## Summary");
  if (idx === -1) return null;
  return content.slice(idx + 1).trimEnd();
}

function generateSymbolNote(
  file: FileData,
  exp: ExportInfo,
  _today: string,
  prefix = MIRROR_PREFIX,
  workspace?: string,
  moduleLinkByDir?: Map<string, string>,
): string {
  const lines: string[] = [];
  const dirRel = path.posix.dirname(file.relativePath);
  const stem = mirrorStem(file.relativePath);
  const parentFileLink = dirRel === "." ? `${prefix}/${stem}` : `${prefix}/${dirRel}/${stem}`;
  const parentModuleLink = moduleLinkByDir?.get(dirRel)
    ?? (dirRel === "." ? `${prefix}/root_module` : `${prefix}/${dirRel}_module`);

  lines.push(
    "---",
    "type: codeSymbol",
    "tier: 1",
    `path: ${file.relativePath}`,
    `parentFile: ${parentFileLink}`,
    `parentModule: ${parentModuleLink}`,
    "symbolKind: function",
    "language: c",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    "---",
    "",
  );

  lines.push(`# ${exp.name}`, "");
  lines.push(`**Signature:** \`${exp.signature}\``, "");
  lines.push(`**File:** [[${parentFileLink}]]`, "");

  const fn = file.functions.find((f) => f.name === exp.name && f.isExported);
  if (fn) {
    const ownCalls = file.inRepoCalls.filter(
      (c) => c.position >= fn.bodyStart && c.position <= fn.bodyEnd,
    );
    if (ownCalls.length) {
      lines.push("## Calls Into", "");
      const seenTargets = new Set<string>();
      for (const call of ownCalls) {
        const target = toSymbolWikiLink(call.sourceFile, call.calleeName, prefix);
        if (seenTargets.has(target)) continue;
        seenTargets.add(target);
        lines.push(`- [[${target}|${call.calleeName}]]`);
      }
      lines.push("");
    }
  }

  const callers = file.callIns.filter((c) => c.calledName === exp.name);
  if (callers.length) {
    lines.push("## Called By", "");
    const seen = new Set<string>();
    for (const caller of callers) {
      if (caller.callerName) {
        const symLink = toSymbolWikiLink(caller.callerFile, caller.callerName, prefix);
        if (seen.has(symLink)) continue;
        seen.add(symLink);
        lines.push(`- [[${symLink}|${caller.callerName}]]`);
      } else {
        const fileLink = toWikiLink(caller.callerFile, prefix);
        if (seen.has(fileLink)) continue;
        seen.add(fileLink);
        lines.push(`- [[${fileLink}]]`);
      }
    }
    lines.push("");
  }

  return lines.join("\n");
}

function generateModuleNote(
  dirRel: string,
  fileEntries: { relPath: string; stem: string }[],
  _today: string,
  prefix = MIRROR_PREFIX,
  workspace?: string,
  childModuleLinks: string[] = [],
  parentModuleLink?: string,
): string {
  const lines: string[] = [];
  const dirName = dirRel === "." ? "root" : path.posix.basename(dirRel);

  lines.push(
    "---",
    "type: codeModule",
    "tier: 3",
    `path: ${dirRel}`,
    `title: ${dirName}`,
    "language: c",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    "---",
    "",
  );

  lines.push(`# ${dirName}`, "");
  lines.push("*Module summary not yet generated.*", "");

  if (parentModuleLink) {
    lines.push("## Parent", "");
    lines.push(`- [[${parentModuleLink}]]`, "");
  }

  if (childModuleLinks.length > 0) {
    lines.push("## Submodules", "");
    for (const link of childModuleLinks) lines.push(`- ${link}`);
    lines.push("");
  }

  lines.push("## Files", "");
  for (const { relPath, stem } of fileEntries) {
    const d = path.posix.dirname(relPath);
    const link = d === "." ? `${prefix}/${stem}` : `${prefix}/${d}/${stem}`;
    lines.push(`- [[${link}]]`);
  }
  lines.push("");

  return lines.join("\n");
}

function generateFileMarkdown(
  file: FileData,
  _allFiles: FileData[],
  _today: string,
  prefix = MIRROR_PREFIX,
  workspace?: string,
  moduleLinkByDir?: Map<string, string>,
): string {
  const lines: string[] = [];
  const filename = path.basename(file.relativePath);
  const dirRel = path.posix.dirname(file.relativePath);
  const parentModuleLink = moduleLinkByDir?.get(dirRel)
    ?? (dirRel === "." ? `${prefix}/root_module` : `${prefix}/${dirRel}_module`);

  lines.push(
    "---",
    "type: codeUnit",
    "tier: 2",
    `path: ${file.relativePath}`,
    `parentModule: ${parentModuleLink}`,
    "language: c",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    "---",
    "",
  );

  const title = filename;
  lines.push(`# ${title}`, "");
  lines.push(`> \`${file.relativePath}\``, "");

  if (file.exports.length) {
    lines.push("## Exports", "");
    for (const exp of file.exports) {
      lines.push(`- \`${exp.signature}\``);
    }
    lines.push("");
  }

  if (file.includes.length) {
    lines.push("## Includes", "");
    for (const inc of file.includes) {
      lines.push(`- ${inc.isSystem ? `<${inc.specifier}>` : `"${inc.specifier}"`}`);
    }
    lines.push("");
  }

  if (file.functions.length) {
    lines.push("## Functions", "");
    for (const fn of file.functions) {
      const flags: string[] = [];
      if (fn.isStatic) flags.push("static");
      if (fn.isExported) flags.push("exported");
      const flagStr = flags.length ? ` *(${flags.join(", ")})*` : "";
      lines.push(`### \`${fn.signature}\`${flagStr}`);

      const ownCalls = file.inRepoCalls.filter((c) => c.position >= fn.bodyStart && c.position <= fn.bodyEnd);
      if (ownCalls.length) {
        const bySource = new Map<string, Set<string>>();
        for (const c of ownCalls) {
          if (!bySource.has(c.sourceFile)) bySource.set(c.sourceFile, new Set());
          bySource.get(c.sourceFile)!.add(c.calleeName);
        }
        const parts: string[] = [];
        for (const [srcFile, names] of bySource) {
          parts.push(`[[${toWikiLink(srcFile, prefix)}]] (\`${[...names].join("`, `")}\`)`);
        }
        lines.push(`*Calls into: ${parts.join(", ")}*`);
      }
      lines.push("");
    }
  }

  if (file.inRepoCalls.length) {
    lines.push("## In-Repo Calls", "");
    lines.push("Functions this file calls in other mirrored files:", "");
    const bySource = new Map<string, Set<string>>();
    for (const c of file.inRepoCalls) {
      if (!bySource.has(c.sourceFile)) bySource.set(c.sourceFile, new Set());
      bySource.get(c.sourceFile)!.add(c.calleeName);
    }
    for (const [srcFile, names] of bySource) {
      lines.push(`- [[${toWikiLink(srcFile, prefix)}]] — \`${[...names].join("`, `")}\``);
    }
    lines.push("");
  }

  if (file.callIns.length) {
    lines.push("## Call-Ins", "");
    lines.push("Mirrored files that call exports from this file:", "");
    const byCaller = new Map<string, Set<string>>();
    for (const c of file.callIns) {
      if (!byCaller.has(c.callerFile)) byCaller.set(c.callerFile, new Set());
      byCaller.get(c.callerFile)!.add(c.calledName);
    }
    for (const [callerFile, names] of byCaller) {
      lines.push(`- [[${toWikiLink(callerFile, prefix)}]] — calls \`${[...names].join("`, `")}\``);
    }
    lines.push("");
  }

  return lines.join("\n");
}

function extractModuleFileLinks(modulePath: string): string[] | null {
  let content: string;
  try {
    content = fs.readFileSync(modulePath, "utf8");
  } catch {
    return null;
  }
  const section = content.match(/^## Files\s*\n((?:- \[\[.+\]\]\n?)*)/m);
  if (!section) return null;
  return section[1].trim().split("\n").map((l) => l.replace(/^- /, "").trim());
}

function extractModuleSubmoduleLinks(modulePath: string): string[] {
  let content: string;
  try {
    content = fs.readFileSync(modulePath, "utf8");
  } catch {
    return [];
  }
  const section = content.match(/^## Submodules\s*\n((?:- \[\[.+\]\]\n?)*)/m);
  if (!section) return [];
  return section[1].trim().split("\n").map((l) => l.replace(/^- /, "").trim());
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function runMirrorC(
  opts: MirrorCOptions,
): Promise<MirrorCResult> {
  const today = new Date().toISOString().slice(0, 10);
  const discovered = collectFiles(opts.scanDir, opts.mirrorDir, opts.omitPatterns);

  const files: FileData[] = [];
  let parseErrors = 0;
  for (const { absolutePath, relativePath, mirrorPath } of discovered) {
    try {
      files.push(extractFileData(absolutePath, relativePath, mirrorPath));
    } catch (err) {
      console.warn(`[mirror-c] WARN: failed to parse ${relativePath}: ${err}`);
      parseErrors++;
    }
  }

  buildCallGraph(files);

  const prefix = opts.wikilinkPrefix ?? MIRROR_PREFIX;
  const validPaths = new Set<string>();
  let written = 0;
  let skipped = 0;
  const byDir = buildDirectoryEntries(files);
  const moduleLinkByDir = new Map(
    [...byDir.keys()].map((dirRel) => [dirRel, moduleWikiLink(opts.mirrorDir, dirRel, prefix, "c")]),
  );

  for (const file of files) {
    validPaths.add(path.resolve(file.mirrorPath));

    const definedSymbols = file.exports.map((exp) => exp.name);
    for (const symbol of definedSymbols) {
      validPaths.add(path.resolve(symbolNotePath(opts.mirrorDir, file.relativePath, symbol)));
    }

    let isStale = true;
    if (!opts.force) {
      try {
        const srcStat = fs.statSync(file.absolutePath);
        const mirrorStat = fs.statSync(file.mirrorPath);
        if (mirrorStat.mtimeMs >= srcStat.mtimeMs) {
          isStale = false;
        }
      } catch {
        // missing mirror — treat as stale
      }
    }

    if (!isStale) {
      for (const symbol of definedSymbols) {
        const symPath = symbolNotePath(opts.mirrorDir, file.relativePath, symbol);
        if (!fs.existsSync(symPath)) {
          const symMarkdown = generateSymbolNote(file, file.exports.find((e) => e.name === symbol)!, today, prefix, opts.workspace, moduleLinkByDir);
          const preserved = extractSummarySection(symPath);
          const final = preserved ? symMarkdown.trimEnd() + "\n\n" + preserved + "\n" : symMarkdown;
          fs.mkdirSync(path.dirname(symPath), { recursive: true });
          fs.writeFileSync(symPath, final, "utf8");
        }
      }
      skipped++;
      continue;
    }

    const markdown = generateFileMarkdown(file, files, today, prefix, opts.workspace, moduleLinkByDir);
    const preserved = extractSummarySection(file.mirrorPath);
    const final = preserved ? markdown.trimEnd() + "\n\n" + preserved + "\n" : markdown;
    fs.mkdirSync(path.dirname(file.mirrorPath), { recursive: true });
    fs.writeFileSync(file.mirrorPath, final, "utf8");
    written++;

    for (const exp of file.exports) {
      const symPath = symbolNotePath(opts.mirrorDir, file.relativePath, exp.name);
      const symMarkdown = generateSymbolNote(file, exp, today, prefix, opts.workspace, moduleLinkByDir);
      const preserved = extractSummarySection(symPath);
      const final = preserved ? symMarkdown.trimEnd() + "\n\n" + preserved + "\n" : symMarkdown;
      fs.mkdirSync(path.dirname(symPath), { recursive: true });
      fs.writeFileSync(symPath, final, "utf8");
    }
  }

  for (const [dirRel, fileEntries] of byDir) {
    const modPath = moduleNotePath(opts.mirrorDir, dirRel, "c");
    validPaths.add(path.resolve(modPath));

    const childModuleLinksSet = new Set<string>();
    for (const key of byDir.keys()) {
      if (path.posix.dirname(key) === dirRel && key !== dirRel) {
        childModuleLinksSet.add(`[[${moduleLinkByDir.get(key)}]]`);
      }
    }

    const mirrorSubdir = dirRel === "." ? opts.mirrorDir : path.join(opts.mirrorDir, dirRel);
    try {
      for (const entry of fs.readdirSync(mirrorSubdir, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        if (fs.existsSync(path.join(mirrorSubdir, entry.name + "_module.md"))) {
          const link = dirRel === "." ? `${prefix}/${entry.name}_module` : `${prefix}/${dirRel}/${entry.name}_module`;
          childModuleLinksSet.add(`[[${link}]]`);
        }
      }
    } catch {
      // directory may not exist yet
    }
    const childModuleLinks = [...childModuleLinksSet].sort();
    const parentModuleLink = dirRel === "." ? undefined : moduleLinkByDir.get(path.posix.dirname(dirRel));

    const existingFileLinks = extractModuleFileLinks(modPath);
    const existingSubmoduleLinks = extractModuleSubmoduleLinks(modPath);
    const currentFileLinks = fileEntries
      .map(({ relPath, stem }) => {
        const d = path.posix.dirname(relPath);
        return d === "." ? `[[${prefix}/${stem}]]` : `[[${prefix}/${d}/${stem}]]`;
      })
      .sort();

    const needsWrite = opts.force || existingFileLinks === null
      || JSON.stringify(existingFileLinks.sort()) !== JSON.stringify(currentFileLinks)
      || JSON.stringify(existingSubmoduleLinks.sort()) !== JSON.stringify(childModuleLinks);

    if (needsWrite) {
      const modMarkdown = generateModuleNote(dirRel, fileEntries, today, prefix, opts.workspace, childModuleLinks, parentModuleLink);
      const preserved = extractSummarySection(modPath);
      const final = preserved ? modMarkdown.trimEnd() + "\n\n" + preserved + "\n" : modMarkdown;
      fs.mkdirSync(path.dirname(modPath), { recursive: true });
      fs.writeFileSync(modPath, final, "utf8");
    }
  }

  if (parseErrors > 0) console.warn(`[mirror-c] ${parseErrors} files failed to parse`);

  return { written, skipped, validPaths };
}

// ---------------------------------------------------------------------------
// CLI entrypoint
// ---------------------------------------------------------------------------

async function main() {
  const args = parseCliArgs();
  const { scanDir, mirrorDir, omitPatterns, force } = args;

  const { written, skipped } = await runMirrorC({
    scanDir,
    mirrorDir,
    omitPatterns,
    force,
  });

  console.log(`[mirror-c] written=${written} skipped=${skipped}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
