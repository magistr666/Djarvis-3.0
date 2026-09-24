# -*- coding: utf-8 -*-
"""
Jarvis Avatar — окно оболочки на базе Edge (app mode).
HTML отдаётся локальным HTTP-сервером (надёжный рендер фото),
состояние читается из jarvis_state.json по http-запросу /state.json:
  idle      — дыхание
  speaking  — анимация головы и рук
  multitool — ядро на костюме светит ярким синим
"""
import http.server
import json
import os
import socketserver
import subprocess
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "jarvis_state.json")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)

    # На Windows mimetypes не знает .js/.glb — задаём вручную,
    # иначе браузер откажется исполнять ES-модули (MIME text/plain)
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".glb": "model/gltf-binary",
        ".json": "application/json",
        ".html": "text/html; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }

    def do_GET(self):
        if self.path.startswith("/state.json"):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = f.read()
            except Exception:
                data = '{"state":"idle","ts":0}'
            body = data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def start_server():
    handler = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    port = handler.server_address[1]
    threading.Thread(target=handler.serve_forever, daemon=True).start()
    return port


def find_edge():
    for cand in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.exists(cand):
            return cand
    return None


if __name__ == "__main__":
    port = start_server()
    # 3D-оболочка на базе Three.js; фолбэк на 2D, если 3D-файлы не на месте
    html_file = "jarvis_avatar_3d.html"
    if not os.path.exists(os.path.join(BASE, html_file)) or \
       not os.path.exists(os.path.join(BASE, "three.min.js")):
        html_file = "jarvis_avatar.html"
    url = f"http://127.0.0.1:{port}/{html_file}?t={int(time.time())}"
    edge = find_edge()
    if edge:
        subprocess.Popen(
            [edge, f"--app={url}", "--window-size=300,540", "--new-window"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        print(f"[avatar] Edge app window opened ({html_file})", flush=True)
    else:
        import webbrowser
        import urllib.parse
        webbrowser.open(url)
        print("[avatar] системный браузер", flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass