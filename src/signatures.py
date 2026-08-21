from __future__ import annotations

from hashlib import sha256
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_SIGNATURE_UPLOAD_BYTES = 5_000_000
MAX_SIGNATURE_PIXELS = 20_000_000
MAX_SIGNATURE_WIDTH = 1_200
MAX_SIGNATURE_HEIGHT = 400


class SignatureImageError(ValueError):
    """Raised when an uploaded signature cannot be used safely."""


def normalise_signature_image(content: bytes) -> bytes:
    """Validate, crop and rewrite a signature as a metadata-free PNG."""
    raw = bytes(content or b"")
    if not raw:
        raise SignatureImageError("Choose a PNG or JPG signature image first.")
    if len(raw) > MAX_SIGNATURE_UPLOAD_BYTES:
        raise SignatureImageError("The signature image must be smaller than 5 MB.")
    try:
        source = Image.open(BytesIO(raw))
        source.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise SignatureImageError("The uploaded file is not a valid PNG or JPG image.") from exc
    if source.width * source.height > MAX_SIGNATURE_PIXELS:
        raise SignatureImageError("The signature image is too large to process safely.")

    source = ImageOps.exif_transpose(source).convert("RGBA")
    white = Image.new("RGBA", source.size, (255, 255, 255, 255))
    white.alpha_composite(source)
    image = white.convert("RGB")
    ink_mask = ImageOps.grayscale(image).point(lambda value: 255 if value < 245 else 0)
    bounds = ink_mask.getbbox()
    if not bounds:
        raise SignatureImageError("No signature could be found in that image.")
    left, top, right, bottom = bounds
    padding = max(8, min(image.size) // 40)
    image = image.crop(
        (
            max(0, left - padding),
            max(0, top - padding),
            min(image.width, right + padding),
            min(image.height, bottom + padding),
        )
    )
    image.thumbnail(
        (MAX_SIGNATURE_WIDTH, MAX_SIGNATURE_HEIGHT),
        Image.Resampling.LANCZOS,
    )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def signature_sha256(content: bytes) -> str:
    return sha256(bytes(content)).hexdigest()
