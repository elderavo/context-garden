from __future__ import annotations

import os
from pathlib import Path


def resolve_mirror_cli(*, data_dir: Path | None = None) -> str:
    """Resolve the TypeScript mirror CLI path with sane runtime fallbacks."""
    env_path = os.environ.get("CG_MIRROR_CLI", "").strip()
    if env_path:
        return str(Path(env_path).expanduser())

    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parent
    candidates: list[Path] = [
        repo_root / "dist" / "src" / "mirror" / "mirror-cli.js",
        Path.cwd().resolve() / "dist" / "src" / "mirror" / "mirror-cli.js",
    ]
    if data_dir is not None:
        candidates.append(Path(data_dir).resolve() / "dist" / "src" / "mirror" / "mirror-cli.js")

    for path in candidates:
        if path.exists():
            return str(path)

    # Default to repo-root layout so logs/errors point to a stable expected path.
    return str(candidates[0])
