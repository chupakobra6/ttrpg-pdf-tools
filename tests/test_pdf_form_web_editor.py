from __future__ import annotations

import base64
import html
import json
import re
import http.client
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for candidate in (ROOT, SCRIPTS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts.pdf_form_editor import PdfFormEditor  # noqa: E402
from scripts.pdf_form_web_editor import AppState, build_handler  # noqa: E402


class PdfFormWebEditorTests(unittest.TestCase):
    def test_unsupported_list_field_is_view_only_and_preserved_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pdf_path = root / "other-system.pdf"
            with fitz.open() as document:
                page = document.new_page()
                choice = fitz.Widget()
                choice.field_name = "background"
                choice.field_type = fitz.PDF_WIDGET_TYPE_LISTBOX
                choice.choice_values = ["Scholar", "Traveler"]
                choice.field_value = "Scholar"
                choice.rect = fitz.Rect(20, 20, 150, 40)
                page.add_widget(choice)
                text = fitz.Widget()
                text.field_name = "name"
                text.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                text.rect = fitz.Rect(20, 60, 150, 80)
                page.add_widget(text)
                document.save(pdf_path)
            server, _ = self._start_server(AppState(pdf_path, root, "filled", 1.0))
            status, _, page_html = self._request(server, "GET", "/")
            self.assertEqual(status, 200)
            self.assertNotIn(b'name="text:background"', page_html)
            body, content_type = self._multipart_body(
                {**self._form_tokens(server), "text:name": "New character"}, {},
            )
            status, _, _ = self._request(server, "POST", "/save", body, {"Content-Type": content_type})
            self.assertEqual(status, 303)
            with fitz.open(pdf_path) as saved:
                values = {widget.field_name: widget.field_value for widget in saved[0].widgets()}
                self.assertEqual(values, {"background": "Scholar", "name": "New character"})

    def test_combo_selection_survives_multipart_save_and_invalid_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pdf_path = root / "armour.pdf"
            with fitz.open() as document:
                page = document.new_page()
                choice = fitz.Widget()
                choice.field_name = "armour.head"
                choice.field_type = fitz.PDF_WIDGET_TYPE_COMBOBOX
                choice.choice_values = ["Обычная", "Панцирь — тяжёлый"]
                choice.field_value = "Обычная"
                choice.rect = fitz.Rect(20, 20, 250, 45)
                page.add_widget(choice)
                document.save(pdf_path)
            server, _ = self._start_server(AppState(pdf_path, root, "filled", 1.0))
            status, _, page_html = self._request(server, "GET", "/")
            self.assertEqual(status, 200)
            self.assertIn('name="choice:armour.head"', page_html.decode())
            self.assertIn('Панцирь — тяжёлый', page_html.decode())
            for value, expected_status in [("Панцирь — тяжёлый", 303), ("Несуществующая", 500)]:
                body, content_type = self._multipart_body(
                    {**self._form_tokens(server), "choice:armour.head": value}, {},
                )
                status, _, payload = self._request(server, "POST", "/save", body, {"Content-Type": content_type})
                self.assertEqual(status, expected_status)
                with fitz.open(pdf_path) as saved:
                    page = saved[0]
                    self.assertEqual(next(page.widgets()).field_value, "Панцирь — тяжёлый")
                    self.assertIn("Панцирь", page.get_text())
                if expected_status == 500:
                    self.assertIn('Несуществующая', payload.decode())

    def test_save_keeps_localized_skill_values_in_submitted_raw_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pdf_path = root / "sheet.pdf"
            shutil.copy2(ROOT / "templates" / "dnd-5e-2014" / "DnD_5E_CharacterSheet_Form_Fillable_ru.pdf", pdf_path)
            state = AppState(pdf_path, root, "filled", 1.0)
            server, _ = self._start_server(state)
            body, content_type = self._multipart_body(
                {**self._form_tokens(server), "text:Performance": "+7", "text:History ": "+2"},
                {},
            )
            status, _, _ = self._request(server, "POST", "/save", body, {"Content-Type": content_type})
            self.assertEqual(status, 303)
            saved = PdfFormEditor(pdf_path)
            self.addCleanup(saved.close)
            self.assertEqual(saved.field_value("raw:Performance"), "+7")
            self.assertEqual(saved.field_value("raw:History "), "+2")

    def _form_tokens(self, server: ThreadingHTTPServer) -> dict[str, str]:
        status, _, payload = self._request(server, "GET", "/")
        self.assertEqual(status, 200)
        return {name: html.unescape(value) for name, value in re.findall(
            r'name="(expected_[^"]+)" value="([^"]*)"', payload.decode())}

    def _make_pdf(self, path: Path) -> None:
        doc = fitz.open()
        doc.new_page()
        doc.save(path)
        doc.close()

    def _start_server(self, state: AppState) -> tuple[ThreadingHTTPServer, threading.Thread]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)
        return server, thread

    def _request(
        self,
        server: ThreadingHTTPServer,
        method: str,
        path: str,
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def _multipart_body(
        self,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> tuple[bytes, str]:
        boundary = "----CodexBoundary7MA4YWxkTrZu0gW"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        for name, (filename, payload, content_type) in files.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    (
                        f'Content-Disposition: form-data; name="{name}"; '
                        f'filename="{filename}"\r\n'
                    ).encode("utf-8"),
                    f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                    payload,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _render_page_png(self, pdf_path: Path, page_number: int) -> bytes:
        doc = fitz.open(pdf_path)
        try:
            page = doc.load_page(page_number)
            return page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False).tobytes("png")
        finally:
            doc.close()

    def test_switch_pdf_updates_state_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            self._make_pdf(first_pdf)
            self._make_pdf(second_pdf)

            state = AppState(
                pdf_path=first_pdf.resolve(),
                picker_root=root.resolve(),
                autosize_mode="filled",
                scale=1.0,
            )
            server, _ = self._start_server(state)

            status, headers, _ = self._request(
                server,
                "GET",
                f"/switch-pdf?path={urllib.parse.quote(second_pdf.name)}",
            )

            self.assertEqual(status, 303)
            self.assertEqual(headers.get("Location"), "/")
            self.assertEqual(state.pdf_path, second_pdf.resolve())
            self.assertEqual(state.document_revision, 1)
            self.assertIn("Открыт PDF", state.last_message)

    def test_stale_save_request_is_rejected_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "sheet.pdf"
            self._make_pdf(pdf_path)

            state = AppState(
                pdf_path=pdf_path.resolve(),
                picker_root=root.resolve(),
                autosize_mode="filled",
                scale=1.0,
            )
            server, _ = self._start_server(state)

            body = urllib.parse.urlencode(
                {
                    "expected_pdf_path": str(pdf_path.resolve()),
                    "expected_pdf_revision": "999",
                }
            )
            status, headers, _ = self._request(
                server,
                "POST",
                "/save",
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            self.assertEqual(status, 409)
            self.assertNotIn("Location", headers)
            self.assertIn("форма устарела", state.last_message)
            self.assertEqual(state.document_revision, 0)

    def test_index_shows_image_selector_but_not_button_overlays_for_dnd_template(self) -> None:
        state = AppState(
            pdf_path=(ROOT / "templates" / "dnd-5e-2014" / "DnD_5E_CharacterSheet_Form_Fillable_ru.pdf").resolve(),
            picker_root=(ROOT / "templates").resolve(),
            autosize_mode="filled",
            scale=1.0,
        )
        server, _ = self._start_server(state)

        status, _, payload = self._request(server, "GET", "/")
        html = payload.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertIn('name="image_field_name"', html)
        self.assertIn(">CHARACTER IMAGE</option>", html)
        self.assertIn(">Faction Symbol Image</option>", html)
        self.assertNotIn('name="text:CHARACTER IMAGE"', html)
        self.assertNotIn('name="text:Faction Symbol Image"', html)

    def test_multipart_save_updates_text_and_rerendered_page_for_uploaded_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "sheet.pdf"
            shutil.copy2(
                ROOT / "templates" / "dnd-5e-2014" / "DnD_5E_CharacterSheet_Form_Fillable_ru.pdf",
                pdf_path,
            )

            state = AppState(
                pdf_path=pdf_path.resolve(),
                picker_root=root.resolve(),
                autosize_mode="filled",
                scale=1.0,
            )
            server, _ = self._start_server(state)

            editor = PdfFormEditor(pdf_path)
            self.addCleanup(editor.close)
            image_page_number = editor.widgets_by_name["CHARACTER IMAGE"][0].page_number

            before_png = self._render_page_png(pdf_path, image_page_number)
            image_bytes = (ROOT / "docs" / "images" / "demo-sheet-page1.png").read_bytes()
            body, content_type = self._multipart_body(
                fields={
                    **self._form_tokens(server),
                    "text:CharacterName": "Web Smoke",
                    "image_field_name": "CHARACTER IMAGE",
                },
                files={
                    "portrait_image": ("portrait.png", image_bytes, "image/png"),
                },
            )

            status, headers, _ = self._request(
                server,
                "POST",
                "/save",
                body=body,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                },
            )

            self.assertEqual(status, 303)
            self.assertEqual(headers.get("Location"), "/")
            self.assertEqual(state.document_revision, 1)
            self.assertIn("PDF сохранён", state.last_message)

            saved = PdfFormEditor(pdf_path)
            self.addCleanup(saved.close)
            self.assertEqual(saved.field_value("CharacterName"), "Web Smoke")

            after_png = self._render_page_png(pdf_path, image_page_number)
            self.assertNotEqual(before_png, after_png)

    def test_synthetic_multipart_save_preserves_values_and_renders_changed_text(self) -> None:
        from test_pdf_file_state import make_pdf
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pdf = root / "sheet.pdf"
            make_pdf(pdf)
            before = self._render_page_png(pdf, 0)
            server, _ = self._start_server(AppState(pdf, root, "filled", 1.0))
            body, content_type = self._multipart_body({
                **self._form_tokens(server), "text:Name": "HTTP manual edit", "check:Ready": "on",
            }, {})
            status, _, _ = self._request(server, "POST", "/save", body, {"Content-Type": content_type})
            self.assertEqual(status, 303)
            saved = PdfFormEditor(pdf)
            self.addCleanup(saved.close)
            self.assertEqual(saved.field_value("Name"), "HTTP manual edit")
            self.assertTrue(saved.checkbox_checked("Ready"))
            self.assertNotEqual(before, self._render_page_png(pdf, 0))

    def test_external_changes_and_restart_keep_submitted_multipart_draft(self) -> None:
        for conflict in ("external", "replace", "delete", "restart", "switch", "other_tab"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pdf = root / "sheet.pdf"
                self._make_pdf(pdf)
                state = AppState(pdf.resolve(), root.resolve(), "filled", 1.0)
                server, _ = self._start_server(state)
                tokens = self._form_tokens(server)
                if conflict == "external":
                    with fitz.open(pdf) as doc:
                        doc[0].insert_text((30, 30), "External edit")
                        doc.saveIncr()
                elif conflict == "replace":
                    other = root / "other.pdf"
                    self._make_pdf(other)
                    other.replace(pdf)
                elif conflict == "delete":
                    pdf.unlink()
                elif conflict == "restart":
                    server, _ = self._start_server(AppState(pdf.resolve(), root.resolve(), "filled", 1.0))
                elif conflict == "switch":
                    other = root / "other.pdf"
                    self._make_pdf(other)
                    self._request(server, "GET", "/switch-pdf?path=other.pdf")
                elif conflict == "other_tab":
                    first, content_type = self._multipart_body(tokens, {})
                    status, _, _ = self._request(server, "POST", "/save", first, {"Content-Type": content_type})
                    self.assertEqual(status, 303)
                before = pdf.read_bytes() if pdf.exists() else None
                draft_value = "My manual <draft> & more"
                upload = b"original uploaded bytes"
                body, content_type = self._multipart_body(
                    {**tokens, "text:Name": draft_value, "check:Ready": "on"},
                    {"portrait_image": ("portrait.png", upload, "image/png")},
                )
                status, headers, payload = self._request(server, "POST", "/save", body, {"Content-Type": content_type})
                self.assertEqual(status, 409)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(pdf.read_bytes() if pdf.exists() else None, before)
                draft = json.loads(base64.b64decode(re.search(
                    rb"data:application/json;base64,([^']+)", payload)[1]))
                self.assertEqual(draft["values"]["text:Name"], [draft_value])
                self.assertEqual(draft["values"]["check:Ready"], ["on"])
                self.assertEqual(base64.b64decode(draft["files_base64"]["portrait_image"]), upload)
                self.assertIn(html.escape(draft_value).encode(), payload)


if __name__ == "__main__":
    unittest.main()
