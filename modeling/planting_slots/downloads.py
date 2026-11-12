"""Robust file downloads for the national batch: atomic (a `.part` file renamed
only when complete), retried with backoff and resumed with HTTP Range, so an
interrupted download of a multi-GB file is never mistaken for a finished one.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

# No personal contact here on purpose: this runs from shared VMs.
HEADERS = {"User-Agent": "ospm-pontos-brasil/0.1"}
CHUNK_BYTES = 1 << 20
RETRIES = 5


class DownloadNotFound(RuntimeError):
    """The server answered 404: the file does not exist (retrying is pointless)."""


def file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 — integrity check against the publisher's .md5, not security
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, dest: Path, *, headers: dict | None = None, timeout: float = 120.0,
                  retries: int = RETRIES, backoff_s: float = 5.0) -> Path:
    """Download `url` to `dest` (skipped if it already exists). Resumes a
    leftover `dest.part` with a Range request when the server supports it."""
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    headers = {**HEADERS, **(headers or {})}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            done = part.stat().st_size if part.exists() else 0
            request_headers = {**headers, **({"Range": f"bytes={done}-"} if done else {})}
            with httpx.stream("GET", url, headers=request_headers, timeout=timeout, follow_redirects=True) as resp:
                if resp.status_code == 416 and done:  # asked past the end: the .part is already complete
                    resp.read()
                    part.replace(dest)
                    return dest
                resp.raise_for_status()
                resumed = resp.status_code == 206
                if done and not resumed:  # server ignored Range: start over
                    done = 0
                expected = int(resp.headers["content-length"]) + done if "content-length" in resp.headers else None
                with open(part, "ab" if resumed else "wb") as fh:
                    for chunk in resp.iter_bytes():  # as they arrive: an error keeps what was received
                        fh.write(chunk)
            if expected is not None and part.stat().st_size != expected:
                raise IOError(f"download incompleto: {part.stat().st_size} de {expected} bytes")
            part.replace(dest)
            return dest
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                raise DownloadNotFound(f"{url} não existe (404)") from exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403):
                break  # retrying will not help
            print(f"  tentativa {attempt}/{retries} de {url} falhou: {exc}")
            time.sleep(backoff_s * attempt)
    raise RuntimeError(f"Falha ao baixar {url}: {last_error}")


def write_atomic(path: Path, data: bytes | str) -> None:
    """Write a small file so that readers never see it half written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    tmp.replace(path)
