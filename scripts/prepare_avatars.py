"""Re-encode the reviewed generated originals into Telegram JPEGs.

Requires ImageMagick (`magick`). No network, secrets or image generation.
Run only when changing an original asset; bump ASSET_REVISION to force rotation.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.services.avatars import ALL_THEMES


def main():
    executable = shutil.which("magick")
    if not executable:
        raise SystemExit("Установите ImageMagick (magick)")
    root = Path(__file__).resolve().parents[1]
    original = root / "output" / "imagegen" / "avatars" / "originals"
    destination = root / "public" / "avatars"
    # Validate all input files before replacing any delivery asset.
    for theme in ALL_THEMES:
        if not (original / f"{theme.key}.png").is_file():
            raise SystemExit(f"Нет исходника: {theme.key}.png")
    destination.mkdir(parents=True, exist_ok=True)
    for theme in ALL_THEMES:
        subprocess.run(
            [
                executable,
                str(original / f"{theme.key}.png"),
                "-resize",
                "768x768",
                "-strip",
                "-sampling-factor",
                "4:4:4",
                "-quality",
                "92",
                str(destination / theme.filename),
            ],
            check=True,
        )
    subprocess.run(
        [
            executable,
            "montage",
            "-font",
            "DejaVu-Sans",
            "-pointsize",
            "12",
            "-background",
            "#EAE6DF",
            "-fill",
            "#24364F",
            "-label",
            "%t",
            *[str(destination / t.filename) for t in ALL_THEMES],
            "-thumbnail",
            "240x240",
            "-tile",
            "4x",
            "-geometry",
            "+10+12",
            "-quality",
            "90",
            str(original.parent / "preview.jpg"),
        ],
        check=True,
    )
    print(f"Подготовлено JPEG: {len(ALL_THEMES)}; public/avatars/")


if __name__ == "__main__":
    main()
