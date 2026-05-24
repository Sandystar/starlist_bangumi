from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence

import httpx
import uvicorn

from starlist_bangumi.api import create_app


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Starlist Bangumi desktop app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--web", action="store_true", help="Open in the default browser instead of WebView"
    )
    args = parser.parse_args(argv)

    port = args.port or _find_free_port()
    url = f"http://{args.host}:{port}"
    app = create_app()
    config = uvicorn.Config(app, host=args.host, port=port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="starlist-uvicorn", daemon=True)
    thread.start()
    _wait_until_ready(f"{url}/api/health")

    print(f"Starlist Bangumi is running at {url}")
    if args.web:
        webbrowser.open(url)
        _block_until_interrupt(server)
        return

    try:
        import webview
    except Exception:
        webbrowser.open(url)
        _block_until_interrupt(server)
        return

    webview.create_window("Starlist Bangumi", url, width=1280, height=860)
    webview.start()
    server.should_exit = True
    thread.join(timeout=5)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(url: str) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError("Backend did not become ready")


def _block_until_interrupt(server: uvicorn.Server) -> None:
    try:
        while not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        sys.exit(0)
