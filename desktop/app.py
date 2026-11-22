import json
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep, time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

import webview


ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = Path(__file__).with_name("ui") / "logo.ico"
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


def set_windows_icon(window) -> None:
    from ctypes import windll
    from ctypes.wintypes import HWND, LPARAM, UINT, WPARAM
    from System import Action

    def configure():
        hwnd = window.native.Handle.ToInt64()
        icon = window.native.Icon.Handle.ToInt64()
        send_message = windll.user32.SendMessageW
        send_message.argtypes = (HWND, UINT, WPARAM, LPARAM)
        send_message.restype = LPARAM
        wm_seticon = 0x0080
        send_message(hwnd, wm_seticon, 0, icon)  # ICON_SMALL
        send_message(hwnd, wm_seticon, 1, icon)  # ICON_BIG

    window.native.Invoke(Action(configure))


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
            "Loomis", f"{url}/?v={int(time())}", width=1280, height=820, background_color="#09090b", js_api=api,
        )
        api._window = window
        if sys.platform == "win32":
            window.events.shown += lambda: set_dark_title_bar(window)
            window.events.shown += lambda: set_windows_icon(window)
            window.events.loaded += lambda: enable_workspace_popup(window, url)
        webview.start(private_mode=True, icon=str(ICON_PATH))
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    main()
