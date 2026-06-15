from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


def make_contact_sheet(
    records: list[dict[str, Any]],
    output_path: Path,
    *,
    offsets_ms: Sequence[int],
    cell_width: int,
    cell_height: int,
    label_height: int = 28,
    jpeg_quality: int = 80,
) -> None:
    sheet = Image.new("RGB", (cell_width * len(offsets_ms), cell_height + label_height), "#20252b")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    by_offset = {item["offset_ms"]: item for item in records if not item.get("missing")}
    for index, offset in enumerate(offsets_ms):
        x = index * cell_width
        label = f"T{offset / 1000:+g}s" if offset else "T trigger"
        record = by_offset.get(offset)
        if record:
            with Image.open(output_path.parent / record["filename"]) as source:
                image = source.convert("RGB")
                fitted = ImageOps.contain(image, (cell_width - 8, cell_height - 8))
            px = x + (cell_width - fitted.width) // 2
            py = label_height + (cell_height - fitted.height) // 2
            sheet.paste(fitted, (px, py))
        else:
            draw.rectangle(
                (x + 4, label_height + 4, x + cell_width - 4, label_height + cell_height - 4),
                outline="#7d8790",
                width=2,
            )
            draw.text(
                (x + max(8, (cell_width - 42) // 2), label_height + max(8, (cell_height - 10) // 2)),
                "missing",
                fill="#c7cdd3",
                font=font,
            )
        border = "#ff3b30" if offset == 0 else "#59636d"
        draw.rectangle(
            (x + 1, label_height + 1, x + cell_width - 2, label_height + cell_height - 2),
            outline=border,
            width=4 if offset == 0 else 1,
        )
        draw.text((x + 8, 8), label, fill="white", font=font)
    sheet.save(output_path, format="JPEG", quality=jpeg_quality, optimize=True)
