"""Review image-search hits for creatures that have no art.

Google captchas this server, so the slider is filled from Bing image search
using the query "pathfinder 2e {name}". Nothing is written until OK is pressed
for the image currently on screen. That downloads the original file into
MonsterImage and sets ImageUrl.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from import_demiplane_missing_creatures import (
    connect,
    download_image,
    find_monster_image_table,
    insert_monster_image,
)


BING_ASYNC = "https://www.bing.com/images/async"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    )
}
RESULT_RE = re.compile(
    r"murl&quot;:&quot;(https?://.*?)&quot;.*?turl&quot;:&quot;(https?://.*?)&quot;",
    re.S,
)
MAX_IMAGES = 12


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pick creature art from image search. Writes only on OK.")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=10, help="Sample seed. Default 10 matches the preview table.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)
    connection = connect()
    connection.autocommit = False
    cursor = connection.cursor()
    image_table = find_monster_image_table(cursor)
    if not image_table:
        raise RuntimeError("MonsterImage table was not found.")
    monsters = load_without_images(cursor, image_table)
    sample = random.sample(monsters, min(args.count, len(monsters)))
    serve_web(connection, cursor, image_table, sample)


def load_without_images(cursor, image_table: str) -> list[tuple[int, str]]:
    cursor.execute(
        f"""
        SELECT m.MonsterId, m.Name,
               COALESCE(NULLIF(LTRIM(RTRIM(m.RawText)), ''), NULLIF(LTRIM(RTRIM(m.RawMD)), ''), '')
        FROM pf2.Monster AS m
        WHERE NOT EXISTS (
            SELECT 1 FROM {image_table} AS mi WHERE mi.MonsterID = m.MonsterId
        )
        """
    )
    return [(int(row.MonsterId), str(row.Name), readable_description(row[2])) for row in cursor.fetchall()]


def readable_description(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def search_images(name: str) -> list[dict[str, str]]:
    query = f"pathfinder 2e {name}"
    response = requests.get(
        BING_ASYNC,
        params={"q": query, "first": "1", "count": "35"},
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    found = []
    seen = set()
    for original, thumb in RESULT_RE.findall(response.text):
        original = html.unescape(original)
        thumb = html.unescape(thumb)
        if original in seen:
            continue
        seen.add(original)
        found.append({"original": original, "thumb": thumb, "query": query})
        if len(found) >= MAX_IMAGES:
            break
    return found


def save_image(cursor, connection, image_table: str, monster_id: int, original: str) -> str | None:
    data = download_image(requests.Session(), original, 20 * 1024 * 1024)
    if not data:
        return "The original image could not be downloaded."
    insert_monster_image(cursor, image_table, monster_id, data)
    cursor.execute(
        """
        UPDATE pf2.Monster
        SET ImageUrl = ?,
            UpdatedAt = SYSDATETIME()
        WHERE MonsterId = ?
        """,
        original,
        monster_id,
    )
    connection.commit()
    return None


def serve_web(connection, cursor, image_table: str, monsters: list[tuple[int, str, str]]) -> None:
    lock = threading.Lock()
    cache: dict[int, list[dict[str, str]]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
                return
            if self.path.startswith("/api/creature/"):
                index = int(self.path.rsplit("/", 1)[-1])
                if index < 0 or index >= len(monsters):
                    self._send(404, "application/json", b"{}")
                    return
                monster_id, name, description = monsters[index]
                if index not in cache:
                    cache[index] = search_images(name)
                body = json.dumps({
                    "index": index,
                    "count": len(monsters),
                    "monsterId": monster_id,
                    "name": name,
                    "description": description,
                    "images": cache[index],
                }).encode()
                self._send(200, "application/json", body)
                return
            self._send(404, "text/plain", b"not found")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path != "/api/save":
                self._send(404, "text/plain", b"not found")
                return
            monster_id = int(payload["monsterId"])
            original = str(payload["original"])
            with lock:
                error = save_image(cursor, connection, image_table, monster_id, original)
            if error:
                self._send(502, "application/json", json.dumps({"error": error}).encode())
                return
            self._send(200, "application/json", b'{"ok":true}')

        def log_message(self, fmt: str, *args) -> None:
            print(fmt % args)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    print("Open http://127.0.0.1:8765")
    print("No image is saved until you press OK.")
    server.serve_forever()


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Creature image review</title>
<style>
body { font-family: sans-serif; margin: 24px; background: #1c1c1c; color: #eee; }
.layout { display: flex; gap: 24px; align-items: flex-start; }
.art { flex: 1 1 60%; }
.stat { flex: 1 1 40%; white-space: pre-wrap; max-height: 640px; overflow: auto; background: #111; padding: 12px; }
img { max-width: 100%; max-height: 520px; background: #111; }
button { font-size: 16px; margin-right: 8px; padding: 8px 14px; }
#url { color: #aaa; word-break: break-all; }
</style>
</head>
<body>
<h1 id="title">Loading...</h1>
<div class="layout">
<div class="art">
<p id="caption"></p>
<img id="picture" alt="">
<p id="url"></p>
</div>
<pre id="description" class="stat"></pre>
</div>
<p>
<button id="prev">Previous image</button>
<button id="next">Next image</button>
<button id="ok">OK, save this image</button>
<button id="skip">Skip creature</button>
</p>
<p id="status"></p>
<script>
let creature = 0;
let image = 0;
let data = null;
async function load() {
  const response = await fetch("/api/creature/" + creature);
  if (!response.ok) { document.getElementById("title").textContent = "Done"; return; }
  data = await response.json();
  image = 0;
  show();
}
function show() {
  document.getElementById("title").textContent =
    (data.index + 1) + "/" + data.count + "  " + data.name + "  (MonsterId " + data.monsterId + ")";
  document.getElementById("description").textContent = data.description || "No description stored.";
  if (!data.images.length) {
    document.getElementById("caption").textContent = "No images found.";
    document.getElementById("picture").removeAttribute("src");
    document.getElementById("url").textContent = "";
    return;
  }
  const item = data.images[image];
  document.getElementById("caption").textContent = "Image " + (image + 1) + " of " + data.images.length;
  document.getElementById("picture").src = item.thumb;
  document.getElementById("url").textContent = item.original;
}
document.getElementById("prev").onclick = () => { if (data.images.length) { image = (image + data.images.length - 1) % data.images.length; show(); } };
document.getElementById("next").onclick = () => { if (data.images.length) { image = (image + 1) % data.images.length; show(); } };
document.getElementById("skip").onclick = () => { creature += 1; load(); };
document.getElementById("ok").onclick = async () => {
  if (!data.images.length) return;
  document.getElementById("status").textContent = "Saving...";
  const response = await fetch("/api/save", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({monsterId: data.monsterId, original: data.images[image].original})
  });
  const body = await response.json();
  if (!response.ok) { document.getElementById("status").textContent = body.error || "Save failed"; return; }
  document.getElementById("status").textContent = "Saved.";
  creature += 1;
  load();
};
load();
</script>
</body>
</html>
"""




if __name__ == "__main__":
    main()
