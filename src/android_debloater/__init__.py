"""Console launcher for the loopback-only Android Debloater workspace."""

import argparse
import socket
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
from platformdirs import user_data_path


class ReadyServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started and self.config.app.state.open_browser:
            url = self.config.app.state.url

            def open_when_ready():
                try:
                    with urllib.request.urlopen(url + "/api/health", timeout=5) as response:
                        if response.status == 200:
                            webbrowser.open(url)
                except OSError:
                    pass  # The printed URL remains usable if browser launching fails.

            threading.Thread(target=open_when_ready, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and manage Android apps in a local browser.")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--adb-path", type=Path)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=user_data_path("Android Debloater", appauthor=False)
        if sys.platform == "darwin"
        else user_data_path("android-debloater", appauthor=False),
    )
    parser.add_argument("--read-only", action="store_true", help="Block every phone-changing command.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    from .server import create_app

    app = create_app(args.data_dir, port=args.port, adb_path=args.adb_path, read_only=args.read_only)
    app.state.open_browser = not args.no_browser
    print(f"Android Debloater: {app.state.url}\nData directory: {args.data_dir.resolve()}", flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
    # Bind before starting the application so an occupied port cannot open another server's UI.
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", args.port))
        ReadyServer(config).run(sockets=[sock])
    except KeyboardInterrupt:
        pass  # Uvicorn completes application shutdown before propagating Ctrl+C.
    except OSError as exc:
        parser.exit(1, f"Cannot start local server: {exc}\n")
    finally:
        sock.close()
