#!/usr/bin/env python3
"""Prepare image-only iPhone/iPad PDF copies and optionally send them to Saved Messages."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import fitz

if __package__:
    from .pdf_file_state import FileConflict, file_version, publish, read_snapshot
else:
    from pdf_file_state import FileConflict, file_version, publish, read_snapshot


DELIVERY_DPI = 300
JPEG_QUALITY = 92
DELIVERY_SUFFIX = "_iPhone_iPad"
TELEGRAM_HELPER = (
    Path(__file__).resolve().parents[2]
    / "telegram-harvest"
    / "bin"
    / "telegram-harvest"
)


class DeliveryError(RuntimeError):
    """Raised when preparation or Telegram readback violates the delivery contract."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def delivery_filename(source: Path) -> str:
    stem = re.sub(r"[\s-]+", "_", source.stem).strip("_")
    return f"{stem}{DELIVERY_SUFFIX}.pdf"


def _validate_image_only_pdf(
    path: Path,
    dimensions: list[tuple[float, float, float, float]],
    embedded_images: list[bytes],
) -> None:
    with fitz.open(path) as document:
        if document.is_encrypted or document.is_form_pdf:
            raise DeliveryError(f"Delivery copy is encrypted or still fillable: {path}")
        if [tuple(page.rect) for page in document] != dimensions:
            raise DeliveryError(f"Page dimensions changed: {path}")
        for xref in range(1, document.xref_length()):
            object_type = document.xref_get_key(xref, "Type")[1]
            if object_type in ("/Font", "/ObjStm"):
                raise DeliveryError(f"Unexpected {object_type} object in {path}")
        for index, page in enumerate(document):
            if list(page.widgets() or ()) or list(page.annots() or ()):
                raise DeliveryError(f"Widgets or annotations remain on page {index + 1}: {path}")
            if page.get_fonts(full=True) or page.get_text().strip():
                raise DeliveryError(f"Selectable text or fonts remain on page {index + 1}: {path}")
            images = page.get_images(full=True)
            if len(images) != 1:
                raise DeliveryError(f"Page {index + 1} must contain exactly one image: {path}")
            xref, smask, width, height, bpc, colorspace = images[0][:6]
            expected_width = round(page.rect.width * DELIVERY_DPI / 72)
            expected_height = round(page.rect.height * DELIVERY_DPI / 72)
            if smask != 0 or bpc != 8 or colorspace != "DeviceRGB":
                raise DeliveryError(f"Page {index + 1} is not opaque 8-bit DeviceRGB: {path}")
            if abs(width - expected_width) > 1 or abs(height - expected_height) > 1:
                raise DeliveryError(f"Page {index + 1} is not {DELIVERY_DPI} DPI: {path}")
            if document.xref_stream_raw(xref) != embedded_images[index]:
                raise DeliveryError(f"Embedded page image changed on page {index + 1}: {path}")
            placements = page.get_image_rects(xref)
            if len(placements) != 1 or any(
                abs(actual - expected) >= 0.001
                for actual, expected in zip(placements[0], page.rect)
            ):
                raise DeliveryError(f"Page image does not cover page {index + 1}: {path}")


def prepare_for_apple(source: Path, output_dir: Path | None = None) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise DeliveryError(f"Input is not a PDF file: {source}")
    target_dir = (output_dir.resolve() if output_dir else source.parent / "iphone-ipad")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / delivery_filename(source)
    if target == source:
        raise DeliveryError("Delivery output must not overwrite the editable source PDF")

    snapshot = read_snapshot(source)
    source_hash = hashlib.sha256(snapshot.data).hexdigest()
    target_version = file_version(target)
    images: list[bytes] = []
    page_results: list[dict[str, Any]] = []
    temporary: Path | None = None
    try:
        with fitz.open(stream=snapshot.data, filetype="pdf") as original, fitz.open() as delivery:
            if original.is_encrypted or original.page_count == 0:
                raise DeliveryError(f"Input PDF is encrypted or empty: {source}")
            dimensions = [tuple(page.rect) for page in original]
            for index, page in enumerate(original):
                rendered = page.get_pixmap(
                    dpi=DELIVERY_DPI,
                    colorspace=fitz.csRGB,
                    alpha=False,
                    annots=True,
                )
                jpeg = rendered.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
                decoded = fitz.Pixmap(jpeg)
                if decoded.alpha or decoded.colorspace.n != 3:
                    raise DeliveryError(f"Unexpected JPEG colorspace on page {index + 1}: {source}")
                if (decoded.width, decoded.height) != (rendered.width, rendered.height):
                    raise DeliveryError(f"JPEG dimensions changed on page {index + 1}: {source}")
                sample_before = rendered.samples[::53]
                sample_after = decoded.samples[::53]
                mean_error = sum(
                    abs(before - after)
                    for before, after in zip(sample_before, sample_after)
                ) / len(sample_before)
                if mean_error >= 5:
                    raise DeliveryError(f"JPEG loss is too high on page {index + 1}: {source}")

                images.append(jpeg)
                output_page = delivery.new_page(
                    width=page.rect.width,
                    height=page.rect.height,
                )
                image_xref = output_page.insert_image(
                    output_page.rect,
                    stream=jpeg,
                    keep_proportion=False,
                )
                delivery.xref_set_key(image_xref, "ColorSpace", "/DeviceRGB")
                page_results.append(
                    {
                        "page": index + 1,
                        "pixels": [rendered.width, rendered.height],
                        "mean_jpeg_error": round(mean_error, 4),
                    }
                )

            delivery.set_metadata(
                {
                    "title": target.stem.replace("_", " "),
                    "subject": (
                        "iPhone/iPad delivery copy: image-only pages, "
                        "no forms or PDF fonts"
                    ),
                }
            )
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{target.stem}.",
                suffix=".tmp.pdf",
                dir=target_dir,
            )
            os.close(handle)
            temporary = Path(temporary_name)
            temporary.unlink()
            delivery.save(temporary, garbage=4, deflate=True, use_objstms=0)

        _validate_image_only_pdf(temporary, dimensions, images)
        output_hash = sha256(temporary)
        output_bytes = temporary.stat().st_size
        publish(temporary, target, {source: snapshot.version, target: target_version})
        temporary = None
    except FileConflict as exc:
        raise DeliveryError(str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return {
        "source": str(source),
        "output": str(target),
        "source_sha256": source_hash,
        "output_sha256": output_hash,
        "source_bytes": len(snapshot.data),
        "output_bytes": output_bytes,
        "dpi": DELIVERY_DPI,
        "jpeg_quality": JPEG_QUALITY,
        "fonts": 0,
        "forms": 0,
        "transparency": False,
        "pages": page_results,
    }


def default_caption(path: Path) -> str:
    title = path.stem.removesuffix(DELIVERY_SUFFIX).replace("_", " ")
    return (
        f"{title}. Версия для iPhone/iPad: страницы запечены в {DELIVERY_DPI} dpi, "
        "без шрифтов и форм."
    )


def send_saved(
    path: Path,
    caption: str,
    helper: Path = TELEGRAM_HELPER,
) -> dict[str, Any]:
    path = path.resolve()
    helper = helper.resolve()
    if not helper.is_file():
        raise DeliveryError(f"Telegram Harvest helper not found: {helper}")
    completed = subprocess.run(
        [
            str(helper),
            "--profile",
            "main",
            "send-saved",
            "--file",
            str(path),
            "--caption",
            caption,
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(completed.stdout)
        profile = payload["profile"]
        message = payload["message"]
        attachments = message["attachments"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise DeliveryError("Telegram helper returned invalid readback JSON") from error
    expected_attachment = {
        "file_name": path.name,
        "mime_type": "application/pdf",
        "size": path.stat().st_size,
    }
    if (
        payload.get("verified") is not True
        or payload.get("destination") != "saved_messages"
        or profile.get("username") != "Pheik13"
        or len(attachments) != 1
        or any(
            attachments[0].get(key) != value
            for key, value in expected_attachment.items()
        )
    ):
        raise DeliveryError("Telegram Saved Messages readback did not match the sent PDF")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create validated image-only iPhone/iPad copies of final PDFs. "
            "Editable sources are preserved."
        )
    )
    parser.add_argument("input_pdf", nargs="+", type=Path)
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="Output directory. Default: an iphone-ipad folder beside each input.",
    )
    parser.add_argument(
        "--send-saved",
        action="store_true",
        help=(
            "Send each prepared PDF to @Pheik13 Saved Messages through the canonical "
            "Telegram Harvest helper and verify the readback."
        ),
    )
    parser.add_argument(
        "--caption",
        help="Custom Telegram caption. Allowed only when one input PDF is supplied.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.caption and len(args.input_pdf) != 1:
        parser.error("--caption can be used only with one input PDF")

    for source in args.input_pdf:
        result = prepare_for_apple(source, args.out_dir)
        if args.send_saved:
            output = Path(result["output"])
            payload = send_saved(output, args.caption or default_caption(output))
            result["telegram"] = {
                "verified": payload["verified"],
                "message_id": payload["message"]["message_id"],
                "file_name": payload["message"]["attachments"][0]["file_name"],
                "mime_type": payload["message"]["attachments"][0]["mime_type"],
                "size": payload["message"]["attachments"][0]["size"],
            }
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
