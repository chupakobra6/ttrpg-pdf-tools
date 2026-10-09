from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pdf_form_editor import (  # noqa: E402
    PdfFormEditor,
    normalize_text,
)


class NormalizeTextTests(unittest.TestCase):
    def test_normalize_text_rewrites_problem_glyphs_to_pdf_safe_ascii(self) -> None:
        self.assertEqual(
            normalize_text("• “умно” — и\r\nбез сюрпризов"),
            '- "умно" - и\nбез сюрпризов',
        )


class GenericTtrpgFormTests(unittest.TestCase):
    def test_single_digit_field_keeps_large_font_and_enforces_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "digits.pdf"
            with fitz.open() as document:
                page = document.new_page()
                widget = fitz.Widget()
                widget.field_name = "score.tens"
                widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                widget.rect = fitz.Rect(20, 30, 45, 59)
                widget.text_maxlen = 1
                widget.field_value = "2"
                widget.text_fontsize = 30
                page.add_widget(widget)
                document.save(source)
            editor = PdfFormEditor(source)
            try:
                editor.set_text("score.tens", "3")
                with self.assertRaisesRegex(ValueError, "длину поля"):
                    editor.set_text("score.tens", "35")
                editor.autosize_text_fields("filled")
                editor.save()
            finally:
                editor.close()
            with fitz.open(source) as saved:
                page = saved[0]
                widget = next(page.widgets())
                self.assertEqual(widget.field_value, "3")
                self.assertEqual(widget.text_maxlen, 1)
                self.assertGreater(widget.text_fontsize, 20)
                self.assertIn("3", page.get_text())

    def test_other_system_form_roundtrips_without_dnd_skill_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "other-system.pdf"
            with fitz.open() as document:
                page = document.new_page(width=300, height=420)
                for name, kind, rect in (
                    ("Performance", fitz.PDF_WIDGET_TYPE_TEXT, fitz.Rect(20, 30, 180, 55)),
                    ("trained", fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.Rect(20, 70, 35, 85)),
                ):
                    widget = fitz.Widget()
                    widget.field_name, widget.field_type, widget.rect = name, kind, rect
                    page.add_widget(widget)
                document.save(source)
            editor = PdfFormEditor(source)
            try:
                self.assertIsNone(editor.template_profile)
                editor.set_text("Performance", "Other system")
                editor.set_checkbox("trained", True)
                editor.autosize_text_fields("filled")
                editor.save()
            finally:
                editor.close()
            saved = PdfFormEditor(source)
            try:
                self.assertIsNone(saved.template_profile)
                self.assertEqual(saved.field_value("Performance"), "Other system")
                self.assertTrue(saved.checkbox_checked("trained"))
                self.assertEqual(tuple(saved.pages[0].rect), (0, 0, 300, 420))
                self.assertEqual({field.name for field in saved.list_fields()}, {"Performance", "trained"})
                for ref in saved.widgets_by_name["Performance"]:
                    self.assertEqual(saved.doc.xref_get_key(ref.xref, "V"), ("string", "Other system"))
            finally:
                saved.close()


class LocalizedDndTemplateTests(unittest.TestCase):
    def test_list_fields_preserves_raw_skill_values_for_visual_editor(self) -> None:
        editor = PdfFormEditor(ROOT / "templates" / "dnd-5e-2014" / "DnD_5E_CharacterSheet_Form_Fillable_ru.pdf")
        self.addCleanup(editor.close)
        editor.set_text("raw:Performance", "+7")
        editor.set_text("raw:History ", "+2")

        values = {field.name: field.value for field in editor.list_fields()}
        self.assertEqual(values["Performance"], "+7")
        self.assertEqual(values["History "], "+2")

    def test_dnd5e_2014_ru_profile_maps_logical_skill_rows_and_checkboxes(self) -> None:
        editor = PdfFormEditor(ROOT / "templates" / "dnd-5e-2014" / "DnD_5E_CharacterSheet_Form_Fillable_ru.pdf")
        self.addCleanup(editor.close)

        self.assertEqual(editor.template_profile, "dnd5e_2014_ru_localized")

        editor.set_skill_values({"Performance": "+2"})
        self.assertEqual(editor.field_value("Performance"), "+2")
        self.assertEqual(editor.field_value("raw:History "), "+2")

        editor.set_skill_proficiencies({"Performance": True})
        self.assertTrue(editor.checkbox_checked("skill_prof:Performance"))
        self.assertTrue(editor.checkbox_checked("raw:Check Box 28"))

    def test_raw_prefix_bypasses_dnd5e_2014_logical_skill_remap(self) -> None:
        editor = PdfFormEditor(ROOT / "templates" / "dnd-5e-2014" / "DnD_5E_CharacterSheet_Form_Fillable_ru.pdf")
        self.addCleanup(editor.close)

        editor.set_text("raw:Performance", "+7")
        self.assertEqual(editor.field_value("raw:Performance"), "+7")
        self.assertEqual(editor.field_value("Performance"), "")

    def test_dnd2024_ru_profile_maps_logical_skill_rows_and_checkboxes(self) -> None:
        editor = PdfFormEditor(ROOT / "templates" / "dnd-5e-2024" / "DnD_2024_Character-Sheet-Fillable-RUS.pdf")
        self.addCleanup(editor.close)

        self.assertEqual(editor.template_profile, "dnd2024_ru_anonymous_fields")

        editor.set_skill_values({"Persuasion": "+4"})
        self.assertEqual(editor.field_value("Persuasion"), "+4")
        self.assertEqual(editor.field_value("raw:text_77nads"), "+4")

        editor.set_skill_proficiencies({"Persuasion": True})
        self.assertTrue(editor.checkbox_checked("skill_prof:Persuasion"))
        self.assertTrue(editor.checkbox_checked("raw:checkbox_255ltdr"))

    def test_dnd5e_2014_ru_exposes_expected_image_button_fields(self) -> None:
        editor = PdfFormEditor(ROOT / "templates" / "dnd-5e-2014" / "DnD_5E_CharacterSheet_Form_Fillable_ru.pdf")
        self.addCleanup(editor.close)

        self.assertEqual(
            editor.button_field_names(),
            ["CHARACTER IMAGE", "Faction Symbol Image"],
        )
        self.assertEqual(editor.default_image_field_name(), "CHARACTER IMAGE")

    def test_dnd2024_ru_exposes_no_image_button_fields(self) -> None:
        editor = PdfFormEditor(ROOT / "templates" / "dnd-5e-2024" / "DnD_2024_Character-Sheet-Fillable-RUS.pdf")
        self.addCleanup(editor.close)

        self.assertEqual(editor.button_field_names(), [])
        self.assertIsNone(editor.default_image_field_name())


if __name__ == "__main__":
    unittest.main()
