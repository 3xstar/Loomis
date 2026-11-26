import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic, sleep, time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

import webview
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ICON_PATH = Path(__file__).with_name("ui") / "logo.ico"
SESSION_PATH = Path.home() / ".loomis" / "desktop_session.json"
PREFERENCES_PATH = SESSION_PATH.with_name("desktop_preferences.json")
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from backend.settings import settings


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            temporary.replace(path)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)


def prepare_windows_icon() -> None:
    with Image.open(ICON_PATH.with_name("favicon.png")) as source:
        icon = source.convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
    with BytesIO() as output:
        icon.save(output, format="ICO", sizes=[(size, size) for size in (16, 32, 48, 64, 128, 256)])
        data = output.getvalue()
    if not ICON_PATH.exists() or ICON_PATH.read_bytes() != data:
        write_atomic(ICON_PATH, data)


class DesktopAPI:
    def __init__(self) -> None:
        self._preferences_lock = Lock()

    def save_session(self, token: str) -> None:
        write_atomic(SESSION_PATH, json.dumps({"access_token": token}).encode("utf-8"))

    def load_preferences(self) -> dict:
        try:
            data = json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_preferences(self, changes: dict) -> None:
        with self._preferences_lock:
            preferences = self.load_preferences()
            preferences.update(changes)
            write_atomic(PREFERENCES_PATH, json.dumps(preferences, ensure_ascii=False).encode("utf-8"))

    def load_session(self) -> str | None:
        try:
            data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        token = data.get("access_token") if isinstance(data, dict) else None
        return token if isinstance(token, str) and token else None

    def clear_session(self) -> None:
        SESSION_PATH.unlink(missing_ok=True)

    def select_folder(self) -> str | None:
        file_dialog = getattr(webview, "FileDialog", None)
        folder_dialog = file_dialog.FOLDER if file_dialog is not None else webview.FOLDER_DIALOG
        folders = self._window.create_file_dialog(folder_dialog)
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
    from System.Drawing import Icon

    def configure():
        hwnd = window.native.Handle.ToInt64()
        send_message = windll.user32.SendMessageW
        send_message.argtypes = (HWND, UINT, WPARAM, LPARAM)
        send_message.restype = LPARAM
        wm_seticon = 0x0080
        icons = tuple(Icon(str(ICON_PATH), size, size) for size in (16, 32))
        for kind, icon in enumerate(icons):  # ICON_SMALL, ICON_BIG
            send_message(hwnd, wm_seticon, kind, icon.Handle.ToInt64())
        window._taskbar_icons = icons

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
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("3xstar.Loomis.Desktop.1")
        prepare_windows_icon()
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
            "Loomis", f"{url}/?v={int(time())}&desktop=1", width=1280, height=820,
            background_color="#09090b", js_api=api,
        )
        api._window = window
        if sys.platform == "win32":
            window.events.shown += lambda: set_dark_title_bar(window)
            window.events.loaded += lambda: set_windows_icon(window)
            window.events.loaded += lambda: enable_workspace_popup(window, url)
        webview.start(private_mode=True, icon=str(ICON_PATH))
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    main()
