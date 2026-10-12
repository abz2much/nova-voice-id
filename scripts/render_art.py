"""Render art/icon.svg and art/logo.svg to the PNGs the app store shows.

    python scripts/render_art.py

Needs resvg-py (a dev dependency). The logo font, Nunito (SIL Open Font
License 1.1), is downloaded from the Google Fonts repository at a fixed
commit and checked against its SHA-256. Only system-independent inputs are
used, so the output doesn't depend on the fonts installed on this machine.
"""

import hashlib
import struct
import sys
import urllib.request
from pathlib import Path

import resvg_py

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "art"
APP = ROOT / "nova_voice_id"
CACHE = ROOT / ".cache"

FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/"
    "604936664fd62c14271209b51f98e7f495dd1a3e/ofl/nunito/Nunito%5Bwght%5D.ttf"
)
FONT_SHA256 = "bb55a5ca5c2042335b3991af27c4d0705d0ef41cac6164ac737fd8f2a1e85207"

# (source, output, width, height)
RENDERS = [
    ("icon.svg", APP / "icon.png", 128, 128),
    ("logo.svg", APP / "logo.png", 250, 100),
]


def font() -> Path:
    path = CACHE / "Nunito.ttf"
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != FONT_SHA256:
        CACHE.mkdir(exist_ok=True)
        data = urllib.request.urlopen(FONT_URL, timeout=60).read()
        if hashlib.sha256(data).hexdigest() != FONT_SHA256:
            sys.exit("Nunito download doesn't match its SHA-256")
        path.write_bytes(data)
    return path


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


def render(source: str, width: int, height: int, font_file: Path) -> bytes:
    return bytes(
        resvg_py.svg_to_bytes(
            svg_path=str(ART / source),
            width=width,
            height=height,
            resources_dir=str(ART),
            skip_system_fonts=True,
            font_files=[str(font_file)],
        )
    )


def main() -> None:
    font_file = font()
    for source, output, width, height in RENDERS:
        data = render(source, width, height, font_file)
        if png_size(data) != (width, height):
            sys.exit(f"{output.name} came out {png_size(data)}, not {(width, height)}")
        output.write_bytes(data)
        print(f"{output.relative_to(ROOT)}: {width}x{height}, {len(data)} bytes")


if __name__ == "__main__":
    main()
