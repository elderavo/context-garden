import { runMirrorTs } from "./parsers/typescript.js";
import { runMirrorPy } from "./parsers/python.js";
import { runMirrorC } from "./parsers/c.js";

type MirrorRunner = (
  opts: LanguageMirrorOptions,
) => Promise<LanguageMirrorResult>;

export interface LanguageMirrorOptions {
  scanDir: string;
  mirrorDir: string;
  omitPatterns: string[];
  force: boolean;
  workspace?: string;
  wikilinkPrefix?: string;
}

export interface LanguageMirrorResult {
  written: number;
  skipped: number;
  validPaths: Set<string>;
}

export interface LanguageDescriptor {
  id: WorkspaceLanguage;
  displayName: string;
  globPattern: string;
  defaultOmit: string[];
  runMirror: MirrorRunner;
}

export type WorkspaceLanguage = string;

const registry = new Map<WorkspaceLanguage, LanguageDescriptor>();
let defaultsRegistered = false;

export function registerLanguage(descriptor: LanguageDescriptor): void {
  registry.set(descriptor.id, descriptor);
}

export function unregisterLanguage(id: WorkspaceLanguage): void {
  registry.delete(id);
}

export function getLanguage(id: WorkspaceLanguage): LanguageDescriptor | undefined {
  ensureDefaultLanguages();
  return registry.get(id);
}

export function listLanguages(): LanguageDescriptor[] {
  ensureDefaultLanguages();
  return [...registry.values()];
}

export function ensureDefaultLanguages(): void {
  if (defaultsRegistered) return;
  if (!registry.has("ts")) {
    registerLanguage({
      id: "ts",
      displayName: "TypeScript",
      globPattern: "**/*.ts",
      defaultOmit: ["dist", "node_modules", "_legacy", ".claude", "*.d.ts"],
      runMirror: runMirrorTs,
    });
  }
  if (!registry.has("py")) {
    registerLanguage({
      id: "py",
      displayName: "Python",
      globPattern: "**/*.py",
      defaultOmit: ["__pycache__", "*.pyi", ".venv", "env", "venv", "dist", "node_modules"],
      runMirror: runMirrorPy,
    });
  }
  if (!registry.has("c")) {
    registerLanguage({
      id: "c",
      displayName: "C",
      globPattern: "**/*.c",
      defaultOmit: ["build", "dist", "out", "cmake-build-debug", "cmake-build-release", "node_modules", ".git", ".context-garden"],
      runMirror: runMirrorC,
    });
  }
  defaultsRegistered = true;
}
