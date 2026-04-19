"""ContextGarden Python config loader - reads config.json + ~/.context-garden/.env.

This module is the single source of truth for provider configuration in the
Python daemon. It replaces the CG_EMBED_* / CG_LLM_* environment variable
approach in providers.py.

Config file location:
    <dataDir>/.context-garden/config.json  (dataDir defaults to cwd)

Secrets file location:
    ~/.context-garden/.env

Secret references in config.json use the form:
    "apiKeyRef": "env:MY_VAR_NAME"

Only the var named in the ref is read from the environment (not a blanket env
override). The .env file is the preferred place to store secrets.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (kept in sync with src/config.ts DEFAULTS)
# ---------------------------------------------------------------------------

_EMBED_DEFAULTS: dict[str, Any] = {
    "provider": "ollama",
    "model": "nomic-embed-text:latest",
    "host": "http://localhost:11434",
    "context_length": 512,
    "api_key": "",
}

_SYNTH_DEFAULTS: dict[str, Any] = {
    "provider": "ollama",
    "model": "cogito:8b",
    "host": "http://localhost:11434",
    "context_window": 32768,
    "max_tokens": 4096,
    "api_key": "",
}


# ---------------------------------------------------------------------------
# .env parser
# ---------------------------------------------------------------------------


def _load_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (no variable expansion, no shell quoting)."""
    secrets: dict[str, str] = {}
    try:
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            # Strip surrounding single or double quotes
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            secrets[k] = v
    except OSError:
        # Secrets file is optional.
        return secrets
    return secrets


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def _resolve_key(
    api_key_ref: str | None,
    raw_api_key: str | None,
    secrets: dict[str, str],
) -> str:
    """Resolve an apiKeyRef or fall back to a raw key.

    Preference order:
      1. env: ref resolved against ~/.context-garden/.env
      2. empty string
    """
    if api_key_ref and api_key_ref.startswith("env:"):
        var = api_key_ref[4:]
        value = secrets.get(var, "")
        if not value:
            log.warning(
                "apiKeyRef %r references '%s' but it is not set in ~/.context-garden/.env",
                api_key_ref,
                var,
            )
        return value
    if raw_api_key:
        raise RuntimeError(
            "config.json contains a raw 'apiKey'. Hard cutover requires apiKeyRef values "
            "resolved from ~/.context-garden/.env."
        )
    return ""


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """Load and return the full config dict.

    Returns::

        {
            "embedding":   {provider, model, host, context_length, api_key},
            "synthesizer": {provider, model, host, context_window, max_tokens, api_key},
        }
    """
    config_path = _DATA_DIR / ".context-garden" / "config.json"
    env_file = Path.home() / ".context-garden" / ".env"

    secrets = _load_dotenv(env_file)

    raw: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text("utf-8"))
            log.debug("Loaded config from %s", config_path)
        except Exception as exc:
            log.warning("Failed to parse %s: %s - using defaults", config_path, exc)
    else:
        log.debug("No config.json at %s — using defaults", config_path)

    # --- Embedding ---
    embed_raw = raw.get("embedding", {})
    embed: dict[str, Any] = dict(_EMBED_DEFAULTS)
    for field in ("provider", "model", "host"):
        if embed_raw.get(field):
            embed[field] = embed_raw[field]
    if embed_raw.get("contextLength"):
        embed["context_length"] = int(embed_raw["contextLength"])
    embed["api_key"] = _resolve_key(
        embed_raw.get("apiKeyRef"), embed_raw.get("apiKey"), secrets
    )

    # --- Synthesizer ---
    synth_raw = raw.get("synthesizer", {})
    synth: dict[str, Any] = dict(_SYNTH_DEFAULTS)
    for field in ("provider", "model", "host"):
        if synth_raw.get(field):
            synth[field] = synth_raw[field]
    if synth_raw.get("contextWindow"):
        synth["context_window"] = int(synth_raw["contextWindow"])
    if synth_raw.get("maxTokens"):
        synth["max_tokens"] = int(synth_raw["maxTokens"])
    synth["api_key"] = _resolve_key(
        synth_raw.get("apiKeyRef"), synth_raw.get("apiKey"), secrets
    )

    return {"embedding": embed, "synthesizer": synth}


# ---------------------------------------------------------------------------
# Module-level cache (daemon reads config once at startup; restart to reload)
# ---------------------------------------------------------------------------

_cached: dict[str, Any] | None = None
_DATA_DIR: Path = Path.cwd()


def set_data_dir(data_dir: str | Path) -> None:
    """Set canonical data dir used for config resolution."""
    global _DATA_DIR, _cached
    _DATA_DIR = Path(data_dir).resolve()
    _cached = None


def get_data_dir() -> Path:
    return _DATA_DIR


def get_embed_config() -> dict[str, Any]:
    """Return embedding config (shape compatible with providers.py interface)."""
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached["embedding"]


def get_synth_config() -> dict[str, Any]:
    """Return synthesizer/LLM config."""
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached["synthesizer"]


def get_config_snapshot() -> dict[str, Any]:
    """Return a flat snapshot suitable for the MCP configure/setup tools."""
    embed = get_embed_config()
    synth = get_synth_config()
    return {
        "embedProvider": embed.get("provider", ""),
        "embedModel": embed.get("model", ""),
        "embedHost": embed.get("host", ""),
        "embedContextLength": embed.get("context_length", 512),
        "embedApiKey": embed.get("api_key", ""),
        "llmProvider": synth.get("provider", ""),
        "llmModel": synth.get("model", ""),
        "llmHost": synth.get("host", ""),
        "llmContextWindow": synth.get("context_window", 32768),
        "llmMaxTokens": synth.get("max_tokens", 4096),
        "llmApiKey": synth.get("api_key", ""),
    }


def write_config(patch: dict[str, Any], persist: bool = True) -> None:
    """Apply a flat patch (same shape as get_config_snapshot) to config.json.

    Keys understood: embedProvider, embedModel, embedHost, embedApiKey,
    llmProvider, llmModel, llmHost, llmApiKey.
    API keys are written to ~/.context-garden/.env (never config.json).
    """
    global _cached

    config_path = _DATA_DIR / ".context-garden" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    raw: dict[str, Any] = {}
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text("utf-8"))
        except Exception:
            pass

    embed_raw = raw.setdefault("embedding", {})
    synth_raw = raw.setdefault("synthesizer", {})

    mapping_embed = {
        "embedProvider": "provider",
        "embedModel": "model",
        "embedHost": "host",
    }
    mapping_synth = {
        "llmProvider": "provider",
        "llmModel": "model",
        "llmHost": "host",
    }

    for flat_key, raw_key in mapping_embed.items():
        if flat_key in patch and patch[flat_key]:
            embed_raw[raw_key] = patch[flat_key]
    for flat_key, raw_key in mapping_synth.items():
        if flat_key in patch and patch[flat_key]:
            synth_raw[raw_key] = patch[flat_key]

    # API keys go to ~/.context-garden/.env, never config.json
    env_file = Path.home() / ".context-garden" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_lines: list[str] = []
    try:
        env_lines = env_file.read_text("utf-8").splitlines()
    except OSError:
        pass

    def _upsert_env(lines: list[str], key: str, value: str) -> list[str]:
        prefix = f"{key}="
        updated = [l for l in lines if not l.startswith(prefix)]
        if value:
            updated.append(f"{key}={value}")
        return updated

    if patch.get("embedApiKey"):
        env_lines = _upsert_env(env_lines, "CG_EMBED_API_KEY", patch["embedApiKey"])
        embed_raw["apiKeyRef"] = "env:CG_EMBED_API_KEY"
    if patch.get("llmApiKey"):
        env_lines = _upsert_env(env_lines, "CG_LLM_API_KEY", patch["llmApiKey"])
        synth_raw["apiKeyRef"] = "env:CG_LLM_API_KEY"

    if persist:
        config_path.write_text(json.dumps(raw, indent=2), "utf-8")
        env_file.write_text("\n".join(env_lines) + "\n", "utf-8")
        log.info("Config written to %s", config_path)

    # Bust cache so next get_*_config() re-reads
    _cached = None
