import json
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

import webview


ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from backend.settings import settings


class DesktopAPI:
    def select_folder(self) -> str | None:
        folders = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if isinstance(folders, str):
            return folders or None
        return folders[0] if folders else None


def set_dark_title_bar(window) -> None:
    from ctypes import byref, c_int, windll
    from ctypes.wintypes import HWND

    hwnd = HWND(window.native.Handle.ToInt64())
    enabled = c_int(1)
    set_attribute = windll.dwmapi.DwmSetWindowAttribute
    if set_attribute(hwnd, 20, byref(enabled), 4) != 0:
        set_attribute(hwnd, 19, byref(enabled), 4)


def enable_workspace_popup(window, origin: str) -> None:
    from System import Action

    def configure():
        browser = window.native.browser
        control = getattr(browser.webview, "CoreWebView2", None)
        if control is None or getattr(browser, "_loomis_popup_handler", None):
            return

        def open_window(sender, args):
            target = urlsplit(str(args.get_Uri()))
            if (f"{target.scheme}://{target.netloc}" == origin
                    and target.path == "/"
                    and parse_qs(target.query).get("popout") == ["workspace"]):
                # Let WebView2 create the popup with its same-origin opener.
                return
            browser.on_new_window_request(sender, args)

        control.NewWindowRequested -= browser.on_new_window_request
        control.NewWindowRequested += open_window
        browser._loomis_popup_handler = open_window

    window.native.Invoke(Action(configure))


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
        # Load the FastAPI-mounted UI on the same origin as the API and WebSocket.
        api = DesktopAPI()
        window = webview.create_window(
            "Loomis", f"{url}/index.html", width=1280, height=820, background_color="#0b1020", js_api=api,
        )
        api._window = window
        if sys.platform == "win32":
            window.events.shown += lambda: set_dark_title_bar(window)
            window.events.loaded += lambda: enable_workspace_popup(window, url)
        webview.start(private_mode=False)
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    main()
