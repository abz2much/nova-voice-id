"""Download the models listed in nova_voice_id/models.txt and check each SHA-256.

Used by the tests and CI. The image does the same with curl and sha256sum.

    python scripts/fetch_models.py [target dir, default .models]
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "nova_voice_id" / "models.txt"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".models")
    target.mkdir(parents=True, exist_ok=True)
    for line in MANIFEST.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        expected, name, url = line.split()
        path = target / name
        if path.exists() and sha256(path) == expected:
            print(f"ok      {name}")
            continue
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        actual = sha256(tmp)
        if actual != expected:
            tmp.unlink()
            print(f"MISMATCH {name}: expected {expected}, got {actual}", file=sys.stderr)
            return 1
        tmp.replace(path)
        print(f"fetched {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
