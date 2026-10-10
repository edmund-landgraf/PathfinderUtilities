"""Review image-search hits for creatures that have no art.

Google captchas this server, so the slider is filled from Bing image search
using the query "pathfinder 2e {name}". Nothing is written until OK is pressed
for the image currently on screen. That downloads the original file into
MonsterImage. ImageUrl is this server's image route. The original
address is kept in ImageSourceUrl.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
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
MIN_IMAGE_EDGE = 400


def parse_args() -> argparse.Namespace:
    # Allow --100 as well as --count 100.
    argv = []
    for token in sys.argv[1:]:
        if re.fullmatch(r"--\d+", token):
            argv.extend(["--count", token[2:]])
        else:
            argv.append(token)
    parser = argparse.ArgumentParser(description="Pick creature art from image search. Writes only on OK.")
    parser.add_argument("--count", type=int, default=10, help="How many creatures to review. --100 is the same as --count 100.")
    parser.add_argument("--seed", type=int, default=10, help="Sample seed. Default 10 matches the preview table.")
    return parser.parse_args(argv)


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
        SELECT m.MonsterId, m.Name, m.AonUrl,
               COALESCE(NULLIF(LTRIM(RTRIM(m.RawMD)), ''), NULLIF(LTRIM(RTRIM(m.RawText)), ''), '')
        FROM pf2.Monster AS m
        WHERE NOT EXISTS (
            SELECT 1 FROM {image_table} AS mi WHERE mi.MonsterID = m.MonsterId
        )
        """
    )
    return [
        (int(row.MonsterId), str(row.Name), source_link(row.AonUrl), readable_description(row[3]))
        for row in cursor.fetchall()
    ]


def source_link(url: str | None) -> tuple[str, str]:
    text = str(url or "").strip()
    if not text.startswith("http"):
        return "", ""
    lower = text.lower()
    if "demiplane.com" in lower:
        label = "Demiplane"
    elif "aonprd.com" in lower:
        label = "Archives of Nethys"
    else:
        label = "Source"
    return label, text


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
    for match in RESULT_RE.finditer(response.text):
        original = html.unescape(match.group(1))
        thumb = html.unescape(match.group(2))
        if original in seen or "aonprd.com" in original.lower():
            continue
        window = html.unescape(response.text[max(0, match.start() - 400): match.end() + 900])
        width = first_int(window, "expw")
        height = first_int(window, "exph")
        if width is not None and height is not None and min(width, height) < MIN_IMAGE_EDGE:
            continue
        seen.add(original)
        found.append({"original": original, "thumb": thumb, "query": query, "width": width or 0, "height": height or 0})
        if len(found) >= MAX_IMAGES:
            break
    return found


def first_int(text: str, name: str) -> int | None:
    match = re.search(rf"{name}=(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def image_is_large_enough(data: bytes) -> bool:
    from PIL import Image
    import io

    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    return min(width, height) >= MIN_IMAGE_EDGE


def ensure_image_source_column(cursor) -> None:
    cursor.execute(
        """
        IF COL_LENGTH('pf2.Monster', 'ImageSourceUrl') IS NULL
            ALTER TABLE pf2.Monster ADD ImageSourceUrl NVARCHAR(MAX) NULL
        """
    )


def save_image(cursor, connection, image_table: str, monster_id: int, original: str) -> str | None:
    data = download_image(requests.Session(), original, 20 * 1024 * 1024)
    if not data:
        return "The original image could not be downloaded."
    ensure_image_source_column(cursor)
    if not image_is_large_enough(data):
        return f"Image is smaller than {MIN_IMAGE_EDGE}px on its shortest side."
    insert_monster_image(cursor, image_table, monster_id, data)
    local_url = f"https://pf2.unwhelm.online/api/monsters/{monster_id}/image"
    cursor.execute(
        """
        UPDATE pf2.Monster
        SET ImageUrl = ?,
            ImageSourceUrl = ?,
            UpdatedAt = SYSDATETIME()
        WHERE MonsterId = ?
        """,
        local_url,
        original,
        monster_id,
    )
    connection.commit()
    return None


def serve_web(connection, cursor, image_table: str, monsters: list[tuple]) -> None:
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
                monster_id, name, source, description = monsters[index]
                if index not in cache:
                    cache[index] = search_images(name)
                body = json.dumps({
                    "index": index,
                    "count": len(monsters),
                    "monsterId": monster_id,
                    "name": name,
                    "sourceLabel": source[0],
                    "sourceUrl": source[1],
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
    print("No image is saved until you click one.")
    server.serve_forever()


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Creature image review</title>
<style>
body { font-family: sans-serif; margin: 24px; background: #1c1c1c; color: #eee; }
.layout { display: flex; gap: 24px; align-items: flex-start; }
.art { flex: 1 1 62%; }
.stat { flex: 1 1 38%; white-space: pre-wrap; max-height: 80vh; overflow: auto; background: #111; padding: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.grid button { padding: 0; border: 2px solid transparent; background: #111; cursor: pointer; }
.grid button:hover { border-color: #9ecbff; }
.grid img { width: 100%; height: 180px; object-fit: contain; display: block; background: #111; }
button.skip { font-size: 16px; margin-top: 12px; padding: 8px 14px; }
a { color: #9ecbff; word-break: break-all; }
</style>
</head>
<body>
<h1 id="title">Loading...</h1>
<p id="source"></p>
<div class="layout">
<div class="art">
<p id="caption"></p>
<div id="grid" class="grid"></div>
<button id="skip" class="skip">Skip creature</button>
</div>
<pre id="description" class="stat"></pre>
</div>
<p id="status"></p>
<script>
let creature = 0;
let data = null;
async function load() {
  const response = await fetch("/api/creature/" + creature);
  if (!response.ok) { document.getElementById("title").textContent = "Done"; return; }
  data = await response.json();
  show();
}
function show() {
  document.getElementById("title").textContent =
    (data.index + 1) + "/" + data.count + "  " + data.name + "  (MonsterId " + data.monsterId + ")";
  document.getElementById("description").textContent = data.description || "No description stored.";
  const source = document.getElementById("source");
  source.textContent = "";
  if (data.sourceUrl) {
    const link = document.createElement("a");
    link.href = data.sourceUrl;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = data.sourceLabel + ": " + data.sourceUrl;
    source.appendChild(link);
  }
  const grid = document.getElementById("grid");
  grid.textContent = "";
  document.getElementById("caption").textContent = data.images.length
    ? data.images.length + " images. Click one to save it."
    : "No images found.";
  data.images.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    const img = document.createElement("img");
    img.src = item.thumb;
    img.alt = item.original;
    button.appendChild(img);
    button.onclick = () => save(item.original);
    grid.appendChild(button);
  });
}
async function save(original) {
  document.getElementById("status").textContent = "Saving...";
  const response = await fetch("/api/save", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({monsterId: data.monsterId, original})
  });
  const body = await response.json();
  if (!response.ok) { document.getElementById("status").textContent = body.error || "Save failed"; return; }
  document.getElementById("status").textContent = "Saved.";
  creature += 1;
  load();
}
document.getElementById("skip").onclick = () => { creature += 1; load(); };
load();
</script>
</body>
</html>
"""




if __name__ == "__main__":
    main()
