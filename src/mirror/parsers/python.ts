#!/usr/bin/env npx tsx
/**
 * mirror-codebase-py.ts
 *
 * Python variant of mirror-codebase.ts — generates a "code mirror" markdown
 * note for every Python (.py) file found under the scan directory.
 *
 * Extracts from each .py file (line-based scanner, no external dependency):
 *   - Imports: `import X` and `from X import Y, Z`
 *   - Classes: plain and @dataclass-decorated
 *   - Functions/methods: def / async def with full signatures
 *     - @staticmethod: self/cls not stripped
 *     - @classmethod / regular methods: cls/self stripped from params
 *     - Multiline signatures supported
 *   - In-repo calls: name-based matching against exported symbols
 *   - Call-ins: inverse of above
 *
 * Call graph analysis is NAME-BASED.
 * TODO: name-based matching can produce false positives if two different in-repo
 * files export a function with the same name. A future version could use
 * Python's ast module (via subprocess) to resolve call targets unambiguously.
 *
 * All structural info is placed in the NOTE BODY, not frontmatter, because
 * reader strips frontmatter from embedded text — agents only see body
 * during RAG retrieval. Wikilinks in the body become graph edges.
 * 
 * Usage:
 *   npx tsx scripts/mirror-codebase-py.ts [options]
 *
 * Options:
 *   --scan-dir <path>    Root directory to scan (default: cwd)
 *   --mirror-dir <path>  Output directory (default: <cwd>/md_db/code)
 *   --omit <glob,...>    Comma-separated patterns to exclude
 *                        (default: __pycache__,*.pyi,.venv,env,venv,dist)
 *   --force              Regenerate all files even if mirror is up-to-date
 */

import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);

const DEFAULT_OMIT = ["__pycache__", "*.pyi", ".venv", "env", "venv", "dist", "node_modules"];

// ---------------------------------------------------------------------------
// Types (same shape as mirror-codebase.ts for consistency)
// ---------------------------------------------------------------------------

interface CliArgs {
  scanDir: string;
  mirrorDir: string;
  omitPatterns: string[];
  force: boolean;
}

interface ImportInfo {
  specifier: string;       // e.g. ".models" or "llama_index.core"
  isInternal: boolean;     // true if starts with '.'
  bindings: string[];      // named imports
}

interface ExportInfo {
  name: string;
  kind: "function" | "class" | "const" | "type" | "interface" | "enum" | "reexport" | "route" | "fixture";
  signature?: string;
  decorators?: string[];   // bare decorator names, e.g. ["router.get", "abstractmethod"]
  routeMethod?: string;    // HTTP verb for routes: "GET", "POST", etc.
  routePath?: string;      // URL path for routes: "/api/users/{id}"
  baseClasses?: string[];  // raw base class names from class(...) declaration
  isAbstract?: boolean;    // has @abstractmethod or inherits from ABC/Protocol
  isDataclass?: boolean;   // decorated with @dataclass
}

interface FunctionInfo {
  name: string;
  signature: string;       // e.g. "ClassName.method(param: T) -> R"
  isExported: boolean;     // true for top-level non-underscore defs
  isAsync: boolean;
  isMethod: boolean;
  className?: string;
  bodyStart: number;       // char offset — used for per-function call attribution
  bodyEnd: number;
  decorators?: string[];
}

interface ParsedFunction extends FunctionInfo {
  indent: number;          // indentation level of the def line (for bodyEnd refinement)
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
  imports: ImportInfo[];
  exports: ExportInfo[];
  functions: FunctionInfo[];
  callSites: CallSite[];
  inRepoCalls: InRepoCall[];
  callIns: CallIn[];
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
 * For `__init__.py` files the stem would collide across directories, so we use
 * `{parentDirName}-init` instead (e.g. `knowledge_graph/__init__.py` → stem
 * `knowledge_graph-init`). Same treatment for `index.py` → `{parent}-index`.
 */
function mirrorStem(relPath: string): string {
  const stem = path.posix.basename(relPath).replace(/\.py$/, "");
  if (stem === "__init__" || stem === "index") {
    const parentDir = path.posix.dirname(relPath);
    const parent = parentDir === "." ? "" : path.posix.basename(parentDir);
    const suffix = stem === "__init__" ? "init" : "index";
    return parent ? `${parent}-${suffix}` : `root-${suffix}`;
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
      } else if (entry.isFile() && entry.name.endsWith(".py")) {
        const mirrorRel = path.posix.join(path.posix.dirname(relPath), mirrorStem(relPath) + ".md");
        const mirrorPath = path.join(mirrorDir, mirrorRel);
        results.push({ absolutePath: absPath, relativePath: relPath, mirrorPath });
      }
    }
  }

  walk(scanDir);
  return results;
}

function detectEntrypointFingerprints(
  relativePath: string,
  source: string,
  functions: FunctionInfo[],
): string[] {
  const fingerprints = new Set<string>();
  const base = path.posix.basename(relativePath).toLowerCase();
  const dirParts = path.posix.dirname(relativePath).split("/");

  if (["__main__.py", "main.py", "cli.py", "server.py", "app.py", "manage.py", "wsgi.py", "asgi.py"].includes(base)) {
    fingerprints.add(`entrypoint-like file name: ${base}`);
  }
  if (dirParts.some((part) => ["bin", "cli", "scripts"].includes(part.toLowerCase()))) {
    fingerprints.add("located under an entrypoint-like directory");
  }
  if (source.startsWith("#!")) {
    fingerprints.add("shebang executable script");
  }
  if (/\bif\s+__name__\s*==\s*["']__main__["']\s*:/.test(source)) {
    fingerprints.add("uses Python __main__ guard");
  }
  if (functions.some((fn) => !fn.isMethod && fn.name === "main")) {
    fingerprints.add("declares a function named main");
  }
  if (/\bmain\s*\([^)]*\)/.test(source) && /\b__main__\b/.test(source)) {
    fingerprints.add("invokes main() from the __main__ guard");
  }
  if (/\bargparse\b|\bArgumentParser\s*\(/.test(source)) {
    fingerprints.add("configures argparse CLI handling");
  }
  if (/\b(click|typer)\b/.test(source) || /@\w+\.command\b|@\w+\.group\b/.test(source)) {
    fingerprints.add("uses click or typer CLI structure");
  }
  if (/\buvicorn\.run\s*\(|\bapp\.run\s*\(/.test(source)) {
    fingerprints.add("starts an application server");
  }
  if (/\bexecute_from_command_line\s*\(/.test(source)) {
    fingerprints.add("Django management command entrypoint");
  }

  return [...fingerprints];
}

// ---------------------------------------------------------------------------
// Python keywords + builtins — excluded from call-site extraction
// ---------------------------------------------------------------------------

const PY_KEYWORDS = new Set([
  // Language keywords
  "False", "None", "True", "and", "as", "assert", "async", "await",
  "break", "class", "continue", "def", "del", "elif", "else", "except",
  "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
  "while", "with", "yield",
  // Common builtins
  "print", "len", "range", "list", "dict", "set", "tuple", "str", "int",
  "float", "bool", "bytes", "type", "isinstance", "issubclass", "hasattr",
  "getattr", "setattr", "delattr", "callable", "iter", "next", "enumerate",
  "zip", "map", "filter", "sorted", "reversed", "sum", "min", "max", "abs",
  "round", "open", "input", "repr", "hash", "id", "dir", "vars", "locals",
  "globals", "super", "object", "property", "staticmethod", "classmethod",
  "dataclass", "field", "asdict", "Optional", "Any", "List", "Dict", "Set",
  "Tuple", "Union", "Literal", "Callable", "Type", "ClassVar",
]);

// ---------------------------------------------------------------------------
// Import extraction
// ---------------------------------------------------------------------------

function extractImports(source: string): ImportInfo[] {
  const imports: ImportInfo[] = [];

  for (const line of source.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    // import X  or  import X as Y  or  import X.Y
    const importMatch = trimmed.match(/^import\s+(\S+?)(?:\s+as\s+\w+)?\s*(?:#.*)?$/);
    if (importMatch) {
      imports.push({ specifier: importMatch[1], isInternal: false, bindings: [] });
      continue;
    }

    // from X import A, B  or  from X import (A, B)  or  from X import *
    const fromMatch = trimmed.match(/^from\s+(\S+)\s+import\s+(.+)/);
    if (fromMatch) {
      const specifier = fromMatch[1];
      const isInternal = specifier.startsWith(".");
      const raw = fromMatch[2].replace(/[()\\]/g, "").replace(/#.*$/, "").trim();
      const bindings =
        raw === "*"
          ? ["*"]
          : raw
              .split(",")
              .map((b) => {
                // "A as alias" → use alias
                const parts = b.trim().split(/\s+as\s+/);
                return parts[parts.length - 1].trim();
              })
              .filter(Boolean);
      imports.push({ specifier, isInternal, bindings });
    }
  }

  return imports;
}

// ---------------------------------------------------------------------------
// Class / function / method extraction
// ---------------------------------------------------------------------------

function extractDefinitions(source: string): {
  exports: ExportInfo[];
  functions: FunctionInfo[];
} {
  const lines = source.split("\n");
  const functions: ParsedFunction[] = [];
  const exports: ExportInfo[] = [];

  // Explicit __all__ restricts which names are considered exported
  const allMatch = source.match(/__all__\s*=\s*\[([^\]]*)\]/s);
  const explicitExports: Set<string> | null = allMatch
    ? new Set(
        allMatch[1]
          .split(",")
          .map((s) => s.trim().replace(/['"]/g, ""))
          .filter(Boolean)
      )
    : null;

  // Pending decorators — bare name AND full line (for route path extraction)
  let pendingDecorators: string[] = [];
  let pendingDecoratorLines: string[] = [];

  // Current class context — single-level tracking
  let currentClass: { name: string; indent: number } | null = null;

  let charOffset = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const lineLen = line.length;
    const trimmed = line.trimStart();
    const indent = lineLen - trimmed.length;

    if (!trimmed || trimmed.startsWith("#")) {
      charOffset += lineLen + 1;
      continue;
    }

    // Clear class context when we encounter a statement at the same or lower
    // indent, unless it's a decorator, def, class, or comment
    if (
      currentClass !== null &&
      indent <= currentClass.indent &&
      !trimmed.startsWith("@") &&
      !trimmed.startsWith("def ") &&
      !trimmed.startsWith("async def ") &&
      !trimmed.startsWith("class ")
    ) {
      currentClass = null;
    }

    // Decorator line — capture bare name and full line
    if (trimmed.startsWith("@")) {
      const decoratorName = trimmed.slice(1).split("(")[0].trim();
      pendingDecorators.push(decoratorName);
      pendingDecoratorLines.push(trimmed);
      charOffset += lineLen + 1;
      continue;
    }

    // Class definition
    const classMatch = trimmed.match(/^class\s+(\w+)(?:\s*\(([^)]*)\))?\s*:/);
    if (classMatch) {
      const name = classMatch[1];
      const baseList = classMatch[2]?.trim();
      const classSignature = baseList ? `class ${name}(${baseList})` : `class ${name}`;
      const baseClasses = baseList
        ? baseList.split(",").map((b) => b.trim()).filter(Boolean)
        : [];
      const isDataclass = pendingDecorators.some((d) =>
        d === "dataclass" || d.endsWith(".dataclass")
      );
      const isAbstract = baseClasses.some((b) =>
        b === "ABC" || b === "abc.ABC" || b === "Protocol" || b === "typing.Protocol"
      );
      if (indent === 0) {
        currentClass = { name, indent: 0 };
        if (!explicitExports || explicitExports.has(name)) {
          exports.push({
            name,
            kind: "class",
            signature: classSignature,
            decorators: pendingDecorators.length ? [...pendingDecorators] : undefined,
            baseClasses: baseClasses.length ? baseClasses : undefined,
            isAbstract: isAbstract || undefined,
            isDataclass: isDataclass || undefined,
          });
        }
      }
      pendingDecorators = [];
      pendingDecoratorLines = [];
      charOffset += lineLen + 1;
      continue;
    }

    // Function / method definition
    const defMatch = trimmed.match(/^(async\s+)?def\s+(\w+)\s*\(/);
    if (defMatch) {
      const isAsync = !!(defMatch[1]?.trim());
      const name = defMatch[2];
      const isStatic = pendingDecorators.includes("staticmethod");
      const capturedDecorators = [...pendingDecorators];
      const capturedDecoratorLines = [...pendingDecoratorLines];
      pendingDecorators = [];
      pendingDecoratorLines = [];

      // Collect full signature — may span multiple lines if params are multiline
      // Count unbalanced open parens to know when signature ends
      let sigLines = [line];
      let openParens =
        (line.match(/\(/g) || []).length - (line.match(/\)/g) || []).length;
      let j = i + 1;
      while (openParens > 0 && j < lines.length) {
        const next = lines[j];
        sigLines.push(next);
        openParens +=
          (next.match(/\(/g) || []).length - (next.match(/\)/g) || []).length;
        j++;
      }

      // Parse the flattened signature
      const flatSig = sigLines.map((l) => l.trim()).join(" ");
      const sigMatch = flatSig.match(
        /^(?:async\s+)?def\s+\w+\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)\s*(?:->\s*([^:]+?))?\s*:/
      );

      let paramStr = sigMatch ? sigMatch[1].trim() : "...";
      const returnType = sigMatch ? sigMatch[2]?.trim() : undefined;

      // Strip leading self/cls from method params (unless @staticmethod)
      const isMethod = indent > 0 && currentClass !== null;
      if (isMethod && !isStatic) {
        paramStr = paramStr.replace(/^(?:self|cls)\s*,?\s*/, "").trim();
      }

      const displayName =
        isMethod && currentClass ? `${currentClass.name}.${name}` : name;
      const sig = `${displayName}(${paramStr})${returnType ? ` -> ${returnType}` : ""}`;
      const isExported = indent === 0 && !name.startsWith("_");

      // Account for any multiline continuation lines consumed above
      let defLineStart = charOffset;
      if (j > i + 1) {
        for (let k = i + 1; k < j; k++) {
          charOffset += lines[k].length + 1;
        }
        i = j - 1; // outer for-loop will i++ to j
      }
      charOffset += lineLen + 1;

      functions.push({
        name,
        signature: sig,
        isExported,
        isAsync,
        isMethod,
        className: isMethod ? currentClass?.name : undefined,
        bodyStart: defLineStart,
        bodyEnd: defLineStart + lineLen, // refined after the loop
        indent,
        decorators: capturedDecorators.length ? capturedDecorators : undefined,
      });

      if (isExported) {
        if (!explicitExports || explicitExports.has(name)) {
          // Derive kind from decorators
          const HTTP_VERBS = ["get", "post", "put", "patch", "delete", "head", "options"];
          let kind: ExportInfo["kind"] = "function";
          let routeMethod: string | undefined;
          let routePath: string | undefined;
          const isAbstractMethod = capturedDecorators.includes("abstractmethod") ||
            capturedDecorators.includes("abc.abstractmethod");

          for (const decLine of capturedDecoratorLines) {
            // Match @router.get("/path") or @app.route("/path", methods=["GET"])
            const verbMatch = decLine.match(/@[\w.]+\.(get|post|put|patch|delete|head|options)\s*\(\s*["']([^"']+)["']/i);
            if (verbMatch) {
              kind = "route";
              routeMethod = verbMatch[1].toUpperCase();
              routePath = verbMatch[2];
              break;
            }
            // @app.route("/path") style
            const routeMatch = decLine.match(/@[\w.]*route\s*\(\s*["']([^"']+)["']/i);
            if (routeMatch) {
              kind = "route";
              routePath = routeMatch[1];
              // extract methods=["GET"] if present
              const methodsMatch = decLine.match(/methods\s*=\s*\[["'](\w+)["']/i);
              routeMethod = methodsMatch ? methodsMatch[1].toUpperCase() : "GET";
              break;
            }
          }

          // @pytest.fixture
          if (kind === "function" && capturedDecorators.some((d) => d === "fixture" || d.endsWith(".fixture"))) {
            kind = "fixture";
          }

          exports.push({
            name,
            kind,
            signature: sig,
            decorators: capturedDecorators.length ? capturedDecorators : undefined,
            routeMethod,
            routePath,
            isAbstract: isAbstractMethod || undefined,
          });
        }
      }

      continue;
    }

    pendingDecorators = [];
    pendingDecoratorLines = [];
    charOffset += lineLen + 1;
  }

  // Refine bodyEnd: each function body ends where the next function/class
  // at same or shallower indent begins (or end of file for the last one)
  for (let i = 0; i < functions.length; i++) {
    let end = source.length;
    for (let j = i + 1; j < functions.length; j++) {
      if (functions[j].indent <= functions[i].indent) {
        end = functions[j].bodyStart;
        break;
      }
    }
    functions[i].bodyEnd = end;
  }

  return { exports, functions };
}

// ---------------------------------------------------------------------------
// Call-site extraction
// ---------------------------------------------------------------------------

function extractCallSites(source: string): CallSite[] {
  const sites: CallSite[] = [];
  const callRe = /\b([a-zA-Z_]\w*)\s*\(/g;
  let m: RegExpExecArray | null;
  while ((m = callRe.exec(source)) !== null) {
    if (!PY_KEYWORDS.has(m[1])) {
      sites.push({ name: m[1], position: m.index });
    }
  }
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
  const { exports, functions } = extractDefinitions(source);
  return {
    absolutePath,
    relativePath,
    mirrorPath,
    imports: extractImports(source),
    exports,
    functions,
    callSites: extractCallSites(source),
    inRepoCalls: [],
    callIns: [],
    entrypointFingerprints: detectEntrypointFingerprints(relativePath, source, functions),
  };
}

// ---------------------------------------------------------------------------
// Pass 2: cross-file call graph (name-based)
// ---------------------------------------------------------------------------

function buildCallGraph(files: FileData[]): void {
  // Build symbol map: exported name → files that export it.
  // TODO: name-based matching can produce false positives if two different
  // in-repo files export a function with the same name. A future version could
  // use Python's ast module (via subprocess) for unambiguous resolution.
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
        if (src.relativePath === file.relativePath) continue;

        if (
          !file.inRepoCalls.some(
            (c) => c.calleeName === site.name && c.sourceFile === src.relativePath
          )
        ) {
          file.inRepoCalls.push({
            calleeName: site.name,
            sourceFile: src.relativePath,
            position: site.position,
          });
        }

        let callerName: string | undefined;
        const callerFn = file.functions.find(
          (fn) => site.position >= fn.bodyStart && site.position <= fn.bodyEnd
        );
        if (callerFn) {
          if (callerFn.isMethod && callerFn.className) {
            const classExported = file.exports.some(
              (exp) => exp.kind === "class" && exp.name === callerFn.className
            );
            if (classExported) {
              callerName = callerFn.className;
            }
          } else if (callerFn.isExported) {
            callerName = callerFn.name;
          }
        }

        if (
          !src.callIns.some(
            (c) =>
              c.callerFile === file.relativePath &&
              c.calledName === site.name &&
              c.callerName === callerName
          )
        ) {
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
 * Convert a .py relative path to a wikilink target.
 * Uses mirrorStem() so __init__ and index files map to their renamed stems:
 *   "knowledge_graph/engine.py"    → "code/knowledge_graph/engine"
 *   "knowledge_graph/__init__.py"  → "code/knowledge_graph/knowledge_graph-init"
 */
function toWikiLink(relPath: string, prefix = MIRROR_PREFIX): string {
  const dir = path.posix.dirname(relPath);
  const stem = mirrorStem(relPath);
  return dir === "." ? `${prefix}/${stem}` : `${prefix}/${dir}/${stem}`;
}

/** Safe filename for a symbol name (strip punctuation used in generic declarations). */
function sanitizeSymbolName(name: string): string {
  return name.replace(/[<>,:\.\s\[\]]/g, "_").replace(/__+/g, "_").replace(/^_|_$/g, "");
}

/**
 * Convert a .py relative path + symbol name to a tier-1 symbol wikilink target.
 *   ("knowledge/graph/retriever.py", "expand_graph_neighbors") →
 *   "code/knowledge/graph/retriever/expand_graph_neighbors"
 */
function toSymbolWikiLink(relPath: string, symbolName: string, prefix = MIRROR_PREFIX): string {
  const fileLink = toWikiLink(relPath, prefix);
  return `${fileLink}/${sanitizeSymbolName(symbolName)}`;
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
  allFiles?: FileData[],
  publicApiMap?: Map<string, Set<string>>,
  moduleLinkByDir?: Map<string, string>,
): string {
  const lines: string[] = [];
  const dirRel = path.posix.dirname(file.relativePath);
  const stem = mirrorStem(file.relativePath);
  const parentFileLink = dirRel === "." ? `${prefix}/${stem}` : `${prefix}/${dirRel}/${stem}`;
  const parentModuleLink = moduleLinkByDir?.get(dirRel)
    ?? (dirRel === "." ? `${prefix}/root_module` : `${prefix}/${dirRel}_module`);

  const isPublicApi = publicApiMap?.get(file.relativePath)?.has(exp.name) ?? false;
  const fmExtra: string[] = [];
  if (isPublicApi) fmExtra.push(`publicApi: true`);
  if (exp.decorators?.length) fmExtra.push(`decorators: [${exp.decorators.join(", ")}]`);
  if (exp.routeMethod) fmExtra.push(`routeMethod: ${exp.routeMethod}`);
  if (exp.routePath)   fmExtra.push(`routePath: "${exp.routePath}"`);
  if (exp.isAbstract)  fmExtra.push(`abstract: true`);
  if (exp.isDataclass) fmExtra.push(`dataclass: true`);
  if (exp.baseClasses?.length) fmExtra.push(`baseClasses: [${exp.baseClasses.join(", ")}]`);

  lines.push(
    "---",
    "type: codeSymbol",
    "tier: 1",
    `path: ${file.relativePath}`,
    `parentFile: ${parentFileLink}`,
    `parentModule: ${parentModuleLink}`,
    `symbolKind: ${exp.kind}`,
    "language: py",
    ...fmExtra,
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    "---",
    "",
  );

  // Build header line: route gets method+path, others get kind
  let kindDisplay = `\`${exp.kind}\``;
  if (exp.kind === "route" && exp.routeMethod && exp.routePath) {
    kindDisplay = `\`${exp.kind}\` — \`${exp.routeMethod} ${exp.routePath}\``;
  } else if (exp.isDataclass) {
    kindDisplay = `\`dataclass\``;
  } else if (exp.isAbstract) {
    kindDisplay = `\`${exp.kind}\` *(abstract)*`;
  }

  lines.push(`# ${exp.name}`, "");
  lines.push(`**Kind:** ${kindDisplay}  `);
  if (exp.decorators?.length) {
    lines.push(`**Decorators:** ${exp.decorators.map((d) => `\`@${d}\``).join(", ")}  `);
  }
  lines.push(`**File:** [[${parentFileLink}]]`, "");

  if (exp.signature) {
    lines.push("## Signature", "");
    lines.push("```py");
    lines.push(exp.signature);
    lines.push("```", "");
  }

  // Base class links (class symbols only)
  if (exp.baseClasses?.length) {
    lines.push("## Inherits", "");
    for (const base of exp.baseClasses) {
      // Try to resolve to an in-repo symbol
      const resolvedBase = allFiles
        ? allFiles.find((f) => f.exports.some((e) => e.name === base))
        : null;
      if (resolvedBase) {
        lines.push(`- [[${toSymbolWikiLink(resolvedBase.relativePath, base, prefix)}|${base}]]`);
      } else {
        lines.push(`- \`${base}\``);
      }
    }
    lines.push("");
  }

  const seenTargets = new Set<string>();
  const appendCalls = (calls: InRepoCall[]) => {
    for (const call of calls) {
      const target = toSymbolWikiLink(call.sourceFile, call.calleeName, prefix);
      if (seenTargets.has(target)) continue;
      seenTargets.add(target);
      lines.push(`- [[${target}|${call.calleeName}]]`);
    }
  };

  if (exp.kind === "function" || exp.kind === "route" || exp.kind === "fixture") {
    const fn = file.functions.find(
      (f) => !f.isMethod && f.name === exp.name && f.isExported,
    );
    if (fn) {
      const ownCalls = file.inRepoCalls.filter(
        (c) => c.position >= fn.bodyStart && c.position <= fn.bodyEnd,
      );
      if (ownCalls.length) {
        lines.push("## Calls Into", "");
        appendCalls(ownCalls);
        lines.push("");
      }
    }
  } else if (exp.kind === "class") {
    const methods = file.functions.filter(
      (f) => f.isMethod && f.className === exp.name,
    );
    const classCalls: InRepoCall[] = [];
    for (const method of methods) {
      const ownCalls = file.inRepoCalls.filter(
        (c) => c.position >= method.bodyStart && c.position <= method.bodyEnd,
      );
      classCalls.push(...ownCalls);
    }
    if (classCalls.length) {
      lines.push("## Calls Into", "");
      appendCalls(classCalls);
      lines.push("");
    }
  }

  // Type annotation cross-refs: extract type names from signature, link in-repo ones
  if (allFiles && exp.signature) {
    const typeTokens = new Set<string>();
    // Match: "param: TypeName" and "-> ReturnType", including Optional[X], list[X], etc.
    const typeRe = /:\s*([A-Z][A-Za-z0-9_]*)(?:\[[^\]]*\])?|\->\s*([A-Z][A-Za-z0-9_]*)/g;
    let tm: RegExpExecArray | null;
    while ((tm = typeRe.exec(exp.signature)) !== null) {
      const t = tm[1] || tm[2];
      if (t) typeTokens.add(t);
    }
    const typeLinks: string[] = [];
    for (const typeName of typeTokens) {
      const defFile = allFiles.find((f) =>
        f.exports.some((e) => e.name === typeName && (e.kind === "class" || e.kind === "type"))
      );
      if (defFile && defFile.relativePath !== file.relativePath) {
        typeLinks.push(`[[${toSymbolWikiLink(defFile.relativePath, typeName, prefix)}|${typeName}]]`);
      }
    }
    if (typeLinks.length) {
      lines.push("## Type References", "");
      for (const link of typeLinks) lines.push(`- ${link}`);
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

/**
 * Resolve a Python relative import specifier to the matching FileData.
 * e.g. from "knowledge_graph/retriever.py", specifier ".models"
 *   → "knowledge_graph/models.py"
 */
function resolveInternal(
  fromRelPath: string,
  specifier: string,
  files: FileData[]
): FileData | null {
  if (!specifier.startsWith(".")) return null;

  const dotMatch = specifier.match(/^(\.+)/);
  const dots = dotMatch ? dotMatch[1].length : 1;
  const moduleName = specifier.slice(dots); // e.g. "models" from ".models"

  let baseDir = path.posix.dirname(fromRelPath);
  for (let i = 1; i < dots; i++) {
    baseDir = path.posix.dirname(baseDir);
  }

  if (!moduleName) {
    // "from . import X" — the package __init__
    const candidate = path.posix.join(baseDir, "__init__.py");
    return files.find((f) => f.relativePath === candidate) ?? null;
  }

  // e.g. ".models" → baseDir/models.py
  // e.g. ".sub.module" → baseDir/sub/module.py
  const modPath = moduleName.replace(/\./g, "/");
  const candidate = path.posix.join(baseDir, modPath) + ".py";
  const match = files.find((f) => f.relativePath === candidate);
  if (match) return match;

  // Also try as a package: baseDir/modPath/__init__.py
  const initCandidate = path.posix.join(baseDir, modPath, "__init__.py");
  return files.find((f) => f.relativePath === initCandidate) ?? null;
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

  // ── Frontmatter ───────────────────────────────────────────────────────────
  const dirRel = path.posix.dirname(file.relativePath);
  const parentModuleLink = moduleLinkByDir?.get(dirRel)
    ?? (dirRel === "." ? `${prefix}/root_module` : `${prefix}/${dirRel}_module`);

  lines.push(
    "---",
    "type: codeUnit",
    "tier: 2",
    `path: ${file.relativePath}`,
    `parentModule: ${parentModuleLink}`,
    "language: py",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    ...(file.entrypointFingerprints.length ? ["  - entrypoint"] : []),
    "---",
    ""
  );

  // ── Title ─────────────────────────────────────────────────────────────────
  // For __init__ and index files the filename alone is meaningless — use the full path
  const title = (filename === "__init__.py" || filename === "index.py")
    ? file.relativePath
    : filename;
  lines.push(`# ${title}`, "");
  lines.push(`> \`${file.relativePath}\``, "");

  // ── Exports ───────────────────────────────────────────────────────────────
  if (file.exports.length) {
    lines.push("## Exports", "");
    for (const exp of file.exports) {
      const display = exp.signature ? `\`${exp.signature}\`` : `\`${exp.name}\``;
      lines.push(`- ${display} *(${exp.kind})*`);
    }
    lines.push("");
  }

  // ── Imports ───────────────────────────────────────────────────────────────
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
        const bindingStr = imp.bindings.length
          ? ` — \`${imp.bindings.join("`, `")}\``
          : "";
        lines.push(`- ${linkTarget}${bindingStr}`);
      }
      lines.push("");
    }

    if (externalImports.length) {
      lines.push("### External", "");
      for (const imp of externalImports) {
        const bindingStr = imp.bindings.length
          ? ` — \`${imp.bindings.join("`, `")}\``
          : "";
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

      // Per-function in-repo calls by position range
      const ownCalls = file.inRepoCalls.filter(
        (c) => c.position >= fn.bodyStart && c.position <= fn.bodyEnd
      );

      lines.push(`### \`${fn.signature}\`${flagStr}`);
      if (ownCalls.length) {
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

  // ── In-Repo Calls (file-level summary) ────────────────────────────────────
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
// Tier-3: Module note generation (Python)
// ---------------------------------------------------------------------------

/**
 * Absolute path for a tier-3 module note.
 * Tier-3 notes live ALONGSIDE their directory (at the parent level) to avoid
 * collisions with same-named tier-2 notes (e.g. orchestrator.py inside orchestrator/).
 *   root dir  → {mirrorDir}/root_module.md
 *   voyager/htn/ → {mirrorDir}/voyager/htn_module.md
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

function moduleNotePath(mirrorDir: string, dirRel: string, language = "py"): string {
  const basePath = baseModuleNotePath(mirrorDir, dirRel);
  const existingLanguage = readFrontmatterLanguage(basePath);
  if (!fs.existsSync(basePath) || existingLanguage === language || existingLanguage === null) {
    return basePath;
  }
  return basePath.replace(/\.md$/, `_${language}.md`);
}

function moduleWikiLink(mirrorDir: string, dirRel: string, prefix: string, language = "py"): string {
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
  fileEntries: { relPath: string; stem: string }[],
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
    "language: py",
    ...(workspace ? [`workspace: ${workspace}`] : []),
    "tags:",
    "  - codeUnit",
    "---",
    ""
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
// Stale mirror cleanup
// ---------------------------------------------------------------------------



// ---------------------------------------------------------------------------
// Public API (used by mirror-watcher and CLI)
// ---------------------------------------------------------------------------

export interface MirrorPyOptions {
  scanDir: string;
  mirrorDir: string;
  omitPatterns: string[];
  force: boolean;
  /** Workspace name — written into frontmatter as `workspace: {name}`. */
  workspace?: string;
  /** Wikilink prefix for cross-note references. Default: "code". */
  wikilinkPrefix?: string;
}

export async function runMirrorPy(
  opts: MirrorPyOptions,
): Promise<{ written: number; skipped: number; validPaths: Set<string> }> {
  const today = new Date().toISOString().slice(0, 10);
  const discovered = collectFiles(opts.scanDir, opts.mirrorDir, opts.omitPatterns);

  const files: FileData[] = [];
  let parseErrors = 0;
  for (const { absolutePath, relativePath, mirrorPath } of discovered) {
    try {
      files.push(extractFileData(absolutePath, relativePath, mirrorPath));
    } catch (err) {
      console.warn(`[mirror-py] WARN: failed to parse ${relativePath}: ${err}`);
      parseErrors++;
    }
  }

  buildCallGraph(files);

  // Build public API set: symbol names re-exported through any __init__.py
  // {relPath of defining file} → Set<symbol name>
  const publicApiMap = new Map<string, Set<string>>();
  for (const initFile of files) {
    if (!initFile.relativePath.endsWith("__init__.py")) continue;
    const pkgDir = path.posix.dirname(initFile.relativePath);
    for (const imp of initFile.imports) {
      if (!imp.isInternal) continue;
      const resolved = resolveInternal(initFile.relativePath, imp.specifier, files);
      if (!resolved) continue;
      // "from .models import Foo, Bar" → mark Foo, Bar as public in resolved file
      for (const binding of imp.bindings) {
        if (binding === "*") {
          // star export: all exports of the resolved file become public
          for (const exp of resolved.exports) {
            if (!publicApiMap.has(resolved.relativePath)) publicApiMap.set(resolved.relativePath, new Set());
            publicApiMap.get(resolved.relativePath)!.add(exp.name);
          }
        } else {
          if (!publicApiMap.has(resolved.relativePath)) publicApiMap.set(resolved.relativePath, new Set());
          publicApiMap.get(resolved.relativePath)!.add(binding);
        }
      }
    }
  }

  let written = 0;
  let skipped = 0;
  const prefix = opts.wikilinkPrefix ?? MIRROR_PREFIX;
  const byDir = buildDirectoryEntries(files);
  const moduleLinkByDir = new Map(
    [...byDir.keys()].map((dirRel) => [dirRel, moduleWikiLink(opts.mirrorDir, dirRel, prefix, "py")]),
  );
  const validMirrorPaths = new Set<string>();

  for (const file of files) {
    validMirrorPaths.add(path.resolve(file.mirrorPath));

    const definedSymbols = file.exports.filter((exp) => exp.kind !== "reexport");
    for (const exp of definedSymbols) {
      const symPath = symbolNotePath(opts.mirrorDir, file.relativePath, exp.name);
      validMirrorPaths.add(path.resolve(symPath));
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
        // mirror doesn't exist yet — proceed
      }
    }

    if (!isStale) {
      for (const exp of definedSymbols) {
        const symPath = symbolNotePath(opts.mirrorDir, file.relativePath, exp.name);
        if (!fs.existsSync(symPath)) {
          const symMarkdown = generateSymbolNote(file, exp, today, prefix, opts.workspace, files, publicApiMap, moduleLinkByDir);
          const symPreserved = extractSummarySection(symPath);
          const symFinal = symPreserved ? symMarkdown.trimEnd() + "\n\n" + symPreserved + "\n" : symMarkdown;
          fs.mkdirSync(path.dirname(symPath), { recursive: true });
          fs.writeFileSync(symPath, symFinal, "utf8");
        }
      }
      skipped++;
      continue;
    }

    const markdown = generateMarkdown(file, files, today, prefix, opts.workspace, moduleLinkByDir);
    const preserved = extractSummarySection(file.mirrorPath);
    const final = preserved ? markdown.trimEnd() + "\n\n" + preserved + "\n" : markdown;
    fs.mkdirSync(path.dirname(file.mirrorPath), { recursive: true });
    fs.writeFileSync(file.mirrorPath, final, "utf8");
    written++;

    for (const exp of definedSymbols) {
      const symPath = symbolNotePath(opts.mirrorDir, file.relativePath, exp.name);
      const symMarkdown = generateSymbolNote(file, exp, today, prefix, opts.workspace, files, publicApiMap, moduleLinkByDir);
      const symPreserved = extractSummarySection(symPath);
      const symFinal = symPreserved ? symMarkdown.trimEnd() + "\n\n" + symPreserved + "\n" : symMarkdown;
      fs.mkdirSync(path.dirname(symPath), { recursive: true });
      fs.writeFileSync(symPath, symFinal, "utf8");
    }
  }

  for (const [dirRel, fileEntries] of byDir) {
    const modPath = moduleNotePath(opts.mirrorDir, dirRel, "py");
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

  if (parseErrors > 0) console.warn(`[mirror-py] ${parseErrors} files failed to parse`);
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
    console.log("[mirror-py] no workspaces.json found, running against cwd");
    const { written, skipped } = await runMirrorPy({ scanDir: cwd, mirrorDir: path.join(mdDbPath, "code"), omitPatterns: DEFAULT_OMIT, force });
    console.log(`  written=${written} skipped=${skipped}`);
    return;
  }

  for (const ws of workspaces) {
    if (!ws.languages.includes("py")) continue;
    const mirrorDir = path.join(mdDbPath, "code", ws.name);
    console.log(`[mirror-py] ${ws.name} (${ws.sourceDir})`);
    const { written, skipped } = await runMirrorPy({
      scanDir: ws.sourceDir,
      mirrorDir,
      omitPatterns: DEFAULT_OMIT,
      force,
      workspace: ws.name,
      wikilinkPrefix: `code/${ws.name}`,
    });
    console.log(`  written=${written} skipped=${skipped}`);
  }
}

// Only run main() when this file is the entrypoint (e.g. npx tsx automation/scripts/mirror-codebase-py.ts)
if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
