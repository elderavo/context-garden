"""BFS shortest-path finder over the SimplePropertyGraphStore.

Treats all edges as bidirectional for pathfinding but records actual
direction in the result so consumers see the relationship semantics.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from llama_index.core.graph_stores import SimplePropertyGraphStore

from .models import PathStep

# ---------------------------------------------------------------------------
# Adjacency builder
# ---------------------------------------------------------------------------

# Each adjacency entry: (neighbor_id, edge_label, direction_from_this_node)
_AdjEntry = tuple[str, str, str]  # (neighbor, label, "outgoing"|"incoming")


def _build_adjacency(
    graph_store: SimplePropertyGraphStore,
    allowed_edge_types: Optional[set[str]] = None,
) -> dict[str, list[_AdjEntry]]:
    """Build an undirected adjacency dict from the graph store's triplets.

    For each directed edge (src --LABEL--> tgt) we add:
      adj[src] += (tgt, LABEL, "outgoing")
      adj[tgt] += (src, LABEL, "incoming")

    This lets BFS traverse edges in either direction while recording
    the actual edge semantics for the result.
    """
    adj: dict[str, list[_AdjEntry]] = {}

    # Use the internal graph's get_triplets() — the wrapper on
    # SimplePropertyGraphStore returns [] when no filters are passed.
    triplets = graph_store.graph.get_triplets()
    for source_node, relation, target_node in triplets:
        src = getattr(source_node, "name", "")
        tgt = getattr(target_node, "name", "")
        label = getattr(relation, "label", "")

        if not src or not tgt:
            continue
        if allowed_edge_types and label not in allowed_edge_types:
            continue

        adj.setdefault(src, []).append((tgt, label, "outgoing"))
        adj.setdefault(tgt, []).append((src, label, "incoming"))

    return adj


# ---------------------------------------------------------------------------
# BFS shortest path
# ---------------------------------------------------------------------------


def find_shortest_path(
    graph_store: SimplePropertyGraphStore,
    start_id: str,
    end_id: str,
    allowed_edge_types: Optional[set[str]] = None,
    max_depth: int = 8,
) -> Optional[list[PathStep]]:
    """Find the shortest path between two note IDs in the graph.

    Returns a list of PathStep from start to end, or None if no path exists
    within max_depth hops.
    """
    if start_id == end_id:
        return [PathStep(node_id=start_id, edge_label="", edge_direction="", from_node_id="")]

    adj = _build_adjacency(graph_store, allowed_edge_types)

    if start_id not in adj and start_id != end_id:
        # Start node has no edges at all — might still be end if equal (handled above)
        return None

    # BFS with parent tracking
    # parent[node] = (from_node, edge_label, edge_direction)
    parent: dict[str, tuple[str, str, str]] = {}
    visited: set[str] = {start_id}
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue

        for neighbor, label, direction in adj.get(current, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parent[neighbor] = (current, label, direction)

            if neighbor == end_id:
                # Reconstruct path
                return _reconstruct(start_id, end_id, parent)

            queue.append((neighbor, depth + 1))

    return None  # No path found


def _reconstruct(
    start_id: str,
    end_id: str,
    parent: dict[str, tuple[str, str, str]],
) -> list[PathStep]:
    """Walk parent pointers back from end to start, then reverse."""
    steps: list[PathStep] = []
    current = end_id

    while current != start_id:
        from_node, label, direction = parent[current]
        steps.append(PathStep(
            node_id=current,
            edge_label=label,
            edge_direction=direction,
            from_node_id=from_node,
        ))
        current = from_node

    # Add start node
    steps.append(PathStep(node_id=start_id, edge_label="", edge_direction="", from_node_id=""))
    steps.reverse()
    return steps
