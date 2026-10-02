"""Photo handling: every upload is re-encoded to a bounded JPEG so the database stays small."""
import io

from PIL import Image, ImageOps


def process_photo(raw: bytes, max_px: int = 1600, quality: int = 80):
    """Returns (jpeg_bytes, thumb_bytes, width, height). Raises ValueError if it is not an image."""
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ValueError("ไฟล์นี้ไม่ใช่รูปภาพที่เปิดได้") from e
    im.thumbnail((max_px, max_px))
    full = io.BytesIO()
    im.save(full, "JPEG", quality=quality, optimize=True)
    th = im.copy()
    th.thumbnail((360, 360))
    tb = io.BytesIO()
    th.save(tb, "JPEG", quality=72, optimize=True)
    return full.getvalue(), tb.getvalue(), im.size[0], im.size[1]


def part_thumb(raw: bytes) -> bytes:
    full, _, _, _ = process_photo(raw, max_px=320, quality=82)
    return full


def image_size(data: bytes):
    try:
        return Image.open(io.BytesIO(data)).size
    except Exception:  # noqa: BLE001
        return (0, 0)
