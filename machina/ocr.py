"""Bounded, local OCR candidate discovery. Never changes drawing geometry.

Results may contain source identities and must stay in private storage. No
network service is called. Tesseract runs without a shell in a private temporary
directory; files are removed on success, timeout, or exception.
"""
from __future__ import annotations

import csv
import io
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import pymupdf as fitz


def _recognize(png: bytes, *, timeout: float, executable: str, scale: float):
    with tempfile.TemporaryDirectory(prefix="machina-ocr-") as folder:
        path = Path(folder) / "page.png"
        with path.open("wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(png)
        result = subprocess.run([executable, str(path), "stdout", "--psm", "11", "tsv"],
                                capture_output=True, timeout=timeout, check=True)
    lines = {}
    for row in csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"):
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        confidence = float(row["conf"])
        if confidence < 0:
            continue
        key = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
        x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
        word = {"text": row["text"], "confidence": confidence,
                "bbox": [x / scale, y / scale, (x+w) / scale, (y+h) / scale]}
        lines.setdefault(key, []).append(word)
    return [{"text": " ".join(w["text"] for w in words), "words": words} for words in lines.values()]


def extract_pdf(data: bytes, *, dpi=150, max_pages=20, max_pixels=16_000_000,
                timeout=20, total_timeout=120) -> dict:
    """Return {pages:[{page,lines}], limitations}, with bboxes in PDF points.

    OCR does not identify entities by itself. Limits and failures are explicit.
    dpi is capped at 300; oversized pages are downsampled before rendering.
    """
    if not 36 <= dpi <= 300 or max_pages < 1 or max_pixels < 1000 or timeout <= 0 or total_timeout <= 0:
        raise ValueError("Invalid OCR resource limits")
    executable = shutil.which("tesseract")
    report = {"pages": [], "limitations": []}
    if executable is None:
        report["limitations"].append("tesseract_unavailable")
        return report
    deadline = time.monotonic() + total_timeout
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("Encrypted PDF requires a password")
        if len(doc) > max_pages:
            report["limitations"].append("page_limit_reached")
        for index in range(min(len(doc), max_pages)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report["limitations"].append("total_timeout_reached")
                break
            page = doc[index]
            scale = min(dpi / 72, math.sqrt(max_pixels / max(1, page.rect.width * page.rect.height)))
            # ceil dimensions may slightly exceed the ideal bound; keep margin.
            scale *= 0.999
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            try:
                lines = _recognize(pix.tobytes("png"), timeout=min(timeout, max(0.001, deadline-time.monotonic())), executable=executable, scale=scale)
                report["pages"].append({"page": index, "lines": lines})
            except subprocess.TimeoutExpired:
                report["limitations"].append("page_ocr_timeout")
            except (subprocess.CalledProcessError, UnicodeError, ValueError, KeyError):
                report["limitations"].append("page_ocr_failed")
    report["limitations"] = sorted(set(report["limitations"]))
    return report
