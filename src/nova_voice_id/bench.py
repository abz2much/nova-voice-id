"""Time one embedding of a 3 second clip with each model.

    python3 -m nova_voice_id.bench [models dir]

Used by CI to report the speed on each architecture. The clip is noise: an
embedding takes the same time whatever the audio says.
"""

import sys
import time
from pathlib import Path

import numpy as np

from .speaker import MODELS, SAMPLE_RATE, Models

RUNS = 7


def main() -> None:
    models_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/src/models")
    clip = np.random.default_rng(0).standard_normal(3 * SAMPLE_RATE).astype(np.float32) * 0.1
    for model in MODELS:
        models = Models(models_dir, model)
        models.embed(clip)  # warm up
        times = []
        for _ in range(RUNS):
            started = time.perf_counter()
            models.embed(clip)
            times.append((time.perf_counter() - started) * 1000)
        vad_started = time.perf_counter()
        models.voiced(clip)
        vad_ms = (time.perf_counter() - vad_started) * 1000
        print(
            f"EMBED_3S model={model} median_ms={sorted(times)[RUNS // 2]:.0f} "
            f"max_ms={max(times):.0f} vad_ms={vad_ms:.0f}"
        )


if __name__ == "__main__":
    main()
