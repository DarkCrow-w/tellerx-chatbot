"""Render every fixture with the bundled LibreOffice document renderer."""

import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2] / "evaluation/generated/stress50"
RENDER = Path(
    "/Users/cliff/.codex/plugins/cache/openai-primary-runtime/documents/26.905.11957/skills/documents/render_docx.py"
)


def render(pair):
    n, spec = pair
    folder = ROOT / "qa-final" / f"{n:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    if not list(folder.glob("page-*.png")):
        with (folder / "render.log").open("w") as log:
            subprocess.run(
                [
                    sys.executable,
                    str(RENDER),
                    str(ROOT / "documents" / spec["filename"]),
                    "--output_dir",
                    str(folder),
                    "--emit_pdf",
                    "--dpi",
                    "90",
                ],
                stdout=log,
                stderr=log,
                check=True,
                timeout=240,
            )
    pages = sorted(folder.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
    # Overview sheets complement the full-size pages; not a pixel-level check.
    for start in range(0, len(pages), 12):
        batch = pages[start : start + 12]
        sheet = Image.new("RGB", (1200, 440 * ((len(batch) + 3) // 4)), "#bbbbbb")
        draw = ImageDraw.Draw(sheet)
        for k, p in enumerate(batch):
            im = Image.open(p)
            im.thumbnail((290, 410))
            x = (k % 4) * 300
            y = (k // 4) * 440
            sheet.paste(im, (x, y + 23))
            draw.text((x + 4, y + 3), f"Doc {n:02d} / {p.stem}", fill="black")
        sheet.save(folder / f"overview-{start // 12 + 1}.jpg", quality=90)
    return {"index": n, "filename": spec["filename"], "pages": len(pages), "folder": str(folder)}


if __name__ == "__main__":
    os.environ["FONTCONFIG_FILE"] = str(Path(__file__).with_name("fonts.conf"))
    manifest = json.loads((ROOT / "manifest.json").read_text())
    pairs = list(enumerate(manifest["documents"][:40], 1))
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(render, pairs):
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    (ROOT / "render-audit.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    )
