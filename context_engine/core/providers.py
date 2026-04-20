"""Provider-agnostic embedding configuration and factory.

Reads config from graph/config.py (config.json + ~/.context-garden/.env).
Supports ollama, openai, and local (no embeddings).

EmbedProvider Protocol is the public interface for embedding implementations.
EmbeddingBackend ABC is the internal plugin contract — one class per provider.
"""

from __future__ import annotations

import logging
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EmbedProvider Protocol (public interface for external implementors)
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
# EmbeddingBackend ABC (internal plugin contract)
# ---------------------------------------------------------------------------


class EmbeddingBackend(ABC):
    """One instance per configured embedding provider.

    Bundles reachability check + LlamaIndex object creation so adding a new
    provider is a single new subclass + one entry in _BACKENDS.
    """

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict[str, Any]) -> "EmbeddingBackend": ...

    @abstractmethod
    def check_reachable(self, timeout: float = 3.0) -> bool: ...

    @abstractmethod
    def create_llama_embedding(self) -> Any:
        """Return a LlamaIndex-compatible embedding object, or None to disable vector search."""
        ...


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------


class OllamaBackend(EmbeddingBackend):
    def __init__(self, model: str, host: str) -> None:
        self.model = model
        self.host = host.rstrip("/")

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "OllamaBackend":
        return cls(config["model"], config["host"])

    def check_reachable(self, timeout: float = 3.0) -> bool:
        try:
            req = urllib.request.Request(f"{self.host}/api/version", method="GET")
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            log.warning("Ollama unreachable at %s: %s", self.host, exc)
            return False

    def create_llama_embedding(self) -> Any:
        from llama_index.embeddings.ollama import OllamaEmbedding
        return OllamaEmbedding(model_name=self.model, base_url=self.host)


class OpenAIBackend(EmbeddingBackend):
    def __init__(self, model: str, api_key: str, host: str) -> None:
        self.model = model
        self.api_key = api_key
        # Normalize to provider root so /v1/models probe works regardless of input
        self.host = host.rstrip("/").removesuffix("/v1")

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "OpenAIBackend":
        return cls(
            config["model"],
            config.get("api_key", ""),
            config.get("host", "https://api.openai.com"),
        )

    def check_reachable(self, timeout: float = 3.0) -> bool:
        try:
            req = urllib.request.Request(f"{self.host}/v1/models", method="GET")
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            log.warning("OpenAI unreachable at %s: %s", self.host, exc)
            return False

    def create_llama_embedding(self) -> Any:
        from llama_index.embeddings.openai import OpenAIEmbedding
        return OpenAIEmbedding(model=self.model, api_key=self.api_key)


class LocalBackend(EmbeddingBackend):
    """No-op backend — disables vector search, keyword-only retrieval."""

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LocalBackend":
        return cls()

    def check_reachable(self, timeout: float = 3.0) -> bool:
        return True

    def create_llama_embedding(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Backend registry + factory
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[EmbeddingBackend]] = {
    "ollama": OllamaBackend,
    "openai": OpenAIBackend,
    "local": LocalBackend,
}


def get_backend(config: dict[str, Any]) -> EmbeddingBackend:
    """Instantiate the backend for the given provider config."""
    provider = config.get("provider", "ollama")
    cls = _BACKENDS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown embedding provider: {provider!r}. "
            f"Registered providers: {list(_BACKENDS)}"
        )
    return cls.from_config(config)


# ---------------------------------------------------------------------------
# Config accessor (delegates to graph.config)
# ---------------------------------------------------------------------------


def get_embed_config() -> dict[str, Any]:
    """Read embedding config from config.json + ~/.context-garden/.env."""
    from ..config import get_embed_config as _from_config
    return _from_config()


# ---------------------------------------------------------------------------
# Backward-compat shims (kept so call sites outside engine.py don't break)
# ---------------------------------------------------------------------------


def check_reachable(config: dict[str, Any], timeout: float = 3.0) -> bool:
    return get_backend(config).check_reachable(timeout)


def create_embedding(config: dict[str, Any]) -> Optional[Any]:
    return get_backend(config).create_llama_embedding()
