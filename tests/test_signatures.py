from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from src.signatures import (
    SignatureImageError,
    normalise_signature_image,
    signature_sha256,
)


def _image_bytes(*, blank: bool = False) -> bytes:
    image = Image.new("RGB", (800, 300), "white")
    if not blank:
        draw = ImageDraw.Draw(image)
        draw.line((120, 210, 270, 75, 410, 210, 680, 90), fill="black", width=12)
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def test_signature_is_cropped_rewritten_and_hashable() -> None:
    prepared = normalise_signature_image(_image_bytes())
    image = Image.open(BytesIO(prepared))

    assert prepared.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.format == "PNG"
    assert image.width < 800
    assert image.height < 300
    assert len(signature_sha256(prepared)) == 64


def test_blank_signature_is_rejected() -> None:
    with pytest.raises(SignatureImageError, match="No signature"):
        normalise_signature_image(_image_bytes(blank=True))


def test_non_image_signature_is_rejected() -> None:
    with pytest.raises(SignatureImageError, match="not a valid"):
        normalise_signature_image(b"this is not an image")
