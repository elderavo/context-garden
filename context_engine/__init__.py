"""ObsidiClaw Knowledge Graph — PropertyGraphIndex-backed retrieval engine."""

# LlamaIndex uses async internally; nest_asyncio prevents "nested event loop"
# errors when calling synchronous .retrieve() from a non-async context.
import nest_asyncio
nest_asyncio.apply()
