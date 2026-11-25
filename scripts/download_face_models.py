#!/usr/bin/env python3
"""Download the official OpenCV Zoo YuNet and SFace models.

This helper does not run automatically during Django startup. It intentionally
requires explicit user action so that administrators can review the target
sources, available disk space, and credentials before large model download.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from urllib import request

MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "models" / "face_recognition"
MODEL_URLS = {
    "face_detection_yunet_2023mar.onnx": "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


def sha256sum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_model(name: str, url: str) -> Path:
    destination = MODEL_DIRECTORY / name
    MODEL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"Model already exists: {destination} ({destination.stat().st_size} bytes)")
        return destination

    print(f"Downloading {name} from {url}")
    with tempfile.NamedTemporaryFile(delete=False, dir=str(MODEL_DIRECTORY), suffix=".part") as tmp_handle:
        temp_path = Path(tmp_handle.name)
        try:
            with request.urlopen(url, timeout=60) as response:
                body = response.read()
            if not body:
                raise ValueError(f"Downloaded file {name} is empty.")
            tmp_handle.write(body)
        except Exception as exc:  # pragma: no cover - exercises network path in integration env.
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Unable to download {name}: {exc}") from exc

    if temp_path.stat().st_size == 0:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Download of {name} produced an empty file.")

    shutil.move(str(temp_path), str(destination))
    print(f"Saved {destination} (sha256={sha256sum(destination)})")
    return destination


def main() -> int:
    for name, url in MODEL_URLS.items():
        try:
            path = download_model(name, url)
            print(f"OK: {path} ({path.stat().st_size} bytes)")
        except Exception as exc:  # pragma: no cover - network failure is environment-specific.
            print(f"ERROR: {name} -> {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
