"""Shared image utilities."""

import base64
from pathlib import Path


def image_to_base64(image_path: Path) -> str:
    """Read an image file and return its base64-encoded string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")
