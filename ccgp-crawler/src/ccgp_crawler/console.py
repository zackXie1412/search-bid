from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local CCGP collection console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("The console is local-only; --host must be 127.0.0.1 or localhost")
    uvicorn.run(
        "ccgp_crawler.api:app",
        host="127.0.0.1",
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()

