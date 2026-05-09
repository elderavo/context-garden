#!/usr/bin/env npx tsx
/**
 * mirror-codebase.ts
 *
 * Generates a "code mirror" markdown note for every TypeScript file in the
 * project, placing notes in a mirror directory that preserves the source
 * directory structure.
 *
 * Extracts from each .ts file:
 *   - Imports (internal vs external, named bindings)
 *   - Exports (functions, classes, types, interfaces, enums, consts)
 *   - Function definitions (signatures, async flag, class method attribution)
 *   - In-repo calls (calls to functions exported by other in-repo files)
 *   - Call-ins (inverse: who calls this file's exports)
 *
 * Call graph analysis is NAME-BASED — it matches call sites to exported symbol
 * names without full TypeScript module resolution.
 * TODO: name-based matching can produce false positives if two different in-repo
 * files export a function with the same name. A future version could use tsc's
 * full program + type checker to resolve call targets unambiguously.
 *
 * All structural info (imports with wikilinks, call graph, function signatures)
 * is placed in the BODY of the generated note, not the frontmatter, because
 * ContextGarden strips frontmatter from the embedded text. Agents only see the
 * body during RAG retrieval.
 *
 * Usage:
 *   npx tsx scripts/mirror-codebase.ts [options]
 *
 * Options:
 *   --scan-dir <path>    Root directory to scan (default: cwd)
 *   --mirror-dir <path>  Output directory (default: <cwd>/md_db/code)
 *   --omit <glob,...>    Comma-separated patterns to exclude
 *                        (default: dist,node_modules,_legacy,.context-garden,*.d.ts)
 *   --force              Regenerate all files even if mirror is up-to-date
 */

import * as ts from "typescript";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);

const DEFAULT_OMIT = ["dist", "node_modules", "_legacy", ".context-garden", "*.d.ts"];

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface CliArgs {
  scanDir: string;
  mirrorDir: string;
  omitPatterns: string[];
  force: boolean;
}

interface ImportInfo {
  specifier: string;       // raw specifier, e.g. './config.js' or 'typescript'
  isInternal: boolean;     // true if starts with '.'
  bindings: string[];      // named imports
  defaultBinding?: string;
  namespaceBinding?: string;
}

interface ExportInfo {
  name: string;
  kind: "function" | "class" | "const" | "type" | "interface" | "enum" | "reexport";
  signature?: string;      // full text signature for functions
  body?: string;           // full source text of the symbol declaration, capped at MAX_BODY_CHARS
}

interface FunctionInfo {
  name: string;
  signature: string;       // e.g. "foo(a: string, b: number): void"
  isExported: boolean;
  isAsync: boolean;
  isMethod: boolean;
  className?: string;
  bodyStart: number;       // char offset — used for per-function call attribution
  bodyEnd: number;
}

interface CallSite {
  name: string;
  position: number;        // char offset in file
}

interface InRepoCall {
  calleeName: string;
  sourceFile: string;      // relative path of the file that defines it
  position: number;
}

interface CallIn {
  callerFile: string;
  calledName: string;
  callerName?: string;     // which exported function/class in callerFile made the call
}

interface FileData {
  absolutePath: string;
  relativePath: string;   // relative to scanDir, forward slashes
  mirrorPath: string;     // absolute path for the output .md file
  imports: ImportInfo[];
  exports: ExportInfo[];
  functions: FunctionInfo[];
  callSites: CallSite[];  // all call expression names + positions (pass 1)
  inRepoCalls: InRepoCall[];  // filled in pass 2
  callIns: CallIn[];          // filled in pass 2
  entrypointFingerprints: string[];
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

function parseCliForce(): boolean {
  return process.argv.includes("--force");
}

// ---------------------------------------------------------------------------
// File discovery
// ---------------------------------------------------------------------------

/**
 * Derive the mirror file stem for a source path.
 * For `index.ts` files the stem would collide across directories, so we use
 * `{parentDirName}-index` instead (e.g. `shared/markdown/index.ts` → stem
 * `markdown-index`, mirror file `markdown-index.md`).
 */
function mirrorStem(relPath: string): string {
  const stem = path.posix.basename(relPath).replace(/\.ts$/, "");
  if (stem === "index") {
    const parentDir = path.posix.dirname(relPath);
    const parent = parentDir === "." ? "" : path.posix.basename(parentDir);
    return parent ? `${parent}-index` : "root-index";
  }
  return stem;
}

function shouldOmit(relPath: string, patterns: string[]): boolean {
  const parts = relPath.split("/");
  // Skip dot-prefixed directories (Obsidian ignores them)
  if (parts.some((p) => p.startsWith(".") && p !== ".")) return true;
  for (const pattern of patterns) {
    if (pattern.startsWith("*.")) {
      // Extension pattern — match any file ending with this suffix
      const suffix = pattern.slice(1); // "*.d.ts" → ".d.ts"
      if (relPath.endsWith(suffix)) return true;
    } else {
      // Directory / segment pattern — match any path segment exactly
      if (parts.some((p) => p === pattern)) return true;
    }
  }
  return false;
}

function collectFiles(
  scanDir: string,
  mirrorDir: string,
  omitPatterns: string[]
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
      } else if (entry.isFile() && entry.name.endsWith(".ts") && !entry.name.endsWith(".d.ts")) {
        const mirrorRel = path.posix.join(path.posix.dirname(relPath), mirrorStem(relPath) + ".md");
        const mirrorPath = path.join(mirrorDir, mirrorRel);
        results.push({ absolutePath: absPath, relativePath: relPath, mirrorPath });
      }
    }
  }

  walk(scanDir);
  return results;
}

function detectEntrypointFingerprints(relativePath: string, source: string): string[] {
  const fingerprints = new Set<string>();
  const base = path.posix.basename(relativePath).toLowerCase();
  const dirParts = path.posix.dirname(relativePath).split("/");

  if (["main.ts", "cli.ts", "server.ts", "app.ts", "bootstrap.ts"].includes(base)) {
    fingerprints.add(`entrypoint-like file name: ${base}`);
  }
  if (base === "index.ts" && path.posix.dirname(relativePath) === ".") {
    fingerprints.add("root index.ts file");
  }
  if (dirParts.some((part) => ["bin", "cli", "scripts"].includes(part.toLowerCase()))) {
    fingerprints.add("located under an entrypoint-like directory");
  }
  if (source.startsWith("#!")) {
    fingerprints.add("shebang executable script");
  }
  if (/\b(?:export\s+)?(?:async\s+)?function\s+main\s*\(/.test(source)
    || /\b(?:const|let|var)\s+main\s*=\s*(?:async\s*)?\(?/.test(source)) {
    fingerprints.add("declares a function named main");
  }
  if (/\b(?:export\s+)?(?:async\s+)?function\s+bootstrap\s*\(/.test(source)
    || /\b(?:const|let|var)\s+bootstrap\s*=/.test(source)) {
    fingerprints.add("declares a bootstrap function");
  }
  if (/\bmain\s*\([^)]*\)\s*(?:\.catch\s*\(|;|\n)/.test(source)) {
    fingerprints.add("invokes main() at module scope");
  }
  if (/require\.main\s*===\s*module/.test(source)
    || (/import\.meta\.url/.test(source) && /process\.argv\[[01]\]/.test(source))) {
    fingerprints.add("uses Node entrypoint guard");
  }
  if (/\bprocess\.argv\b/.test(source)) {
    fingerprints.add("reads process.argv");
  }
  if (/\b(?:program|commander|yargs)\.(?:parse|command|option)\b/.test(source)) {
    fingerprints.add("configures a CLI parser");
  }
  if (/\b(?:app|server)\.listen\s*\(/.test(source)
    || /\bcreateServer\s*\(/.test(source)) {
    fingerprints.add("starts a listening server");
  }

  return [...fingerprints];
}

// ---------------------------------------------------------------------------
// AST helpers
// ---------------------------------------------------------------------------

function hasModifier(node: ts.Node, kind: ts.SyntaxKind): boolean {
  return !!((node as ts.HasModifiers).modifiers?.some((m) => m.kind === kind));
}

function getSlice(sf: ts.SourceFile, node: ts.Node): string {
  return sf.text.slice(node.getStart(sf), node.getEnd());
}

const MAX_BODY_CHARS = 10_000;

function capBody(sf: ts.SourceFile, node: ts.Node): string {
  const raw = getSlice(sf, node);
  if (raw.length <= MAX_BODY_CHARS) return raw;
  return raw.slice(0, MAX_BODY_CHARS) + "\n// ... (truncated)";
}

function formatParams(params: ts.NodeArray<ts.ParameterDeclaration>, sf: ts.SourceFile): string {
  return params
    .map((p) => {
      const rest = p.dotDotDotToken ? "..." : "";
      const name = getSlice(sf, p.name);
      const opt = p.questionToken ? "?" : "";
      const type = p.type ? `: ${getSlice(sf, p.type)}` : "";
      return `${rest}${name}${opt}${type}`;
    })
    .join(", ");
}

function formatReturn(node: ts.SignatureDeclaration, sf: ts.SourceFile): string {
  return node.type ? `: ${getSlice(sf, node.type)}` : "";
}

function nodeBodyRange(node: ts.Node): { start: number; end: number } {
  return { start: node.getFullStart(), end: node.getEnd() };
}

// ---------------------------------------------------------------------------
// AST extraction
// ---------------------------------------------------------------------------

function extractImports(sf: ts.SourceFile): ImportInfo[] {
  const imports: ImportInfo[] = [];
  ts.forEachChild(sf, (node) => {
    if (!ts.isImportDeclaration(node)) return;
    const specifier = (node.moduleSpecifier as ts.StringLiteral).text;
    const isInternal = specifier.startsWith(".");
    const bindings: string[] = [];
    let defaultBinding: string | undefined;
    let namespaceBinding: string | undefined;

    if (node.importClause) {
      if (node.importClause.name) defaultBinding = node.importClause.name.text;
      const nb = node.importClause.namedBindings;
      if (nb) {
        if (ts.isNamespaceImport(nb)) {
          namespaceBinding = nb.name.text;
        } else if (ts.isNamedImports(nb)) {
          nb.elements.forEach((el) => bindings.push(el.name.text));
        }
      }
    }
    imports.push({ specifier, isInternal, bindings, defaultBinding, namespaceBinding });
  });
  return imports;
}

function extractExports(sf: ts.SourceFile): ExportInfo[] {
  const exports: ExportInfo[] = [];

  ts.forEachChild(sf, (node) => {
    const isExport = hasModifier(node, ts.SyntaxKind.ExportKeyword);

    if (ts.isFunctionDeclaration(node) && isExport && node.name) {
      const params = formatParams(node.parameters, sf);
      const ret = formatReturn(node, sf);
      exports.push({
        name: node.name.text,
        kind: "function",
        signature: `${node.name.text}(${params})${ret}`,
        body: capBody(sf, node),
      });
    } else if (ts.isClassDeclaration(node) && isExport && node.name) {
      exports.push({ name: node.name.text, kind: "class", body: capBody(sf, node) });
    } else if (ts.isVariableStatement(node) && isExport) {
      const stmtBody = capBody(sf, node);
      node.declarationList.declarations.forEach((decl) => {
        if (ts.isIdentifier(decl.name)) {
          exports.push({ name: decl.name.text, kind: "const", body: stmtBody });
        }
      });
    } else if (ts.isTypeAliasDeclaration(node) && isExport) {
      exports.push({ name: node.name.text, kind: "type", body: capBody(sf, node) });
    } else if (ts.isInterfaceDeclaration(node) && isExport) {
      exports.push({ name: node.name.text, kind: "interface", body: capBody(sf, node) });
    } else if (ts.isEnumDeclaration(node) && isExport) {
      exports.push({ name: node.name.text, kind: "enum", body: capBody(sf, node) });
    } else if (ts.isExportDeclaration(node)) {
      if (node.exportClause && ts.isNamedExports(node.exportClause)) {
        node.exportClause.elements.forEach((el) => {
          exports.push({ name: el.name.text, kind: "reexport" });
        });
      }
    }
  });

  return exports;
}

function extractFunctions(sf: ts.SourceFile): FunctionInfo[] {
  const functions: FunctionInfo[] = [];

  function visit(node: ts.Node, className?: string) {
    // Top-level function declarations
    if (ts.isFunctionDeclaration(node) && node.name) {
      const isExported = hasModifier(node, ts.SyntaxKind.ExportKeyword);
      const isAsync = hasModifier(node, ts.SyntaxKind.AsyncKeyword);
      const params = formatParams(node.parameters, sf);
      const ret = formatReturn(node, sf);
      const range = nodeBodyRange(node);
      functions.push({
        name: node.name.text,
        signature: `${node.name.text}(${params})${ret}`,
        isExported,
        isAsync,
        isMethod: false,
        bodyStart: range.start,
        bodyEnd: range.end,
      });
      // Don't recurse into function body — we only want top-level + class methods
      return;
    }

    // const foo = (...) => ...  or  const foo = function(...) {...}
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      const init = node.initializer;
      if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) {
        const varStmt = node.parent?.parent;
        const isExported =
          ts.isVariableStatement(varStmt) && hasModifier(varStmt, ts.SyntaxKind.ExportKeyword);
        const isAsync = hasModifier(init, ts.SyntaxKind.AsyncKeyword);
        const params = formatParams(init.parameters, sf);
        const ret = formatReturn(init, sf);
        const range = nodeBodyRange(node);
        functions.push({
          name: node.name.text,
          signature: `${node.name.text}(${params})${ret}`,
          isExported,
          isAsync,
          isMethod: false,
          bodyStart: range.start,
          bodyEnd: range.end,
        });
        return;
      }
    }

    // Class methods
    if (ts.isMethodDeclaration(node) && ts.isIdentifier(node.name)) {
      const isAsync = hasModifier(node, ts.SyntaxKind.AsyncKeyword);
      const params = formatParams(node.parameters, sf);
      const ret = formatReturn(node, sf);
      const displayName = className ? `${className}.${node.name.text}` : node.name.text;
      const range = nodeBodyRange(node);
      functions.push({
        name: node.name.text,
        signature: `${displayName}(${params})${ret}`,
        isExported: false,
        isAsync,
        isMethod: true,
        className,
        bodyStart: range.start,
        bodyEnd: range.end,
      });
    }

    // Recurse — track className when entering class body
    if (ts.isClassDeclaration(node)) {
      const name = node.name?.text;
      ts.forEachChild(node, (child) => visit(child, name));
      return;
    }

    ts.forEachChild(node, (child) => visit(child, className));
  }

  ts.forEachChild(sf, (node) => visit(node));
  return functions;
}

function extractCallSites(sf: ts.SourceFile): CallSite[] {
  const seen = new Set<string>();
  const sites: CallSite[] = [];

  function visit(node: ts.Node) {
    if (ts.isCallExpression(node)) {
      const expr = node.expression;
      let name: string | undefined;
      if (ts.isIdentifier(expr)) {
        name = expr.text;
      } else if (ts.isPropertyAccessExpression(expr) && ts.isIdentifier(expr.name)) {
        name = expr.name.text;
      }
      if (name) {
        // Record every occurrence (position matters for per-function attribution)
        sites.push({ name, position: node.getStart(sf) });
      }
    }
    ts.forEachChild(node, visit);
  }

  ts.forEachChild(sf, visit);
  return sites;
}

// ---------------------------------------------------------------------------
// Pass 1: per-file extraction
// ---------------------------------------------------------------------------

function extractFileData(
  absolutePath: string,
  relativePath: string,
  mirrorPath: string
): FileData {
  const source = fs.readFileSync(absolutePath, "utf8");
  const sf = ts.createSourceFile(absolutePath, source, ts.ScriptTarget.Latest, /* setParentNodes */ true);

  return {
    absolutePath,
    relativePath,
    mirrorPath,
    imports: extractImports(sf),
    exports: extractExports(sf),
    functions: extractFunctions(sf),
    callSites: extractCallSites(sf),
    inRepoCalls: [],
    callIns: [],
    entrypointFingerprints: detectEntrypointFingerprints(relativePath, source),
  };
}

// ---------------------------------------------------------------------------
// Pass 2: cross-file call graph
// ---------------------------------------------------------------------------

function buildCallGraph(files: FileData[]): void {
  // Build symbol map: exported name → files that export it
  // TODO: name-based matching can produce false positives if two different
  // in-repo files export a function with the same name. A future version could
  // use tsc's full program + type checker for unambiguous resolution.
  const symbolMap = new Map<string, FileData[]>();
  for (const file of files) {
    for (const exp of file.exports) {
      if (exp.kind === "reexport") continue; // skip reexports — symbol notes only exist for defining files
      if (!symbolMap.has(exp.name)) symbolMap.set(exp.name, []);
      symbolMap.get(exp.name)!.push(file);
    }
  }

  for (const file of files) {
    for (const site of file.callSites) {
      const sources = symbolMap.get(site.name);
      if (!sources) continue;
      for (const src of sources) {
        if (src.relativePath === file.relativePath) continue; // skip self-calls

        // In-repo call on the caller side (deduplicate by name+source)
        const alreadyRecorded = file.inRepoCalls.some(
          (c) => c.calleeName === site.name && c.sourceFile === src.relativePath
        );
        if (!alreadyRecorded) {
          file.inRepoCalls.push({
            calleeName: site.name,
            sourceFile: src.relativePath,
            position: site.position,
          });
        }

        // Call-in on the callee side (deduplicate by caller+name+callerName)
        // Attribute call to the specific exported function in the caller file
        const callerFn = file.functions.find(
          (f) => f.isExported && site.position >= f.bodyStart && site.position <= f.bodyEnd
        );
        const callerName = callerFn?.name;
        const alreadyIn = src.callIns.some(
          (c) => c.callerFile === file.relativePath && c.calledName === site.name && c.callerName === callerName
        );
        if (!alreadyIn) {
          src.callIns.push({ callerFile: file.relativePath, calledName: site.name, callerName });
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Markdown generation
// ---------------------------------------------------------------------------

const MIRROR_PREFIX = "code";

/**
 * Convert a .ts relative path to a wikilink target.
 * Uses mirrorStem() so index files map to their renamed stems:
 *   "shared/stack.ts"          → "code/shared/stack"
 *   "shared/markdown/index.ts" → "code/shared/markdown/markdown-index"
 */
function toWikiLink(relPath: string, prefix = MIRROR_PREFIX): string {
  const dir = path.posix.dirname(relPath);
  const stem = mirrorStem(relPath);
  return dir === "." ? `${prefix}/${stem}` : `${prefix}/${dir}/${stem}`;
}

/**
 * Convert a .ts relative path + symbol name to a tier-1 symbol wikilink target.
 *   ("core/os/fs.ts", "fileExists") → "code/core/os/fs/fileExists"
 */
function toSymbolWikiLink(relPath: string, symbolName: string, prefix = MIRROR_PREFIX): string {
  const fileLink = toWikiLink(relPath, prefix);
  return `${fileLink}/${sanitizeSymbolName(symbolName)}`;
}

/**
 * Try to resolve an internal import specifier to a FileData entry.
 * e.g. from "context_engine/context-engine.ts", specifier "../shared/config.js"
 * → looks for "shared/config.ts" in files list.
 */
function resolveInternal(fromRelPath: string, specifier: string, files: FileData[]): FileData | null {
  if (!specifier.startsWith(".")) return null;
  const fromDir = path.posix.dirname(fromRelPath);
  // Strip .js suffix (ESM compat), then normalize
  const stripped = specifier.replace(/\.js$/, "").replace(/\.ts$/, "");
  let resolved = path.posix.normalize(path.posix.join(fromDir, stripped));
  // Try as-is with .ts
  const candidate = resolved + ".ts";
  const match = files.find((f) => f.relativePath === candidate);
  if (match) return match;
  // Also try index.ts
  const indexMatch = files.find((f) => f.relativePath === path.posix.join(resolved, "index.ts"));
  return indexMatch ?? null;
}

function generateMarkdown(
  file: FileData,
  allFiles: FileData[],
  today: string,
  prefix = MIRROR_PREFIX,
  workspace?: string,
  moduleLinkByDir?: Map<string, string>,
): string {
  const lines: string[] = [];
  const filename = path.basename(file.relativePath);

  // ── Frontmatter (minimal — body carries everything for RAG embedding) ──────
  // Only fields the context engine actually reads:
  //   type       → infer_note_type() → graph label
  //   tags       → extract_tags() → tag boosting in retrieval
  //   path       → summarizer source lookup
  //   language   → informational; not used for cleanup
  const dirRel = path.posix.dirname(file.relativePath);
  const parentModuleLink = moduleLinkByDir?.get(dirRel)
    ?? (dirRel === "." ? `${prefix}/root_module` : `${prefix}/${dirRel}_module`);

  lines.push(
    "---",
    "type: codeUnit",
    "tier: 2",
    `path: ${file.relativePath}`,
    `parentModule: ${parentModuleLink}`,
    "language: ts",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    ...(file.entrypointFingerprints.length ? ["  - entrypoint"] : []),
    "---",
    ""
  );

  // ── Title ────────────────────────────────────────────────────────────────
  // For index files the filename alone is meaningless — use the full path as title
  const title = filename === "index.ts" ? file.relativePath : filename;
  lines.push(`# ${title}`, "");
  lines.push(`> \`${file.relativePath}\``, "");

  // ── Exports ──────────────────────────────────────────────────────────────
  if (file.exports.length) {
    lines.push("## Exports", "");
    for (const exp of file.exports) {
      const display = exp.signature ? `\`${exp.signature}\`` : `\`${exp.name}\``;
      lines.push(`- ${display} *(${exp.kind})*`);
    }
    lines.push("");
  }

  // ── Imports ──────────────────────────────────────────────────────────────
  const internalImports = file.imports.filter((i) => i.isInternal);
  const externalImports = file.imports.filter((i) => !i.isInternal);

  if (internalImports.length || externalImports.length) {
    lines.push("## Imports", "");

    if (internalImports.length) {
      lines.push("### Internal", "");
      for (const imp of internalImports) {
        const resolved = resolveInternal(file.relativePath, imp.specifier, allFiles);
        const linkTarget = resolved
          ? `[[${toWikiLink(resolved.relativePath, prefix)}]]`
          : `\`${imp.specifier}\``;

        const allBindings = [
          ...(imp.defaultBinding ? [imp.defaultBinding] : []),
          ...(imp.namespaceBinding ? [`* as ${imp.namespaceBinding}`] : []),
          ...imp.bindings,
        ];
        const bindingStr = allBindings.length ? ` — \`${allBindings.join("`, `")}\`` : "";
        lines.push(`- ${linkTarget}${bindingStr}`);
      }
      lines.push("");
    }

    if (externalImports.length) {
      lines.push("### External", "");
      for (const imp of externalImports) {
        const allBindings = [
          ...(imp.defaultBinding ? [imp.defaultBinding] : []),
          ...(imp.namespaceBinding ? [`* as ${imp.namespaceBinding}`] : []),
          ...imp.bindings,
        ];
        const bindingStr = allBindings.length ? ` — \`${allBindings.join("`, `")}\`` : "";
        lines.push(`- \`${imp.specifier}\`${bindingStr}`);
      }
      lines.push("");
    }
  }

  // ── Functions ─────────────────────────────────────────────────────────────
  if (file.functions.length) {
    lines.push("## Functions", "");
    for (const fn of file.functions) {
      const flags: string[] = [];
      if (fn.isAsync) flags.push("async");
      if (fn.isExported) flags.push("exported");
      if (fn.isMethod && fn.className) flags.push(`method of \`${fn.className}\``);
      const flagStr = flags.length ? ` *(${flags.join(", ")})*` : "";

      // Per-function in-repo calls: find call sites that fall within this function's body range
      const ownCalls = file.inRepoCalls.filter(
        (c) => c.position >= fn.bodyStart && c.position <= fn.bodyEnd
      );

      lines.push(`### \`${fn.signature}\`${flagStr}`);
      if (ownCalls.length) {
        // Group by source file
        const bySource = new Map<string, string[]>();
        for (const c of ownCalls) {
          if (!bySource.has(c.sourceFile)) bySource.set(c.sourceFile, []);
          bySource.get(c.sourceFile)!.push(c.calleeName);
        }
        const callParts: string[] = [];
        for (const [srcFile, names] of bySource) {
          callParts.push(`[[${toWikiLink(srcFile, prefix)}]] (\`${names.join("`, `")}\`)`);
        }
        lines.push(`*Calls into: ${callParts.join(", ")}*`);
      }
      lines.push("");
    }
  }

  // ── In-Repo Calls (file-level summary) ───────────────────────────────────
  if (file.inRepoCalls.length) {
    lines.push("## In-Repo Calls", "");
    lines.push("Functions this file calls that are defined in other in-repo files:", "");

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

  // ── Call-Ins ──────────────────────────────────────────────────────────────
  if (file.callIns.length) {
    lines.push("## Call-Ins", "");
    lines.push("Other in-repo files that call exports from this file:", "");

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

// ---------------------------------------------------------------------------
// Tier-1: Symbol note generation
// ---------------------------------------------------------------------------

/** Safe filename for a symbol name (strips generics, spaces, etc.) */
function sanitizeSymbolName(name: string): string {
  return name.replace(/[<>,:.\s[\]]/g, "_").replace(/__+/g, "_").replace(/^_|_$/g, "");
}

/** Absolute path for a tier-1 symbol note. */
function symbolNotePath(mirrorDir: string, relPath: string, symbolName: string): string {
  const dirRel = path.posix.dirname(relPath);
  const stem = mirrorStem(relPath);
  const subdir = dirRel === "." ? stem : `${dirRel}/${stem}`;
  return path.join(mirrorDir, subdir, sanitizeSymbolName(symbolName) + ".md");
}

function generateSymbolNote(
  file: FileData,
  exp: ExportInfo,
  today: string,
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
    `symbolKind: ${exp.kind}`,
    "language: ts",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    "---",
    ""
  );

  lines.push(`# ${exp.name}`, "");
  lines.push(`**Kind:** \`${exp.kind}\`  `);
  lines.push(`**File:** [[${parentFileLink}]]`, "");

  if (exp.body) {
    lines.push("## Implementation", "");
    lines.push("```ts");
    lines.push(exp.body);
    lines.push("```", "");
  } else if (exp.kind === "reexport" && exp.signature) {
    lines.push("## Signature", "");
    lines.push("```ts");
    lines.push(exp.signature);
    lines.push("```", "");
  }

  // Per-function calls (only for function/class) — emit symbol-level wikilinks
  // so the graph indexer creates proper tier-1→tier-1 CALLS edges.
  if (exp.kind === "function" || exp.kind === "class") {
    const fn = file.functions.find((f) => f.name === exp.name);
    if (fn) {
      const ownCalls = file.inRepoCalls.filter(
        (c) => c.position >= fn.bodyStart && c.position <= fn.bodyEnd
      );
      if (ownCalls.length) {
        lines.push("## Calls Into", "");
        // Deduplicate by callee symbol name (same symbol may be matched from multiple source files)
        const seen = new Set<string>();
        for (const c of ownCalls) {
          const symLink = toSymbolWikiLink(c.sourceFile, c.calleeName, prefix);
          if (seen.has(symLink)) continue;
          seen.add(symLink);
          lines.push(`- [[${symLink}|${c.calleeName}]]`);
        }
        lines.push("");
      }
    }
  }

  // Call-ins for this specific symbol — emit symbol-level wikilinks
  // so the graph indexer creates proper tier-1→tier-1 CALLS edges (reverse direction).
  const callers = file.callIns.filter((c) => c.calledName === exp.name);
  if (callers.length) {
    lines.push("## Called By", "");
    const seen = new Set<string>();
    for (const c of callers) {
      if (c.callerName) {
        // Link to the specific caller symbol (tier-1 → tier-1)
        const symLink = toSymbolWikiLink(c.callerFile, c.callerName, prefix);
        if (seen.has(symLink)) continue;
        seen.add(symLink);
        lines.push(`- [[${symLink}|${c.callerName}]]`);
      } else {
        // Fallback: link to the caller file (tier-2) if we can't attribute to a function
        const fileLink = toWikiLink(c.callerFile, prefix);
        if (seen.has(fileLink)) continue;
        seen.add(fileLink);
        lines.push(`- [[${fileLink}]]`);
      }
    }
    lines.push("");
  }

  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Tier-3: Module note generation
// ---------------------------------------------------------------------------

/**
 * Absolute path for a tier-3 module note.
 * Tier-3 notes live ALONGSIDE their directory (at the parent level) to avoid
 * collisions with same-named tier-2 notes (e.g. orchestrator.ts inside orchestrator/).
 *   root dir  → {mirrorDir}/root_module.md
 *   agents/orchestrator/ → {mirrorDir}/agents/orchestrator_module.md
 */
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

function moduleNotePath(mirrorDir: string, dirRel: string, language = "ts"): string {
  const basePath = baseModuleNotePath(mirrorDir, dirRel);
  const existingLanguage = readFrontmatterLanguage(basePath);
  if (!fs.existsSync(basePath) || existingLanguage === language || existingLanguage === null) {
    return basePath;
  }
  return basePath.replace(/\.md$/, `_${language}.md`);
}

function moduleWikiLink(mirrorDir: string, dirRel: string, prefix: string, language = "ts"): string {
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

function generateModuleNote(
  dirRel: string,
  fileStemsAndPaths: { relPath: string; stem: string }[],
  today: string,
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
    "language: ts",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    "---",
    ""
  );

  lines.push(`# ${dirName}`, "");

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
  for (const { relPath, stem } of fileStemsAndPaths) {
    const d = path.posix.dirname(relPath);
    const link = d === "." ? `${prefix}/${stem}` : `${prefix}/${d}/${stem}`;
    lines.push(`- [[${link}]]`);
  }
  lines.push("");

  return lines.join("\n");
}

/**
 * Extract the file wikilinks listed in an existing _module.md's ## Files section.
 * Returns null if the file doesn't exist.
 */
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

/**
 * Extract the submodule wikilinks listed in an existing _module.md's ## Submodules section.
 * Returns [] if the section is absent (not an error — root modules have no submodules).
 */
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
// Summary preservation — survive mirror regeneration
// ---------------------------------------------------------------------------

/**
 * Extract the "## Summary" section (and everything after it) from an existing
 * mirror file. Returns the section text including the header, or null if the
 * file doesn't exist or has no summary.
 */
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

// ---------------------------------------------------------------------------
// Public API (used by mirror-watcher and CLI)
// ---------------------------------------------------------------------------

export interface MirrorTsOptions {
  scanDir: string;
  mirrorDir: string;
  omitPatterns: string[];
  force: boolean;
  /** Workspace name — written into frontmatter as `workspace: {name}`. */
  workspace?: string;
  /** Wikilink prefix for cross-note references. Default: "code". */
  wikilinkPrefix?: string;
}

export async function runMirrorTs(
  opts: MirrorTsOptions,
): Promise<{ written: number; skipped: number; validPaths: Set<string> }> {
  const today = new Date().toISOString().slice(0, 10);
  const discovered = collectFiles(opts.scanDir, opts.mirrorDir, opts.omitPatterns);

  const files: FileData[] = [];
  let parseErrors = 0;
  for (const { absolutePath, relativePath, mirrorPath } of discovered) {
    try {
      files.push(extractFileData(absolutePath, relativePath, mirrorPath));
    } catch (err) {
      console.warn(`[mirror-ts] WARN: failed to parse ${relativePath}: ${err}`);
      parseErrors++;
    }
  }

  buildCallGraph(files);

  let written = 0;
  let skipped = 0;
  const prefix = opts.wikilinkPrefix ?? MIRROR_PREFIX;
  const byDir = buildDirectoryEntries(files);
  const moduleLinkByDir = new Map(
    [...byDir.keys()].map((dirRel) => [dirRel, moduleWikiLink(opts.mirrorDir, dirRel, prefix, "ts")]),
  );

  // ── Tier-2 file notes + Tier-1 symbol notes ──────────────────────────
  const validMirrorPaths = new Set<string>();

  for (const file of files) {
    let isStale = true;
    if (!opts.force) {
      try {
        const srcStat = fs.statSync(file.absolutePath);
        const mirrorStat = fs.statSync(file.mirrorPath);
        if (mirrorStat.mtimeMs >= srcStat.mtimeMs) {
          isStale = false;
        }
      } catch { /* mirror doesn't exist yet — proceed */ }
    }

    // Track valid tier-2 path
    validMirrorPaths.add(path.resolve(file.mirrorPath));

    // Track valid tier-1 symbol paths (even when skipping, so cleanup keeps them)
    const definedSymbols = file.exports.filter((e) => e.kind !== "reexport");
    for (const exp of definedSymbols) {
      validMirrorPaths.add(path.resolve(symbolNotePath(opts.mirrorDir, file.relativePath, exp.name)));
    }

    if (!isStale) { skipped++; continue; }

    // Write tier-2 note
    const markdown = generateMarkdown(file, files, today, prefix, opts.workspace, moduleLinkByDir);
    const preserved = extractSummarySection(file.mirrorPath);
    const final = preserved ? markdown.trimEnd() + "\n\n" + preserved + "\n" : markdown;
    fs.mkdirSync(path.dirname(file.mirrorPath), { recursive: true });
    fs.writeFileSync(file.mirrorPath, final, "utf8");
    written++;

    // Write tier-1 symbol notes (one per non-reexport export)
    for (const exp of definedSymbols) {
      const symPath = symbolNotePath(opts.mirrorDir, file.relativePath, exp.name);
      const symMarkdown = generateSymbolNote(file, exp, today, prefix, opts.workspace, moduleLinkByDir);
      const symPreserved = extractSummarySection(symPath);
      const symFinal = symPreserved ? symMarkdown.trimEnd() + "\n\n" + symPreserved + "\n" : symMarkdown;
      fs.mkdirSync(path.dirname(symPath), { recursive: true });
      fs.writeFileSync(symPath, symFinal, "utf8");
    }
  }

  for (const [dirRel, fileEntries] of byDir) {
    const modPath = moduleNotePath(opts.mirrorDir, dirRel, "ts");
    validMirrorPaths.add(path.resolve(modPath));

    // Child submodule links: union of this language's byDir children + *_module.md files
    // already on disk from the other-language mirror pass.
    const childModuleLinksSet = new Set<string>();
    for (const k of byDir.keys()) {
      if (path.posix.dirname(k) === dirRel && k !== dirRel) {
        childModuleLinksSet.add(`[[${moduleLinkByDir.get(k)}]]`);
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
    } catch { /* mirror subdir may not exist yet on first run */ }
    const childModuleLinks = [...childModuleLinksSet].sort();
    const parentModuleLink = dirRel === "." ? undefined : moduleLinkByDir.get(path.posix.dirname(dirRel));

    // Stale if: force, doesn't exist, files list changed, or submodules list changed
    const existingFileLinks = extractModuleFileLinks(modPath);
    const existingSubmoduleLinks = extractModuleSubmoduleLinks(modPath);
    const currentFileLinks = fileEntries.map(({ relPath, stem }) => {
      const d = path.posix.dirname(relPath);
      return d === "." ? `[[${prefix}/${stem}]]` : `[[${prefix}/${d}/${stem}]]`;
    }).sort();
    const needsWrite = opts.force || existingFileLinks === null
      || JSON.stringify(existingFileLinks.sort()) !== JSON.stringify(currentFileLinks)
      || JSON.stringify(existingSubmoduleLinks.sort()) !== JSON.stringify(childModuleLinks);

    if (needsWrite) {
      const modMarkdown = generateModuleNote(dirRel, fileEntries, today, prefix, opts.workspace, childModuleLinks, parentModuleLink);
      const modPreserved = extractSummarySection(modPath);
      const modFinal = modPreserved ? modMarkdown.trimEnd() + "\n\n" + modPreserved + "\n" : modMarkdown;
      fs.mkdirSync(path.dirname(modPath), { recursive: true });
      fs.writeFileSync(modPath, modFinal, "utf8");
    }
  }

  if (parseErrors > 0) console.warn(`[mirror-ts] ${parseErrors} files failed to parse`);
  return { written, skipped, validPaths: validMirrorPaths };
}

// ---------------------------------------------------------------------------
// CLI entry point (only when executed directly via tsx/node)
// ---------------------------------------------------------------------------

async function main() {
  const force = parseCliForce();
  const cwd = process.cwd();
  const registryPath = path.join(cwd, ".context-garden", "workspaces.json");
  const mdDbPath = path.join(cwd, "md_db");

  let workspaces: Array<{ name: string; sourceDir: string; languages: string[]; active: boolean }> = [];
  try {
    const raw = fs.readFileSync(registryPath, "utf8");
    workspaces = JSON.parse(raw).filter((w: { active: boolean }) => w.active);
  } catch {
    // Fall back to running against cwd with no workspace prefix (legacy behaviour).
    // Note: no cleanup here — use mirror-workspaces.ts for the full pipeline.
    console.log("[mirror-ts] no workspaces.json found, running against cwd");
    const { written, skipped } = await runMirrorTs({ scanDir: cwd, mirrorDir: path.join(mdDbPath, "code"), omitPatterns: DEFAULT_OMIT, force });
    console.log(`  written=${written} skipped=${skipped}`);
    return;
  }

  for (const ws of workspaces) {
    if (!ws.languages.includes("ts")) continue;
    const mirrorDir = path.join(mdDbPath, "code", ws.name);
    console.log(`[mirror-ts] ${ws.name} (${ws.sourceDir})`);
    const { written, skipped } = await runMirrorTs({
      scanDir: ws.sourceDir,
      mirrorDir,
      omitPatterns: DEFAULT_OMIT,
      force,
      workspace: ws.name,
      wikilinkPrefix: `code/${ws.name}`,
    });
    // Cleanup is intentionally omitted here — this CLI only mirrors TS files.
    // Running cleanMirrorDir with TS-only validPaths would incorrectly prune
    // Python notes in the same mirrorDir. Use mirror-workspaces.ts instead.
    console.log(`  written=${written} skipped=${skipped}`);
  }
}

// Only run main() when this file is the entrypoint (e.g. npx tsx automation/scripts/mirror-codebase.ts)
if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
