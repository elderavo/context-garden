"""Entry point: python -m knowledge_graph

Starts the JSON-RPC stdio server that the TS ContextEngine bridges to.
"""

import sys
import argparse

from .server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="ObsidiClaw Knowledge Graph service")
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Run in standalone mode for manual JSON queries on stdin",
    )
    args = parser.parse_args()
    run_server(standalone=args.standalone)


if __name__ == "__main__":
    main()
