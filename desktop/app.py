import json
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import webview


ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from backend.settings import settings


def server_ready(url: str) -> bool:
    try:
        with build_opener(ProxyHandler({})).open(f"{url}/health", timeout=1) as response:
            return json.load(response).get("status") == "ok"
    except (OSError, URLError, ValueError, AttributeError):
        return False


def main() -> None:
    url = f"http://127.0.0.1:{settings.api_port}"
    server = None
    try:
        if not server_ready(url):
            server = subprocess.Popen(
                [sys.executable, "-m", "backend.main"],
                cwd=ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            deadline = monotonic() + 30
            while not server_ready(url):
                if server.poll() is not None or monotonic() >= deadline:
                    raise RuntimeError(f"Сервер Loomis недоступен по адресу {url}")
                sleep(0.2)
        webview.create_window("Loomis", url, width=1280, height=820)
        webview.start()
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    main()
