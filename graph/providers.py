"""Provider-agnostic embedding configuration and factory.

Reads config from graph/config.py (config.json + ~/.context-garden/.env).
Supports ollama, openai, and local (no embeddings).

EmbedProvider Protocol is the public interface for embedding implementations.
"""

from __future__ import annotations

import logging
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
    """Read embedding config from config.json + ~/.context-garden/.env.

    Delegates to graph.config.get_embed_config() which reads the canonical
    config.json file (<dataDir>/.context-garden/config.json) and resolves
    apiKeyRef secrets from ~/.context-garden/.env.
    """
    from .config import get_embed_config as _from_config
    return _from_config()


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
            host = str(config.get("host", "https://api.openai.com")).rstrip("/")
            # Normalize to provider root if a full /v1 path was supplied.
            if host.endswith("/v1"):
                host = host[:-3]
            url = f"{host}/v1/models"
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
