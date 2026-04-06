"""Entry point: python -m graph

  python -m graph           — stdio JSON-RPC server (legacy / compat mode)
  python -m graph --daemon  — persistent TCP daemon (Phase 2, recommended)
  python -m graph.daemon    — same as --daemon
"""

import sys
import argparse

from .server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="ContextGarden knowledge graph service")
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Run stdio server in standalone mode for manual JSON queries on stdin",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as a persistent TCP daemon (port CG_DAEMON_PORT, default 7432)",
    )
    args = parser.parse_args()

    if args.daemon:
        from .daemon import main as daemon_main
        daemon_main()
    else:
        run_server(standalone=args.standalone)


if __name__ == "__main__":
    main()
