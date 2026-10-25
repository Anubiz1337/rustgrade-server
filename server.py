"""Local development server for the RustGrade Roblox project.

Serves:
  /sync.json              manifest of Lua sources for the Studio sync script
  /catalog/*.json         generated rust.wiki catalogue snapshot
  /icons/<skinId>.bin     64x64 raw RGBA icon, downloaded from Facepunch once

The icon endpoint converts the real PNG skin icon into the raw pixel format that
Roblox's EditableImage accepts, because Roblox cannot load arbitrary external
image URLs at runtime. Nothing is fabricated: if Facepunch has no icon, the
endpoint returns 404 and the client draws a neutral placeholder.
"""

import http.server
import io
import json
import os
import socketserver
import sys
import threading
import urllib.error
import urllib.request

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from PIL import Image
except Exception:  # pragma: no cover - reported at runtime
    Image = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(BASE_DIR, "src")
ICON_CACHE = os.path.join(BASE_DIR, "tools", "icon_cache")
ICON_SIZE = 64
ICON_URL = "https://files.facepunch.com/rust/icons/inventory/rust/{id}_small.png"
USER_AGENT = "RustGradeLocalIconProxy/1.0 (local Roblox development)"
PORT = 8931

ROUTES = [
    ("shared/", "ReplicatedStorage.RustGrade.Modules.", "ModuleScript"),
    ("server/services/", "ServerScriptService.RustGradeServices.", "ModuleScript"),
    ("client/ui/", "StarterPlayer.StarterPlayerScripts.RustGradeUI.", "ModuleScript"),
    ("client/anim/", "StarterPlayer.StarterPlayerScripts.RustGradeAnim.", "ModuleScript"),
    ("client/pages/", "StarterPlayer.StarterPlayerScripts.RustGradeUI.Pages.", "ModuleScript"),
]

SPECIAL = {
    "server/MainServer.lua": ("ServerScriptService.MainServer", "Script"),
    "client/RustGradeClient.lua": ("StarterPlayer.StarterPlayerScripts.RustGradeClient", "LocalScript"),
    "client/App.lua": ("StarterPlayer.StarterPlayerScripts.RustGradeUI.App", "ModuleScript"),
    "shared/ClientBridge.lua": ("ReplicatedStorage.RustGrade.ClientBridge", "ModuleScript"),
}

_icon_lock = threading.Lock()


def build_manifest():
    entries = []
    for dirpath, _, filenames in os.walk(ROOT):
        for filename in filenames:
            if not filename.endswith(".lua"):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            if rel in SPECIAL:
                target, cls = SPECIAL[rel]
                entries.append({"url": rel, "target": target, "class": cls})
                continue
            for prefix, target_prefix, cls in ROUTES:
                if rel.startswith(prefix):
                    sub = rel[len(prefix):]
                    target = target_prefix + os.path.splitext(sub)[0].replace("/", ".")
                    entries.append({"url": rel, "target": target, "class": cls})
                    break
    entries.sort(key=lambda e: e["url"])
    return entries


def load_icon(skin_id: str) -> bytes:
    """Return 16,384 bytes of RGBA pixels, caching the result on disk."""
    if not skin_id.isdigit() or len(skin_id) > 20:
        raise ValueError("invalid skin id")
    cached = os.path.join(ICON_CACHE, skin_id + ".bin")
    if os.path.exists(cached):
        with open(cached, "rb") as handle:
            data = handle.read()
        if len(data) == ICON_SIZE * ICON_SIZE * 4:
            return data
        os.remove(cached)

    if Image is None:
        raise RuntimeError("Pillow is not installed (pip install Pillow)")

    with _icon_lock:
        if os.path.exists(cached):
            with open(cached, "rb") as handle:
                data = handle.read()
            if len(data) == ICON_SIZE * ICON_SIZE * 4:
                return data

        request = urllib.request.Request(
            ICON_URL.format(id=skin_id),
            headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/*"},
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = response.read()
        image = Image.open(io.BytesIO(payload))
        image = image.convert("RGBA")
        image = image.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        data = image.tobytes()
        if len(data) != ICON_SIZE * ICON_SIZE * 4:
            raise RuntimeError("unexpected RGBA size %d" % len(data))
        os.makedirs(ICON_CACHE, exist_ok=True)
        tmp = cached + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, cached)
        return data


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/sync.json":
            payload = json.dumps(build_manifest(), ensure_ascii=False).encode("utf-8")
            self._send_bytes(payload, "application/json; charset=utf-8")
            return
        if path.startswith("/icons/"):
            self._serve_icon(path)
            return
        super().do_GET()

    def _serve_icon(self, path):
        name = path[len("/icons/"):]
        if not name.endswith(".bin"):
            self.send_error(404, "not found")
            return
        skin_id = name[:-4]
        try:
            data = load_icon(skin_id)
        except ValueError:
            self.send_error(400, "invalid skin id")
            return
        except urllib.error.HTTPError as exc:
            self.send_error(404 if exc.code == 404 else 502, "icon unavailable")
            return
        except Exception as exc:  # noqa: BLE001 - local dev proxy
            sys.stderr.write("[icons] %s: %s\n" % (skin_id, exc))
            self.send_error(502, "icon fetch failed")
            return
        self._send_bytes(data, "application/octet-stream")

    def _send_bytes(self, payload, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def end_headers(self):
        if not self.path.startswith(("/icons/", "/sync.json")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    os.makedirs(ROOT, exist_ok=True)
    os.makedirs(ICON_CACHE, exist_ok=True)
    port = int(os.environ.get("PORT", PORT))
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as httpd:
        httpd.daemon_threads = True
        print(f"RustGrade sync server on port {port}")
        print(f"Icon cache: {ICON_CACHE}")
        httpd.serve_forever()
