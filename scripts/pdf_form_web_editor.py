#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import secrets
import html
import subprocess
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy_default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock

import fitz

if __package__:
    from .pdf_form_editor import FieldInfo, FileConflict, PdfFormEditor, RAW_FIELD_PREFIX
    from .pdf_library import PdfLibrary, PROJECT_ROOT, read_snapshot
    from .pdf_library_web import render_library
else:
    from pdf_form_editor import FieldInfo, FileConflict, PdfFormEditor, RAW_FIELD_PREFIX
    from pdf_library import PdfLibrary, PROJECT_ROOT, read_snapshot
    from pdf_library_web import render_library


DEFAULT_SCALE = 1.35


@dataclass
class PageSpec:
    page_number: int
    width: float
    height: float


@dataclass
class AppState:
    pdf_path: Path | None
    picker_root: Path
    autosize_mode: str
    scale: float
    last_message: str = ""
    document_revision: int = 0
    session_id: str = field(default_factory=lambda: secrets.token_hex(16))
    lock: RLock = field(default_factory=RLock, repr=False)
    library_root: Path = PROJECT_ROOT


@dataclass
class ParsedForm:
    values: dict[str, list[str]]
    files: dict[str, bytes]
    filenames: dict[str, str] = field(default_factory=dict)

    def getfirst(self, key: str, default: str = "") -> str:
        return self.values.get(key, [default])[0]

    def has(self, key: str) -> bool:
        return key in self.values or key in self.files


def parse_form_data(handler: BaseHTTPRequestHandler) -> ParsedForm:
    content_type = handler.headers.get("Content-Type", "")
    content_length = int(handler.headers.get("Content-Length", "0"))
    payload = handler.rfile.read(content_length)

    if "multipart/form-data" not in content_type:
        parsed = urllib.parse.parse_qs(payload.decode("utf-8"), keep_blank_values=True)
        return ParsedForm(values=parsed, files={})

    message = BytesParser(policy=email_policy_default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + payload
    )
    values: dict[str, list[str]] = {}
    files: dict[str, bytes] = {}
    filenames: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        body = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files[name] = body
            filenames[name] = filename
            continue
        charset = part.get_content_charset() or "utf-8"
        values.setdefault(name, []).append(body.decode(charset, errors="replace"))
    return ParsedForm(values=values, files=files, filenames=filenames)


def html_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #efe7db;
      --panel: #fffaf2;
      --ink: #21160f;
      --muted: #6c5b4d;
      --line: #d7c5af;
      --accent: #7f3119;
      --accent-2: #564235;
      --field: rgba(255,255,255,0.82);
      --field-border: rgba(120, 80, 50, 0.28);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, #eadbc7 0, transparent 28rem),
        linear-gradient(180deg, #f8f3eb 0%, var(--bg) 100%);
      font: 14px/1.4 Georgia, "Times New Roman", serif;
    }}
    .wrap {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 20px;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 30;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 16px;
      padding: 14px 18px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255, 250, 242, 0.94);
      backdrop-filter: blur(8px);
    }}
    .title {{
      font-size: 18px;
      font-weight: 700;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      word-break: break-all;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .btn, button {{
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      color: white;
      background: var(--accent);
      cursor: pointer;
      text-decoration: none;
      font: inherit;
    }}
    .btn.secondary {{
      background: var(--accent-2);
    }}
    .file-input {{
      color: var(--muted);
      font-size: 12px;
      max-width: 220px;
    }}
    .status {{
      margin-bottom: 14px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: #fff;
    }}
    .hintbar {{
      margin-bottom: 16px;
      color: var(--muted);
      font-size: 13px;
    }}
    .picker-card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
      padding: 18px;
      box-shadow: 0 8px 30px rgba(70, 48, 29, 0.08);
    }}
    .picker-meta {{
      margin: 0 0 16px;
      color: var(--muted);
      font-size: 13px;
      word-break: break-all;
    }}
    [hidden] {{ display: none !important; }}
    .library-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(290px, 100%), 1fr)); gap: 16px; }}
    .library-card h2 {{ font-size: 18px; margin: 12px 0; }}
    .library-preview {{ width: 100%; height: 220px; object-fit: contain; background: white; }}
    .library-filters {{ display: flex; gap: 14px; flex-wrap: wrap; align-items: end; margin-bottom: 16px; }}
    .library-filters label, .library-form label {{ display: grid; gap: 6px; }}
    .library-form {{ display: grid; gap: 14px; margin-top: 16px; }}
    .library-form fieldset {{ border: 1px solid var(--line); display: grid; gap: 14px; }}
    .library-add {{ margin-bottom: 18px; }}
    select, .library-filters input, .library-form input {{ padding: 8px; max-width: 100%; }}
    summary {{ cursor: pointer; }}
    .entry-list {{
      display: grid;
      gap: 10px;
    }}
    .entry {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.72);
    }}
    .entry-name {{
      font-weight: 700;
      color: var(--ink);
    }}
    .entry-meta {{
      color: var(--muted);
      font-size: 12px;
    }}
    .pages {{
      display: grid;
      gap: 20px;
      justify-content: center;
    }}
    .page-card {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 18px;
      padding: 14px;
      box-shadow: 0 8px 30px rgba(70, 48, 29, 0.08);
    }}
    .page-title {{
      margin: 0 0 10px;
      font-size: 15px;
    }}
    .page-canvas {{
      position: relative;
      overflow: hidden;
      border-radius: 10px;
      background: white;
      box-shadow: 0 4px 18px rgba(80, 60, 40, 0.1);
    }}
    .page-image {{
      display: block;
      width: 100%;
      height: auto;
      user-select: none;
      pointer-events: none;
    }}
    .field {{
      position: absolute;
      margin: 0;
      padding: 0;
      border: 1px solid var(--field-border);
      background: rgba(255,255,255,0.58);
      color: #17120d;
      border-radius: 4px;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.32);
      font-family: Menlo, Monaco, monospace;
      line-height: 1.0;
    }}
    .field:focus {{
      outline: 2px solid rgba(127, 49, 25, 0.45);
      background: rgba(255,255,255,0.95);
      z-index: 5;
    }}
    .field.text {{
      padding: 0 2px;
      line-height: 1.08;
    }}
    .field.multiline {{
      padding: 1px 2px;
      resize: none;
      line-height: 1.14;
    }}
    .field.checkbox {{
      appearance: none;
      background: rgba(255,255,255,0.7);
    }}
    .field.checkbox::after {{
      content: "";
      display: block;
      width: 100%;
      height: 100%;
    }}
    .field.checkbox:checked {{
      background:
        linear-gradient(135deg, transparent 40%, #1b1712 40%, #1b1712 53%, transparent 53%),
        linear-gradient(45deg, transparent 58%, #1b1712 58%, #1b1712 71%, transparent 71%),
        rgba(255,255,255,0.92);
    }}
    .legend {{
      position: fixed;
      right: 16px;
      bottom: 16px;
      max-width: 320px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255, 250, 242, 0.95);
      color: var(--muted);
      font-size: 12px;
      backdrop-filter: blur(8px);
    }}
    .fields-hidden .field {{
      opacity: 0;
      pointer-events: none;
    }}
    .saving-overlay {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(25, 18, 14, 0.42);
      z-index: 60;
      padding: 20px;
    }}
    .saving-overlay.visible {{
      display: flex;
    }}
    .saving-card {{
      width: min(420px, 100%);
      border-radius: 16px;
      border: 1px solid var(--line);
      background: rgba(255, 250, 242, 0.98);
      padding: 18px;
      box-shadow: 0 16px 50px rgba(30, 21, 14, 0.25);
    }}
    .saving-title {{
      margin: 0 0 8px;
      font-size: 16px;
      font-weight: 700;
    }}
    .saving-text {{
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .progress {{
      position: relative;
      overflow: hidden;
      height: 10px;
      border-radius: 999px;
      background: #eadcc9;
    }}
    .progress::before {{
      content: "";
      position: absolute;
      inset: 0;
      width: 42%;
      border-radius: 999px;
      background: linear-gradient(90deg, #7f3119, #c06422);
      animation: progress-slide 1.2s linear infinite;
    }}
    @keyframes progress-slide {{
      0% {{ transform: translateX(-110%); }}
      100% {{ transform: translateX(260%); }}
    }}
    @media (max-width: 900px) {{
      .wrap {{ padding: 12px; }}
      .topbar {{ position: static; }}
      .legend {{ position: static; margin-top: 16px; }}
    }}
  </style>
</head>
<body>
{body}
<script>
  const formEl = document.getElementById('editor-form');
  const savingOverlay = document.getElementById('saving-overlay');
  const toggleFieldsBtn = document.getElementById('toggle-fields');
  let fieldsHidden = false;

  function setFieldsHidden(hidden) {{
    fieldsHidden = hidden;
    document.body.classList.toggle('fields-hidden', hidden);
    if (toggleFieldsBtn) {{
      toggleFieldsBtn.textContent = hidden ? 'Показать поля' : 'Скрыть поля';
    }}
  }}

  function targetIsEditable(target) {{
    if (!target) return false;
    if (target instanceof HTMLInputElement) return true;
    if (target instanceof HTMLTextAreaElement) return true;
    if (target instanceof HTMLSelectElement) return true;
    return Boolean(target.closest('[contenteditable="true"]'));
  }}

  function syncPeersByName(syncName, value, checked, source) {{
    const peers = document.querySelectorAll(`[data-sync-name="${{CSS.escape(syncName)}}"]`);
    for (const peer of peers) {{
      if (peer === source) continue;
      if (peer.type === 'checkbox') {{
        peer.checked = checked;
      }} else {{
        peer.value = value;
      }}
    }}
  }}

  for (const el of document.querySelectorAll('[data-sync-name]')) {{
    const eventName = el.type === 'checkbox' ? 'change' : 'input';
    el.addEventListener(eventName, () => {{
      const name = el.dataset.syncName;
      syncPeersByName(name, el.value, el.checked, el);
    }});
  }}

  if (formEl && savingOverlay) {{
    formEl.addEventListener('submit', () => {{
      savingOverlay.classList.add('visible');
    }});
  }}

  if (toggleFieldsBtn) {{
    toggleFieldsBtn.addEventListener('click', () => setFieldsHidden(!fieldsHidden));
  }}

  document.addEventListener('keydown', (event) => {{
    if (event.defaultPrevented) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key.toLowerCase() !== 'f') return;
    if (targetIsEditable(event.target)) return;
    event.preventDefault();
    setFieldsHidden(!fieldsHidden);
  }});
</script>
</body>
</html>""".encode("utf-8")


def get_page_specs(pdf_path: Path) -> list[PageSpec]:
    doc = fitz.open(pdf_path)
    try:
        return [
            PageSpec(page_number=page.number, width=page.rect.width, height=page.rect.height)
            for page in doc
        ]
    finally:
        doc.close()


def default_picker_root() -> Path:
    return Path(__file__).resolve().parent.parent / "templates" / "local"


def resolve_picker_dir(root: Path, raw_dir: str | None) -> Path:
    if not raw_dir:
        return root
    candidate = (root / raw_dir).resolve()
    if root == candidate or root in candidate.parents:
        return candidate
    raise ValueError(f"Directory is outside picker root: {raw_dir}")


def resolve_picker_pdf(root: Path, raw_path: str) -> Path:
    candidate = (root / raw_path).resolve()
    if not candidate.exists():
        raise FileNotFoundError(raw_path)
    if candidate.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF: {raw_path}")
    if root != candidate and root not in candidate.parents:
        raise ValueError(f"PDF is outside picker root: {raw_path}")
    return candidate


def render_picker(
    picker_root: Path,
    current_dir: Path,
    current_pdf: Path | None,
    message: str,
) -> bytes:
    try:
        relative_dir = current_dir.relative_to(picker_root)
        dir_label = "." if str(relative_dir) == "." else str(relative_dir)
    except ValueError:
        relative_dir = Path(".")
        dir_label = str(current_dir)

    directories = sorted(
        [
            entry
            for entry in current_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        ],
        key=lambda path: path.name.lower(),
    )
    pdf_files = sorted(
        [
            entry
            for entry in current_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() == ".pdf"
        ],
        key=lambda path: path.name.lower(),
    )

    up_link = ""
    if current_dir != picker_root:
        parent = current_dir.parent.relative_to(picker_root)
        up_query = urllib.parse.quote(str(parent))
        up_link = (
            f'<div class="entry"><div><div class="entry-name">..</div>'
            f'<div class="entry-meta">На уровень выше</div></div>'
            f'<a class="btn secondary" href="/choose-pdf?dir={up_query}">Открыть</a></div>'
        )

    entry_blocks: list[str] = []
    if up_link:
        entry_blocks.append(up_link)

    for directory in directories:
        rel = directory.relative_to(picker_root)
        query = urllib.parse.quote(str(rel))
        entry_blocks.append(
            f'<div class="entry"><div><div class="entry-name">{html.escape(directory.name)}/</div>'
            f'<div class="entry-meta">Папка</div></div>'
            f'<a class="btn secondary" href="/choose-pdf?dir={query}">Открыть</a></div>'
        )

    for pdf_file in pdf_files:
        rel = pdf_file.relative_to(picker_root)
        query = urllib.parse.quote(str(rel))
        is_current = current_pdf is not None and pdf_file.resolve() == current_pdf.resolve()
        meta = "Открыт сейчас" if is_current else "PDF"
        action = "Открыт" if is_current else "Выбрать"
        class_name = "btn secondary" if is_current else "btn"
        entry_blocks.append(
            f'<div class="entry"><div><div class="entry-name">{html.escape(pdf_file.name)}</div>'
            f'<div class="entry-meta">{meta}</div></div>'
            f'<a class="{class_name}" href="/switch-pdf?path={query}">{action}</a></div>'
        )

    if not entry_blocks:
        entry_blocks.append(
            '<div class="entry"><div><div class="entry-name">Нет PDF</div>'
            '<div class="entry-meta">В этой папке нет PDF-файлов.</div></div></div>'
        )

    status_html = f'<div class="status">{html.escape(message)}</div>' if message else ""
    body = f"""
    <div class="wrap">
      <div class="topbar">
        <div>
          <div class="title">Выбор PDF</div>
          <div class="meta">Текущий файл: {html.escape(str(current_pdf or "Не выбран"))}</div>
        </div>
        <div class="actions">
          <a class="btn secondary" href="/">Назад в редактор</a>
        </div>
      </div>
      {status_html}
      <section class="picker-card">
        <p class="picker-meta">Корень выбора: {html.escape(str(picker_root))}</p>
        <p class="picker-meta">Открыта папка: {html.escape(dir_label)}</p>
        <div class="entry-list">
          {''.join(entry_blocks)}
        </div>
      </section>
    </div>
    """
    return html_page("Выбор PDF", body)


def overlay_font_size(field: FieldInfo, scale: float, height: float, multiline: bool) -> float:
    pdf_size = max(4.0, field.font_size)
    scaled_pdf_size = pdf_size * scale
    if multiline:
        line_count = max(1, field.value.count("\n") + 1 if field.value.strip() else 1)
        height_budget = height * 0.72 / max(1.0, line_count)
        return max(10.5, min(16.0, max(scaled_pdf_size, height_budget)))
    height_budget = height * 0.74
    return max(11.5, min(18.0, max(scaled_pdf_size, height_budget)))


def control_html(field: FieldInfo, scale: float) -> str:
    if field.readonly or field.field_type not in ("Text", "CheckBox"):
        return ""

    left = field.x0 * scale
    top = field.y0 * scale
    width = max(8.0, (field.x1 - field.x0) * scale)
    height = max(8.0, (field.y1 - field.y0) * scale)
    title = html.escape(field.name)
    multiline = "\n" in field.value or height > 24
    font_size = overlay_font_size(field, scale, height, multiline)
    style = (
        f"left:{left:.1f}px;top:{top:.1f}px;width:{width:.1f}px;height:{height:.1f}px;"
        f"font-size:{font_size:.1f}px;"
    )
    if field.field_type == "CheckBox":
        checked = " checked" if field.checked else ""
        return (
            f'<input class="field checkbox" type="checkbox" '
            f'name="check:{title}" data-sync-name="check:{title}" title="{title}" '
            f'style="{style}" value="on"{checked}>'
        )

    value = html.escape(field.value)
    if multiline:
        return (
            f'<textarea class="field text multiline" '
            f'name="text:{title}" data-sync-name="text:{title}" title="{title}" '
            f'style="{style}">{value}</textarea>'
        )
    return (
        f'<input class="field text" type="text" '
        f'name="text:{title}" data-sync-name="text:{title}" title="{title}" '
        f'style="{style}" value="{value}">'
    )


def render_index(
    pdf_path: Path,
    picker_root: Path,
    pages: list[PageSpec],
    fields: list[FieldInfo],
    image_field_names: list[str],
    default_image_field_name: str | None,
    message: str,
    scale: float,
    document_revision: int,
    file_version: str,
    session_id: str,
) -> bytes:
    fields_by_page: dict[int, list[FieldInfo]] = {}
    for field in fields:
        fields_by_page.setdefault(field.page_number, []).append(field)

    page_blocks: list[str] = []
    for page in pages:
        page_width = page.width * scale
        page_height = page.height * scale
        controls = "".join(control_html(field, scale) for field in fields_by_page.get(page.page_number, []))
        page_blocks.append(
            f"""
            <section class="page-card">
              <h2 class="page-title">Страница {page.page_number + 1}</h2>
              <div class="page-canvas" style="width:{page_width:.1f}px;height:{page_height:.1f}px;">
                <img class="page-image" src="/page/{page.page_number}.png" alt="PDF page {page.page_number + 1}" width="{page_width:.0f}" height="{page_height:.0f}">
                {controls}
              </div>
            </section>
            """
        )

    status_html = f'<div class="status">{html.escape(message)}</div>' if message else ""
    image_controls = ""
    if image_field_names:
        options = "".join(
            (
                f'<option value="{html.escape(name)}"'
                f'{" selected" if name == default_image_field_name else ""}>'
                f"{html.escape(name)}</option>"
            )
            for name in image_field_names
        )
        image_controls = f"""
            <select name="image_field_name" title="Поле изображения">
              {options}
            </select>
            <input class="file-input" type="file" name="portrait_image" accept="image/*" title="Загрузить изображение">
        """

    body = f"""
    <div class="wrap">
      <form id="editor-form" method="post" action="/save" enctype="multipart/form-data">
        <input type="hidden" name="expected_pdf_path" value="{html.escape(str(pdf_path.resolve()))}">
        <input type="hidden" name="expected_pdf_revision" value="{document_revision}">
        <input type="hidden" name="expected_file_version" value="{file_version}">
        <input type="hidden" name="expected_session_id" value="{session_id}">
        <div class="topbar">
          <div>
            <div class="title">Редактор PDF для НРИ</div>
            <div class="meta">{html.escape(str(pdf_path))}</div>
            <div class="meta">Папка выбора PDF: {html.escape(str(picker_root))}</div>
          </div>
          <div class="actions">
            {image_controls}
            <button type="button" class="btn secondary" id="toggle-fields">Скрыть поля</button>
            <a class="btn secondary" href="/library">Библиотека НРИ</a>
            <a class="btn secondary" href="/choose-pdf">Сменить PDF</a>
            <a class="btn secondary" href="/open-pdf">Открыть PDF</a>
            <button type="submit">Сохранить PDF</button>
          </div>
        </div>
        {status_html}
        <div class="hintbar">
          Редактируй текст и флажки поверх страницы и нажми «Сохранить PDF». Изображения загружаются через панель сверху, если шаблон содержит поле изображения. Печатный фон и остальные типы полей доступны для просмотра.
        </div>
        <div class="pages">
          {''.join(page_blocks)}
        </div>
      </form>
      <div class="legend">Если одно и то же поле встречается в нескольких местах, ввод в одном месте синхронизируется с его копиями на странице.</div>
    </div>
    <div id="saving-overlay" class="saving-overlay" aria-hidden="true">
      <div class="saving-card">
        <div class="saving-title">Сохранение PDF</div>
        <div class="saving-text">Подождите. Значения полей сейчас пишутся в PDF.</div>
        <div class="progress"></div>
      </div>
    </div>
    """
    return html_page(f"Редактор PDF для НРИ - {pdf_path.name}", body)


def render_page_png(pdf_path: Path, page_number: int, scale: float) -> bytes:
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_number]
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def build_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        def _current_pdf_exists(self) -> bool:
            with state.lock:
                pdf_path = state.pdf_path
            return pdf_path is not None and pdf_path.exists() and pdf_path.is_file()

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def _missing_pdf_message(self, action: str) -> str:
            with state.lock:
                pdf_name = state.pdf_path.name if state.pdf_path else "PDF не выбран"
            return (
                f"Текущий PDF недоступен для действия «{action}»: "
                f"{pdf_name}. Выберите другой файл."
            )

        def _send_picker_for_missing_pdf(self, action: str) -> None:
            with state.lock:
                state.last_message = self._missing_pdf_message(action)
                picker_root = state.picker_root
                pdf_path = state.pdf_path
                message = state.last_message
            content = render_picker(
                picker_root,
                picker_root,
                pdf_path,
                message,
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _redirect_picker_for_missing_pdf(self, action: str) -> None:
            with state.lock:
                state.last_message = self._missing_pdf_message(action)
            self._redirect("/choose-pdf")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            print(f"[pdf-web] GET {parsed.path}")
            if parsed.path.startswith("/library"):
                self._library_get(parsed.path, parsed.query)
                return
            if parsed.path == "/":
                self._send_index()
                return
            if parsed.path == "/choose-pdf":
                self._send_picker(parsed.query)
                return
            if parsed.path == "/switch-pdf":
                self._switch_pdf(parsed.query)
                return
            if parsed.path == "/open-pdf":
                if not self._current_pdf_exists():
                    self._redirect_picker_for_missing_pdf("открытие PDF")
                    return
                with state.lock:
                    pdf_path = state.pdf_path
                subprocess.Popen(["open", "-a", "Google Chrome", str(pdf_path)])
                self._redirect("/")
                return
            if parsed.path.startswith("/page/") and parsed.path.endswith(".png"):
                self._send_page_png(parsed.path)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            print(f"[pdf-web] POST {parsed.path}")
            if parsed.path in ("/library/add", "/library/create"):
                self._library_post(parsed.path)
                return
            if parsed.path != "/save":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            form = parse_form_data(self)
            expected_pdf_path = form.getfirst("expected_pdf_path", "").strip()
            expected_revision_raw = form.getfirst("expected_pdf_revision", "").strip()
            editor = None
            try:
                with state.lock:
                    if state.pdf_path is None:
                        raise FileConflict("Выбери PDF в библиотеке перед сохранением")
                    current_pdf_path = state.pdf_path.resolve()
                    current_revision = state.document_revision
                    autosize_mode = state.autosize_mode

                    if not current_pdf_path.exists():
                        raise FileConflict(self._missing_pdf_message("сохранение"))
                    if form.getfirst("expected_session_id") != state.session_id:
                        raise FileConflict("Сервер перезапущен или форма устарела. Откройте текущий PDF заново.")
                    if not expected_pdf_path or Path(expected_pdf_path).resolve() != current_pdf_path:
                        raise FileConflict("Форма устарела после переключения PDF. Откройте нужный файл заново.")
                    if expected_revision_raw != str(current_revision):
                        raise FileConflict("Форма устарела: файл переключён или изменён в другой вкладке.")

                    editor = PdfFormEditor(current_pdf_path, profile_root=state.library_root)
                    if form.getfirst("expected_file_version") != editor.source_version:
                        raise FileConflict("PDF изменён вне этой формы. Откройте свежую версию и сравните правки.")
                    started = time.time()
                    fields = editor.list_fields()
                    text_names = sorted({field.name for field in fields if field.field_type == "Text" and not field.readonly})
                    checkbox_names = sorted({field.name for field in fields if field.field_type == "CheckBox" and not field.readonly})
                    image_field_name = form.getfirst("image_field_name") or None
                    portrait_bytes = form.files.get("portrait_image", b"")
                    print(
                        f"[pdf-web] save start file={current_pdf_path} "
                        f"text_fields={len(text_names)} checkbox_fields={len(checkbox_names)} "
                        f"portrait={'yes' if portrait_bytes else 'no'} "
                        f"image_field={image_field_name or '<auto>'}"
                    )
                    for name in text_names:
                        editor.set_text(RAW_FIELD_PREFIX + name, form.getfirst(f"text:{name}", ""))
                    for name in checkbox_names:
                        editor.set_checkbox(RAW_FIELD_PREFIX + name, form.has(f"check:{name}"))
                    if portrait_bytes:
                        editor.set_portrait_image(portrait_bytes, field_name=image_field_name)
                    editor.autosize_text_fields(autosize_mode)
                    editor.save()
                    duration = time.time() - started
                    state.document_revision += 1
                    print(f"[pdf-web] save ok duration={duration:.2f}s")
                    state.last_message = f"PDF сохранён за {duration:.2f} с."
            except Exception as exc:
                print(f"[pdf-web] save failed: {exc}")
                if not isinstance(exc, (FileConflict, FileNotFoundError)):
                    traceback.print_exc()
                with state.lock:
                    state.last_message = f"Не удалось сохранить PDF: {exc}"
                self._send_unsaved_form(form, str(exc), HTTPStatus.CONFLICT if isinstance(exc, (FileConflict, FileNotFoundError)) else HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            finally:
                if editor is not None:
                    editor.close()

            self._redirect("/")

        def log_message(self, format: str, *args) -> None:
            return

        def _send_unsaved_form(self, form: ParsedForm, reason: str, status: HTTPStatus) -> None:
            # Keep the exact submitted values and uploaded bytes in the response,
            # not in shared server state where another tab could replace them.
            draft = {"values": form.values, "files_base64": {
                name: base64.b64encode(data).decode("ascii") for name, data in form.files.items()
            }}
            encoded = base64.b64encode(json.dumps(draft, ensure_ascii=False).encode()).decode("ascii")
            values = "".join(
                f"<dt>{html.escape(name)}</dt><dd><pre>{html.escape(chr(10).join(items))}</pre></dd>"
                for name, items in form.values.items() if not name.startswith("expected_")
            )
            content = ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
                "<title>Несохранённые правки PDF</title><h1>Правки не записаны</h1>"
                f"<p>{html.escape(reason)}</p>"
                "<p>Ваш ввод сохранён ниже. Скачайте черновик с полями и изображениями, "
                "затем откройте актуальный файл в новой вкладке и перенесите нужные правки после сравнения. "
                "Не закрывайте эту страницу до переноса. Отсутствующий флажок в черновике означает снятый флажок.</p>"
                f"<p><a download='pdf-unsaved-draft.json' href='data:application/json;base64,{encoded}'>"
                "Скачать весь ввод</a> · <a href='/' target='_blank' rel='noopener'>Открыть актуальный PDF</a></p>"
                f"<dl>{values}</dl></html>").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _respond(self, content: bytes, mime: str = "text/html; charset=utf-8", status: int = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _library_get(self, path: str, query: str) -> None:
            library = PdfLibrary(state.library_root)
            params = urllib.parse.parse_qs(query)
            try:
                if path == "/library":
                    with state.lock:
                        message, session = state.last_message, state.session_id
                    body = render_library(library, params.get("system", [""])[0],
                                          params.get("kind", [""])[0], params.get("q", [""])[0], message, session)
                    self._respond(html_page("Библиотека НРИ", body))
                elif path in ("/library/pdf", "/library/preview"):
                    item = library.item(params.get("id", [""])[0])
                    snapshot = read_snapshot(library.resolve_path(item["path"]))
                    if path == "/library/pdf":
                        self._respond(snapshot.data, "application/pdf")
                    else:
                        with fitz.open(stream=snapshot.data,filetype="pdf") as doc:
                            page = doc[0]
                            scale = min(300/page.rect.width, 300/page.rect.height)
                            content = page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False).tobytes("png")
                        self._respond(content, "image/png")
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                body = '<div class="wrap"><p>'+html.escape(str(exc))+'</p><a href="/library">Вернуться в библиотеку</a></div>'
                self._respond(html_page("Ошибка библиотеки", body), status=HTTPStatus.BAD_REQUEST)

        def _library_post(self, path: str) -> None:
            form = parse_form_data(self)
            try:
                if form.getfirst("session") != state.session_id:
                    raise FileConflict("Страница библиотеки устарела. Открой её заново.")
                library = PdfLibrary(state.library_root)
                if path == "/library/add":
                    data = form.files.get("pdf", b"")
                    system = form.getfirst("system")
                    if system == "new":
                        system = form.getfirst("new_system").strip()
                    profile = json.loads(form.files["profile"]) if form.files.get("profile") else None
                    item = library.add(data, form.filenames.get("pdf", ""), system, form.getfirst("title"),
                                       form.getfirst("kind"), form.getfirst("system_name"), profile)
                    with state.lock:
                        state.last_message = "Добавлен локальный PDF: " + item["title"]
                    self._redirect("/library?system=" + urllib.parse.quote(system))
                else:
                    target = library.create_working_copy(form.getfirst("id"),form.getfirst("copy_id"))
                    with state.lock:
                        state.pdf_path = target
                        state.document_revision += 1
                        state.last_message = "Открыта рабочая копия: " + target.name
                    self._redirect("/")
            except Exception as exc:
                self._send_unsaved_form(form,str(exc),HTTPStatus.CONFLICT if isinstance(exc,FileConflict) else HTTPStatus.BAD_REQUEST)

        def _send_index(self) -> None:
            with state.lock:
                pdf_path = state.pdf_path
                if pdf_path is None:
                    self._redirect("/library")
                    return
                if not pdf_path.exists():
                    self._send_picker_for_missing_pdf("открытие редактора")
                    return
                editor = PdfFormEditor(pdf_path, profile_root=state.library_root)
                try:
                    pages = [PageSpec(i, page.rect.width, page.rect.height)
                             for i, page in enumerate(editor.doc)]
                    content = render_index(
                        pdf_path, state.picker_root, pages, editor.list_fields(),
                        editor.button_field_names(), editor.default_image_field_name(),
                        state.last_message, state.scale, state.document_revision,
                        editor.source_version, state.session_id,
                    )
                finally:
                    editor.close()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _send_picker(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            try:
                with state.lock:
                    picker_root = state.picker_root
                    pdf_path = state.pdf_path
                    last_message = state.last_message
                current_dir = resolve_picker_dir(picker_root, params.get("dir", [""])[0] or None)
                content = render_picker(
                    picker_root,
                    current_dir,
                    pdf_path,
                    last_message,
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as exc:
                with state.lock:
                    state.last_message = f"Не удалось открыть выбор PDF: {exc}"
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/")
                self.end_headers()

        def _switch_pdf(self, query: str) -> None:
            params = urllib.parse.parse_qs(query)
            raw_path = params.get("path", [""])[0]
            try:
                if not raw_path:
                    raise ValueError("PDF path is missing")
                with state.lock:
                    state.pdf_path = resolve_picker_pdf(state.picker_root, raw_path)
                    state.document_revision += 1
                    state.last_message = f"Открыт PDF: {state.pdf_path.name}"
            except Exception as exc:
                with state.lock:
                    state.last_message = f"Не удалось переключить PDF: {exc}"
            self._redirect("/")

        def _send_page_png(self, path: str) -> None:
            with state.lock:
                pdf_path = state.pdf_path
                scale = state.scale
            if pdf_path is None or not pdf_path.exists():
                body = '<div class="wrap"><p>'+html.escape(self._missing_pdf_message("рендер страницы"))+'</p></div>'
                self._respond(html_page("PDF недоступен",body),status=HTTPStatus.NOT_FOUND)
                return
            try:
                page_number = int(path.removeprefix("/page/").removesuffix(".png"))
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            pages = get_page_specs(pdf_path)
            if page_number < 0 or page_number >= len(pages):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            print(f"[pdf-web] render page png page={page_number}")
            content = render_page_png(pdf_path, page_number, scale)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a local visual editor for a fillable PDF.")
    parser.add_argument("input_pdf", nargs="?", type=Path, help="Working PDF. Without it, start in the library.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--autosize",
        choices=("none", "filled", "all"),
        default="filled",
        help="Autosize mode applied after each save. Default: filled.",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=DEFAULT_SCALE,
        help="Rendered page scale for the editor UI.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the local editor in Google Chrome after startup (macOS).",
    )
    parser.add_argument(
        "--picker-dir",
        type=Path,
        default=default_picker_root(),
        help="Root folder used by the in-app PDF picker. Default: templates/local.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    pdf_path = args.input_pdf
    if pdf_path is not None and not pdf_path.is_file():
        parser.error(f"PDF does not exist: {pdf_path}")
    picker_root = args.picker_dir.resolve()
    if not picker_root.exists():
        parser.error(f"Picker directory does not exist: {picker_root}")
    if not picker_root.is_dir():
        parser.error(f"Picker directory is not a folder: {picker_root}")

    state = AppState(
        pdf_path=pdf_path.resolve() if pdf_path else None,
        picker_root=picker_root,
        autosize_mode=args.autosize,
        scale=args.scale,
    )
    server = ThreadingHTTPServer((args.host, args.port), build_handler(state))
    url = f"http://{args.host}:{args.port}/" + ("" if pdf_path else "library")
    print(f"Serving PDF visual editor for {pdf_path}" if pdf_path else "Serving NRI PDF library")
    print(f"Open: {url}")
    if args.open_browser:
        subprocess.Popen(["open", "-a", "Google Chrome", url])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
