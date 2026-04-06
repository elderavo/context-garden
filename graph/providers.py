"""Provider-agnostic embedding configuration and factory.

Reads CG_EMBED_* environment variables to determine which embedding
provider to use. Supports ollama, openai, and local (no embeddings).

EmbedProvider Protocol is the public interface for embedding implementations.
"""

from __future__ import annotations

import logging
import os
import urllib.request
import urllib.error
from typing import Any, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EmbedProvider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbedProvider(Protocol):
    """Protocol for pluggable embedding backends."""

    def get_text_embedding(self, text: str) -> list[float]:
        """Embed a single text string."""
        ...

    def get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of text strings."""
        ...

    @property
    def model_id(self) -> str:
        """Stable identifier for this provider+model combo (used for cache invalidation)."""
        ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def get_embed_config() -> dict[str, Any]:
    """Read embedding config from environment variables with defaults.

    Supports both CG_EMBED_* (canonical) and legacy OBSIDI_EMBED_* names.
    CG_EMBED_* takes precedence when both are set.
    """
    def _env(cg_key: str, legacy_key: str, default: str) -> str:
        return os.environ.get(cg_key) or os.environ.get(legacy_key) or default

    return {
        "provider": _env("CG_EMBED_PROVIDER", "OBSIDI_EMBED_PROVIDER", "ollama"),
        "model": _env("CG_EMBED_MODEL", "OBSIDI_EMBED_MODEL", "nomic-embed-text:latest"),
        "host": _env("CG_EMBED_HOST", "OBSIDI_EMBED_HOST", "http://localhost:11434"),
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "context_length": int(_env("CG_EMBED_CONTEXT_LENGTH", "OBSIDI_EMBED_CONTEXT_LENGTH", "512")),
    }


# ---------------------------------------------------------------------------
# Reachability check
# ---------------------------------------------------------------------------


def check_reachable(config: dict[str, Any], timeout: float = 3.0) -> bool:
    """Check whether the embedding provider is reachable.

    Returns True if the provider health endpoint responds, False otherwise.
    """
    provider = config.get("provider", "ollama")

    if provider == "local":
        return True

    try:
        if provider == "ollama":
            url = f"{config['host']}/api/version"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout):
                return True

        elif provider == "openai":
            api_key = config.get("api_key", "")
            url = "https://api.openai.com/v1/models"
            req = urllib.request.Request(url, method="GET")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=timeout):
                return True

    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        log.warning("Embedding provider '%s' unreachable at %s: %s", provider, config.get("host", ""), exc)
        return False

    return False


# ---------------------------------------------------------------------------
# Embedding factory
# ---------------------------------------------------------------------------


def create_embedding(config: dict[str, Any]) -> Optional[Any]:
    """Create a LlamaIndex embedding model from config.

    Returns None for provider="local" (no embeddings).
    """
    provider = config.get("provider", "ollama")

    if provider == "local":
        return None

    if provider == "ollama":
        from llama_index.embeddings.ollama import OllamaEmbedding

        return OllamaEmbedding(
            model_name=config["model"],
            base_url=config["host"],
        )

    if provider == "openai":
        from llama_index.embeddings.openai import OpenAIEmbedding

        return OpenAIEmbedding(
            model=config["model"],
            api_key=config.get("api_key", ""),
        )

    log.error("Unknown embedding provider: %s", provider)
    return None
