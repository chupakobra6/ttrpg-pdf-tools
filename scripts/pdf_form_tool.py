#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from pathlib import Path

if __package__:
    from .pdf_form_editor import PdfFormEditor
else:
    from pdf_form_editor import PdfFormEditor


def run_once(input_pdf: Path, autosize_mode: str, output_pdf: Path | None = None) -> int:
    editor = PdfFormEditor(input_pdf)
    try:
        updated = editor.autosize_text_fields(autosize_mode)
        if updated > 0 or output_pdf is not None:
            editor.save(output_pdf)
        return updated
    finally:
        editor.close()


def file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def wait_until_file_stable(
    path: Path,
    poll_interval: float,
    checks: int = 2,
) -> tuple[int, int, int] | None:
    stable_reads = 0
    previous: tuple[int, int, int] | None = None
    while stable_reads < checks:
        current = file_signature(path)
        if current is None:
            stable_reads = 0
            previous = None
            time.sleep(poll_interval)
            continue
        if current == previous:
            stable_reads += 1
        else:
            stable_reads = 0
            previous = current
        time.sleep(poll_interval)
    return previous


def pdf_files_in_dir(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")


def build_output_path(input_pdf: Path, output_dir: Path | None) -> Path | None:
    if output_dir is None:
        return None
    return output_dir / input_pdf.name


def watch_file(
    input_pdf: Path,
    autosize_mode: str,
    watch_interval: float,
    output_dir: Path | None = None,
) -> int:
    last_signature = file_signature(input_pdf)
    if last_signature is None:
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")

    output_note = (
        f" Output dir: {output_dir}."
        if output_dir is not None
        else " In-place mode."
    )
    print(
        f"Watching {input_pdf} for saves. Autosize mode: {autosize_mode}. "
        f"Only font sizes will change.{output_note} Press Ctrl+C to stop."
    )
    try:
        while True:
            time.sleep(watch_interval)
            current_signature = file_signature(input_pdf)
            if current_signature is None or current_signature == last_signature:
                continue
            stable_signature = wait_until_file_stable(
                input_pdf,
                max(0.1, watch_interval / 2),
            )
            if stable_signature is None:
                continue
            try:
                output_pdf = build_output_path(input_pdf, output_dir)
                updated = run_once(input_pdf, autosize_mode, output_pdf)
                last_signature = file_signature(input_pdf)
                print(
                    f"Autosized {input_pdf.name} at {time.strftime('%H:%M:%S')} "
                    f"({updated} fields, source change {stable_signature}"
                    f"{'' if output_pdf is None else f', output {output_pdf}'})."
                )
            except Exception as exc:
                last_signature = file_signature(input_pdf)
                print(
                    f"Autosize failed for {input_pdf.name} at {time.strftime('%H:%M:%S')}: "
                    f"{exc}"
                )
    except KeyboardInterrupt:
        return 0


def watch_directory(
    directory: Path,
    autosize_mode: str,
    watch_interval: float,
    output_dir: Path | None = None,
) -> int:
    signatures = {
        path: file_signature(path)
        for path in pdf_files_in_dir(directory)
    }
    output_note = (
        f" Output dir: {output_dir}."
        if output_dir is not None
        else " In-place mode."
    )
    print(
        f"Watching {directory} for PDF saves. Autosize mode: {autosize_mode}. "
        f"Only font sizes will change.{output_note} Press Ctrl+C to stop."
    )
    try:
        while True:
            time.sleep(watch_interval)
            current_paths = pdf_files_in_dir(directory)
            current_set = set(current_paths)
            for removed_path in set(signatures) - current_set:
                signatures.pop(removed_path, None)

            for path in current_paths:
                current_signature = file_signature(path)
                previous_signature = signatures.get(path)
                if current_signature is None or previous_signature == current_signature:
                    continue
                stable_signature = wait_until_file_stable(
                    path,
                    max(0.1, watch_interval / 2),
                )
                if stable_signature is None:
                    continue
                try:
                    output_pdf = build_output_path(path, output_dir)
                    updated = run_once(path, autosize_mode, output_pdf)
                    signatures[path] = file_signature(path)
                    print(
                        f"Autosized {path.name} at {time.strftime('%H:%M:%S')} "
                        f"({updated} fields, source change {stable_signature}"
                        f"{'' if output_pdf is None else f', output {output_pdf}'})."
                    )
                except Exception as exc:
                    signatures[path] = file_signature(path)
                    print(
                        f"Autosize failed for {path.name} at {time.strftime('%H:%M:%S')}: "
                        f"{exc}"
                    )
    except KeyboardInterrupt:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Autosize fillable PDF text fields only. "
            "This tool never changes field values or checkboxes."
        )
    )
    parser.add_argument("input_pdf", nargs="?", type=Path)
    parser.add_argument(
        "--autosize",
        choices=("none", "filled", "all"),
        default="filled",
        help="How to autosize text fields. Default: filled.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch the input PDF and autosize it again after each real file save.",
    )
    parser.add_argument(
        "--watch-dir",
        type=Path,
        help="Watch PDFs in a directory and write copies to .autosized or --out-dir.",
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=0.75,
        help="Polling interval in seconds for watch mode.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="Write autosized PDFs to this directory instead of modifying the source file.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.watch_dir is not None:
        if args.input_pdf is not None or args.watch:
            parser.error("--watch-dir cannot be combined with INPUT_PDF or --watch.")
        watch_dir = args.watch_dir
        if not watch_dir.exists() or not watch_dir.is_dir():
            parser.error(f"Watch directory does not exist: {watch_dir}")
        output_dir = args.out_dir or (watch_dir / ".autosized")
        output_dir.mkdir(parents=True, exist_ok=True)
        return watch_directory(watch_dir, args.autosize, args.watch_interval, output_dir)

    if args.input_pdf is None:
        parser.error("INPUT_PDF is required unless --watch-dir is used.")

    input_pdf = args.input_pdf
    if args.watch:
        try:
            return watch_file(input_pdf, args.autosize, args.watch_interval, args.out_dir)
        except FileNotFoundError as exc:
            parser.error(str(exc))

    updated = run_once(input_pdf, args.autosize, args.out_dir / input_pdf.name if args.out_dir else None)
    if args.out_dir:
        print(f"Autosized {input_pdf.name}: {updated} fields updated. Output: {args.out_dir / input_pdf.name}")
    else:
        print(f"Autosized {input_pdf.name}: {updated} fields updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
