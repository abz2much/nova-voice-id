"""The icon and logo the app store shows."""

import struct
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "nova_voice_id"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_icon_is_a_128_square_png():
    assert png_size(APP / "icon.png") == (128, 128)


def test_logo_exists_at_about_250_by_100():
    assert png_size(APP / "logo.png") == (250, 100)
