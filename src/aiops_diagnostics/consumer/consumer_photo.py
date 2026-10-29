"""短期图片输入，仅保留规范化后的图像于进程内存；不接受远程URL。"""

from __future__ import annotations

import io
import secrets
import threading
import time

from PIL import Image, ImageOps, UnidentifiedImageError

from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

MAX_UPLOAD = 4 * 1024 * 1024


def normalize_photo(raw: bytes, content_type: str) -> bytes:
    if content_type not in {"image/jpeg", "image/png"} or not 0 < len(raw) <= MAX_UPLOAD:
        raise ValueError("photo must be JPEG/PNG and at most 4 MiB")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            expected = "JPEG" if content_type == "image/jpeg" else "PNG"
            if image.format != expected or image.width * image.height > 12_000_000:
                raise ValueError("photo dimensions exceed 12 megapixels")
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((1600, 1600))
            clean = Image.new("RGB", image.size)
            clean.paste(image)
            output = io.BytesIO()
            clean.save(output, format="JPEG", quality=85)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("invalid photo") from exc


class PhotoStore:
    def __init__(self, *, ttl=300):
        self.ttl = ttl
        self._items = {}
        self._active = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _purge(self):
        now = time.monotonic()
        self._items = {key: item for key, item in self._items.items() if item[3] > now}

    def _sweep(self):
        while not self._stop.wait(30):
            with self._lock:
                self._purge()
                if not self._items:
                    self._thread = None
                    return

    def close(self):
        self._stop.set()
        with self._lock:
            self._items.clear()
            self._active.clear()

    def put(self, scope, binding_digest, raw):
        with self._lock:
            self._purge()
            if len(self._items) + len(self._active) >= 32 or (
                sum(i[0] == scope for i in self._items.values())
                + sum(s == scope for s in self._active.values())
                >= 2
            ):
                raise ChatRunUnavailable("PHOTO_LIMIT", "photo limit reached", http_status=429)
            key = "photo_" + secrets.token_hex(16)
            self._items[key] = scope, binding_digest, raw, time.monotonic() + self.ttl
            if self._thread is None:
                self._thread = threading.Thread(target=self._sweep, daemon=True)
                self._thread.start()
            return key

    def complete_upload(self, key, raw):
        with self._lock:
            self._purge()
            item = self._items.get(key)
            if item is None:
                raise ChatRunUnavailable("PHOTO_EXPIRED", "photo expired", http_status=410)
            self._items[key] = item[0], item[1], raw, item[3]

    def discard(self, key):
        with self._lock:
            self._items.pop(key, None)

    def take(self, key, scope, binding_digest):
        with self._lock:
            self._purge()
            item = self._items.get(key)
            if item is None or item[:2] != (scope, binding_digest) or not item[2]:
                raise ChatRunUnavailable("PHOTO_NOT_FOUND", "photo unavailable", http_status=404)
            del self._items[key]
            self._active[key] = scope
            return item[2]

    def finish(self, key):
        with self._lock:
            self._active.pop(key, None)
