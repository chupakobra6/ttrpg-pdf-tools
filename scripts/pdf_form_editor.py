#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import fitz

if __package__:
    from .pdf_file_state import FileConflict, file_version, publish, read_snapshot
else:
    from pdf_file_state import FileConflict, file_version, publish, read_snapshot

MIN_FONT_SIZE = 4.0
MAX_FONT_SIZE = 36.0
MULTILINE_HEIGHT_THRESHOLD = 28.0
MULTILINE_MAX_FONT_SIZE = 12.0
CONSERVATIVE_MULTILINE_MAX_FONT_SIZE = 11.0
DEFAULT_DA = "0 g /Helv 10 Tf"
COMPACT_ROW_Y_TOLERANCE = 1.0
SINGLELINE_MIN_WIDTH_PADDING = 2.0
SINGLELINE_MAX_WIDTH_PADDING = 8.0
SINGLELINE_HEIGHT_PADDING = 2.0
MULTILINE_WIDTH_PADDING = 2.0
MULTILINE_HEIGHT_PADDING = 2.0
CONSERVATIVE_MULTILINE_WIDTH_PADDING = 4.0
CONSERVATIVE_MULTILINE_HEIGHT_PADDING = 4.0
SINGLELINE_HEIGHT_FACTOR = 0.92
EXPLICIT_MULTILINE_LINE_HEIGHT = 1.10
WRAPPED_MULTILINE_LINE_HEIGHT = 1.08
CONSERVATIVE_EXPLICIT_MULTILINE_LINE_HEIGHT = 1.18
CONSERVATIVE_WRAPPED_MULTILINE_LINE_HEIGHT = 1.14
FIELD_REF_PATTERN = re.compile(r"(\d+)\s+0\s+R")
DA_PATTERN = re.compile(
    r"^(?P<prefix>.*?)(?P<font>/\S+)\s+(?P<size>[0-9]+(?:\.[0-9]+)?)\s+Tf(?P<suffix>.*)$"
)
COMPACT_ROW_FONT_PATTERNS = (
    re.compile(r"^att_(str|dex|con|int|wis|cha)$"),
    re.compile(r"^def_armor_(dexorcap|prof|item)$"),
    re.compile(r"^def_armor_penalty$"),
    re.compile(r"^save_(fort|reflex|will)_(prof_calc|item)$"),
    re.compile(r"^perception_(prof_calc|item)$"),
    re.compile(r"^skill_[a-z0-9]+_(prof_calc|item)$"),
    re.compile(r"^weapon_(melee|range)[0-9]+_(strordex|prof|item)$"),
    re.compile(r"^class_dc_(key|prof|item)$"),
    re.compile(r"^spell_stat_(attack_prof_calc|dc_prof_calc|key)$"),
)
EQUAL_FONT_GROUP_PATTERNS = (
    ("save_totals", re.compile(r"^save_(fort|reflex|will)$")),
    (
        "skill_totals",
        re.compile(
            r"^skill_(acrobatics|arcana|athletics|crafting|deception|diplomacy|"
            r"intimidation|lore1|lore2|medicine|nature|occultism|performance|"
            r"religion|society|stealth|survival|thievery)$"
        ),
    ),
)
DEFAULT_IMAGE_FIELD_CANDIDATES = (
    "character_portrait_af_image",
    "CHARACTER IMAGE",
    "Faction Symbol Image",
)
UNSUPPORTED_PDF_GLYPH_MAP = str.maketrans({
    "—": "-",
    "–": "-",
    "‑": "-",
    "‒": "-",
    "−": "-",
    "•": "-",
    "·": "-",
    "●": "-",
    "«": "\"",
    "»": "\"",
    "“": "\"",
    "”": "\"",
    "„": "\"",
    "‟": "\"",
    "’": "'",
    "‘": "'",
    "‚": "'",
    "‛": "'",
})

RAW_FIELD_PREFIX = "raw:"
SKILL_PROFICIENCY_PREFIX = "skill_prof:"
DND5E_2014_RU_PROFILE = "dnd5e_2014_ru_localized"
DND2024_RU_PROFILE = "dnd2024_ru_anonymous_fields"
DND5E_2014_RU_MARKERS = (
    "Анализ",
    "Внимательность",
    "Уход за животными",
)
DND2024_RU_MARKERS = (
    "ГЕРОИЧЕСКОЕ",
    "БОЕВЫЕ ЗАГОВОРЫ",
    "Тайная магия",
)
DND5E_2014_RU_SKILL_TEXT_MAP = {
    "Acrobatics": "Acrobatics",
    "Animal": "Survival",
    "Arcana": "Medicine",
    "Athletics": "Arcana",
    "Deception": "Perception ",
    "Deception ": "Perception ",
    "History": "Intimidation",
    "History ": "Intimidation",
    "Insight": "Persuasion",
    "Intimidation": "Insight",
    "Investigation": "Animal",
    "Investigation ": "Animal",
    "Medicine": "Nature",
    "Nature": "Performance",
    "Perception": "Athletics",
    "Perception ": "Athletics",
    "Performance": "History ",
    "Persuasion": "Stealth ",
    "Religion": "Religion",
    "SleightofHand": "Investigation ",
    "Stealth": "SleightofHand",
    "Stealth ": "SleightofHand",
    "Survival": "Deception ",
}
DND5E_2014_RU_SKILL_CHECKBOX_MAP = {
    "Acrobatics": "Check Box 23",
    "Animal": "Check Box 40",
    "Arcana": "Check Box 32",
    "Athletics": "Check Box 25",
    "Deception": "Check Box 34",
    "History": "Check Box 30",
    "Insight": "Check Box 36",
    "Intimidation": "Check Box 29",
    "Investigation": "Check Box 24",
    "Medicine": "Check Box 33",
    "Nature": "Check Box 35",
    "Perception": "Check Box 26",
    "Performance": "Check Box 28",
    "Persuasion": "Check Box 39",
    "Religion": "Check Box 37",
    "SleightofHand": "Check Box 31",
    "Stealth": "Check Box 38",
    "Survival": "Check Box 27",
}
DND2024_RU_SKILL_TEXT_MAP = {
    "Acrobatics": "text_69srmm",
    "Animal": "text_67cr",
    "Arcana": "text_59mfqs",
    "Athletics": "text_61knsn",
    "Deception": "text_76vfsc",
    "History": "text_55nptn",
    "Insight": "text_66djlf",
    "Intimidation": "text_75pauh",
    "Investigation": "text_57bjob",
    "Medicine": "text_65hnhb",
    "Nature": "text_56ksru",
    "Perception": "text_63uhiv",
    "Performance": "text_74rkfi",
    "Persuasion": "text_77nads",
    "Religion": "text_58zoel",
    "SleightofHand": "text_70obrk",
    "Stealth": "text_71pflk",
    "Survival": "text_64odvk",
}
DND2024_RU_SKILL_CHECKBOX_MAP = {
    "Acrobatics": "checkbox_128cefr",
    "Animal": "checkbox_250mjvi",
    "Arcana": "checkbox_124zscb",
    "Athletics": "checkbox_126dqaq",
    "Deception": "checkbox_254ypds",
    "History": "checkbox_120drb",
    "Insight": "checkbox_249voxf",
    "Intimidation": "checkbox_253mbyq",
    "Investigation": "checkbox_122zffm",
    "Medicine": "checkbox_248scbg",
    "Nature": "checkbox_121xgrv",
    "Perception": "checkbox_246hqns",
    "Performance": "checkbox_252naxc",
    "Persuasion": "checkbox_255ltdr",
    "Religion": "checkbox_123smy",
    "SleightofHand": "checkbox_129tlov",
    "Stealth": "checkbox_130ukqx",
    "Survival": "checkbox_247lffe",
}
SKILL_TEXT_MAPS = {
    DND5E_2014_RU_PROFILE: DND5E_2014_RU_SKILL_TEXT_MAP,
    DND2024_RU_PROFILE: DND2024_RU_SKILL_TEXT_MAP,
}
SKILL_CHECKBOX_MAPS = {
    DND5E_2014_RU_PROFILE: DND5E_2014_RU_SKILL_CHECKBOX_MAP,
    DND2024_RU_PROFILE: DND2024_RU_SKILL_CHECKBOX_MAP,
}


def normalize_text(value: str | None) -> str:
    return (
        str(value or "")
        .translate(UNSUPPORTED_PDF_GLYPH_MAP)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def build_font(font_name: str | None) -> fitz.Font:
    candidates = [font_name, "Helvetica", "helv"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return fitz.Font(candidate)
        except Exception:
            continue
    return fitz.Font("Helvetica")


def wrap_paragraph(paragraph: str, font: fitz.Font, size: float, width: float) -> list[str]:
    if not paragraph:
        return [""]

    words = paragraph.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if font.text_length(candidate, size) <= width:
            current = candidate
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


def wrapped_lines(text: str, font: fitz.Font, size: float, width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in normalize_text(text).split("\n"):
        lines.extend(wrap_paragraph(paragraph, font, size, width))
    return lines or [""]


def fits_single_line(
    text: str,
    font: fitz.Font,
    size: float,
    width: float,
    height: float,
) -> bool:
    return font.text_length(text, size) <= width and size <= height * SINGLELINE_HEIGHT_FACTOR


def singleline_width_padding(rect: fitz.Rect) -> float:
    return min(SINGLELINE_MAX_WIDTH_PADDING, max(SINGLELINE_MIN_WIDTH_PADDING, rect.width * 0.08))


def fits_multiline(
    text: str,
    font: fitz.Font,
    size: float,
    width: float,
    height: float,
    *,
    explicit_line_height: float = EXPLICIT_MULTILINE_LINE_HEIGHT,
    wrapped_line_height: float = WRAPPED_MULTILINE_LINE_HEIGHT,
) -> bool:
    normalized = normalize_text(text)
    raw_lines = normalized.split("\n")
    explicit_linebreaks = "\n" in normalized

    if explicit_linebreaks:
        for raw_line in raw_lines:
            if font.text_length(raw_line, size) > width:
                return False
        line_count = max(1, len(raw_lines))
        return line_count * size * explicit_line_height <= height

    for raw_line in raw_lines:
        for word in raw_line.split():
            if font.text_length(word, size) > width:
                return False

    lines = wrapped_lines(text, font, size, width)
    if any(font.text_length(line, size) > width for line in lines):
        return False
    return len(lines) * size * wrapped_line_height <= height


def format_font_size(size: float) -> str:
    return f"{size:.1f}".rstrip("0").rstrip(".")


def build_da(existing_da: str, size: float) -> str:
    da = (existing_da or "").strip()
    match = DA_PATTERN.match(da)
    if not match:
        return DEFAULT_DA.replace("10", format_font_size(size), 1)

    parts: list[str] = []
    prefix = match.group("prefix").strip()
    suffix = match.group("suffix").strip()
    if prefix:
        parts.append(prefix)
    parts.append(match.group("font"))
    parts.append(format_font_size(size))
    parts.append("Tf")
    if suffix:
        parts.append(suffix)
    return " ".join(parts)


def image_rect_fit(container: fitz.Rect, image_width: float, image_height: float) -> fitz.Rect:
    if image_width <= 0 or image_height <= 0:
        return fitz.Rect(container)
    container_width = max(1.0, container.width)
    container_height = max(1.0, container.height)
    scale = min(container_width / image_width, container_height / image_height)
    draw_width = image_width * scale
    draw_height = image_height * scale
    x0 = container.x0 + (container_width - draw_width) / 2
    y0 = container.y0 + (container_height - draw_height) / 2
    return fitz.Rect(x0, y0, x0 + draw_width, y0 + draw_height)


def image_dimensions(image_bytes: bytes) -> tuple[float, float] | None:
    try:
        image_doc = fitz.open(stream=image_bytes)
    except Exception:
        return None
    try:
        if len(image_doc) < 1:
            return None
        rect = image_doc[0].rect
        if rect.width <= 0 or rect.height <= 0:
            return None
        return (rect.width, rect.height)
    finally:
        image_doc.close()


@dataclass
class WidgetRef:
    name: str
    widget: fitz.Widget
    xref: int
    field_type: str
    page_number: int
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class FieldInfo:
    name: str
    field_type: str
    value: str
    checked: bool
    font_size: float
    page_number: int
    x0: float
    y0: float
    x1: float
    y1: float


class PdfFormEditor:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        snapshot = read_snapshot(self.path)
        self.source_version = snapshot.version
        self.doc = fitz.open(stream=snapshot.data, filetype="pdf")
        self.pages = [self.doc.load_page(i) for i in range(len(self.doc))]
        self.widgets_by_name: dict[str, list[WidgetRef]] = defaultdict(list)
        self.field_xrefs_by_name: dict[str, list[int]] = defaultdict(list)
        self._index_fields()
        self.template_profile = self._detect_template_profile()
        self._set_need_appearances()

    def close(self) -> None:
        self.pages.clear()
        self.doc.close()

    def save(self, output_path: Path | str | None = None) -> None:
        target_path = Path(output_path).resolve() if output_path is not None else self.path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        expected = {self.path: self.source_version}
        if target_path != self.path:
            expected[target_path] = file_version(target_path)
        self.sync_structural_fields_from_widgets()
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".tmp_edit_{target_path.stem}_", suffix=".pdf", dir=target_path.parent,
        )
        os.close(temp_fd)
        temp_path = Path(temp_name)
        try:
            self.doc.save(temp_path, garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            with fitz.open(temp_path) as saved:
                for page in saved:
                    list(page.widgets() or ())
            # The in-memory document remains available to the caller after conflict.
            published_version = publish(temp_path, target_path, expected)
            if target_path == self.path:
                self.source_version = published_version
        finally:
            temp_path.unlink(missing_ok=True)

    def _index_fields(self) -> None:
        self.widgets_by_name.clear()
        self.field_xrefs_by_name.clear()

        for page in self.pages:
            widget = page.first_widget
            while widget:
                name = widget.field_name or ""
                if name:
                    self.widgets_by_name[name].append(
                        WidgetRef(
                            name=name,
                            widget=widget,
                            xref=widget.xref,
                            field_type=widget.field_type_string,
                            page_number=page.number,
                            x0=widget.rect.x0,
                            y0=widget.rect.y0,
                            x1=widget.rect.x1,
                            y1=widget.rect.y1,
                        )
                    )
                widget = widget.next

        acro_xref = self._acroform_xref()
        if acro_xref is None:
            return
        fields_type, fields_value = self.doc.xref_get_key(acro_xref, "Fields")
        if fields_type != "array":
            return

        for match in FIELD_REF_PATTERN.findall(fields_value):
            xref = int(match)
            name_type, name_value = self.doc.xref_get_key(xref, "T")
            if name_type != "string" or not name_value:
                continue
            self.field_xrefs_by_name[name_value].append(xref)

    def _acroform_xref(self) -> int | None:
        catalog_xref = self.doc.pdf_catalog()
        acro_type, acro_value = self.doc.xref_get_key(catalog_xref, "AcroForm")
        if acro_type != "xref":
            return None
        return int(acro_value.split()[0])

    def _set_need_appearances(self) -> None:
        acro_xref = self._acroform_xref()
        if acro_xref is None:
            return
        self.doc.xref_set_key(acro_xref, "NeedAppearances", "true")

    def _detect_template_profile(self) -> str | None:
        if not self.pages:
            return None
        first_page_text = self.pages[0].get_text("text")
        if {"Acrobatics", "Performance", "Persuasion", "Stealth "}.issubset(
            self.widgets_by_name
        ) and all(marker in first_page_text for marker in DND5E_2014_RU_MARKERS):
            return DND5E_2014_RU_PROFILE
        if {"text_59mfqs", "text_77nads", "checkbox_255ltdr"}.issubset(
            self.widgets_by_name
        ) and all(marker in first_page_text for marker in DND2024_RU_MARKERS):
            return DND2024_RU_PROFILE
        return None

    def _raw_field_name(self, field_name: str) -> str:
        if field_name.startswith(RAW_FIELD_PREFIX):
            return field_name[len(RAW_FIELD_PREFIX):]
        return field_name

    def _normalize_skill_name(self, skill_name: str) -> str:
        return skill_name.rstrip()

    def _resolve_text_field_name(self, field_name: str) -> str:
        raw_field_name = self._raw_field_name(field_name)
        if field_name.startswith(RAW_FIELD_PREFIX):
            return raw_field_name
        skill_map = SKILL_TEXT_MAPS.get(self.template_profile or "")
        if skill_map:
            mapped = skill_map.get(raw_field_name)
            if mapped:
                return mapped
        return raw_field_name

    def _resolve_checkbox_field_name(self, field_name: str) -> str:
        raw_field_name = self._raw_field_name(field_name)
        if field_name.startswith(RAW_FIELD_PREFIX):
            return raw_field_name
        if raw_field_name.startswith(SKILL_PROFICIENCY_PREFIX):
            skill_name = raw_field_name[len(SKILL_PROFICIENCY_PREFIX):]
            checkbox_map = SKILL_CHECKBOX_MAPS.get(self.template_profile or "")
            mapped = None
            if checkbox_map:
                mapped = checkbox_map.get(self._normalize_skill_name(skill_name))
            if mapped:
                return mapped
        return raw_field_name

    def _all_xrefs(self, field_name: str) -> list[int]:
        xrefs = {ref.xref for ref in self.widgets_by_name.get(field_name, [])}
        xrefs.update(self.field_xrefs_by_name.get(field_name, []))
        return sorted(xrefs)

    def _editable_xrefs_for_widget(self, ref: WidgetRef) -> list[int]:
        widget_xrefs = {widget_ref.xref for widget_ref in self.widgets_by_name.get(ref.name, [])}
        if len(widget_xrefs) > 1:
            return [ref.xref]
        return self._all_xrefs(ref.name)

    def _pdf_text_literal(self, value: str) -> str:
        if value == "":
            return "()"
        return fitz.get_pdf_str(value)

    def _set_text_xref(self, xref: int, value: str) -> None:
        literal = self._pdf_text_literal(value)
        self.doc.xref_set_key(xref, "V", literal)
        self.doc.xref_set_key(xref, "DV", literal)

    def _set_checkbox_xref(self, xref: int, state: str) -> None:
        target = "/Off" if state == "Off" else f"/{state.lstrip('/')}"
        self.doc.xref_set_key(xref, "V", target)
        self.doc.xref_set_key(xref, "AS", target)
        self.doc.xref_set_key(xref, "DV", target)

    def sync_structural_fields_from_widgets(self) -> int:
        self._set_need_appearances()
        updated = 0
        for field_name, refs in self.widgets_by_name.items():
            widget_ref = refs[0]
            if widget_ref.field_type == "Text":
                value = normalize_text(widget_ref.widget.field_value)
                for xref in self.field_xrefs_by_name.get(field_name, []):
                    self._set_text_xref(xref, value)
                    updated += 1
            elif widget_ref.field_type == "CheckBox":
                value = str(widget_ref.widget.field_value or "")
                state = value if value not in ("", "Off") else "Off"
                for xref in self.field_xrefs_by_name.get(field_name, []):
                    self._set_checkbox_xref(xref, state)
                    updated += 1
        return updated

    def checkbox_on_state(self, field_name: str) -> str | None:
        for ref in self.widgets_by_name.get(field_name, []):
            if ref.field_type == "CheckBox":
                return ref.widget.on_state()

        for xref in self._all_xrefs(field_name):
            ap_type, ap_value = self.doc.xref_get_key(xref, "AP")
            if ap_type != "xref":
                continue
            ap_xref = int(ap_value.split()[0])
            ap_text = self.doc.xref_object(ap_xref, compressed=False)
            matches = re.findall(r"/([A-Za-z0-9_]+)\s+\d+\s+0\s+R", ap_text)
            for match in matches:
                if match != "Off":
                    return match
        return None

    def _should_use_conservative_multiline_layout(
        self,
        font: fitz.Font,
        text: str,
        width: float,
        height: float,
    ) -> bool:
        normalized = normalize_text(text)
        word_count = len([word for word in normalized.replace("\n", " ").split(" ") if word])
        if "\n" in normalized:
            return True
        if len(normalized) >= 48 or word_count >= 7:
            return True
        nominal_size = min(11.0, MULTILINE_MAX_FONT_SIZE)
        return not fits_multiline(normalized, font, nominal_size, width, height)

    def set_text(self, field_name: str, value: str) -> None:
        field_name = self._resolve_text_field_name(field_name)
        text = normalize_text(value)
        for ref in self.widgets_by_name.get(field_name, []):
            if ref.field_type == "Text":
                ref.widget.field_value = text
                ref.widget.update()

        for xref in self._all_xrefs(field_name):
            self._set_text_xref(xref, text)

    def set_checkbox(self, field_name: str, checked: bool) -> None:
        field_name = self._resolve_checkbox_field_name(field_name)
        on_state = self.checkbox_on_state(field_name)
        target = on_state if checked and on_state else "Off"

        for ref in self.widgets_by_name.get(field_name, []):
            if ref.field_type == "CheckBox":
                ref.widget.field_value = target
                ref.widget.update()

        for xref in self._all_xrefs(field_name):
            self._set_checkbox_xref(xref, target)

    def set_text_values(self, values: dict[str, str]) -> None:
        for field_name, value in values.items():
            self.set_text(field_name, value)

    def set_checkbox_values(self, values: dict[str, bool]) -> None:
        for field_name, checked in values.items():
            self.set_checkbox(field_name, checked)

    def set_skill_values(self, values: dict[str, str]) -> None:
        for skill_name, value in values.items():
            self.set_text(skill_name, value)

    def set_skill_proficiencies(self, values: dict[str, bool]) -> None:
        for skill_name, checked in values.items():
            self.set_checkbox(f"{SKILL_PROFICIENCY_PREFIX}{skill_name}", checked)

    def button_field_names(self) -> list[str]:
        return sorted(
            {
                field_name
                for field_name, refs in self.widgets_by_name.items()
                if any(ref.field_type == "Button" for ref in refs)
            }
        )

    def default_image_field_name(self) -> str | None:
        button_names = self.button_field_names()
        if not button_names:
            return None
        for candidate in DEFAULT_IMAGE_FIELD_CANDIDATES:
            if candidate in button_names:
                return candidate
        return button_names[0]

    def set_portrait_image(
        self,
        image_bytes: bytes,
        field_name: str | None = None,
        inset: float = 2.0,
    ) -> None:
        target_field_name = field_name or self.default_image_field_name()
        if not target_field_name:
            raise KeyError("No image button field found in PDF form")

        refs = self.widgets_by_name.get(target_field_name, [])
        if not refs:
            available = ", ".join(self.button_field_names()) or "<none>"
            raise KeyError(
                f"Image field not found: {target_field_name}. Available button fields: {available}"
            )

        ref = refs[0]
        page = self.pages[ref.page_number]
        rect = fitz.Rect(ref.x0, ref.y0, ref.x1, ref.y1)
        target = fitz.Rect(
            rect.x0 + inset,
            rect.y0 + inset,
            rect.x1 - inset,
            rect.y1 - inset,
        )
        draw_rect = fitz.Rect(target)
        dims = image_dimensions(image_bytes)
        if dims is not None:
            draw_rect = image_rect_fit(target, dims[0], dims[1])
        page.draw_rect(target, color=(1, 1, 1), fill=(1, 1, 1), overlay=True)
        page.insert_image(draw_rect, stream=image_bytes, keep_proportion=False, overlay=True)
        for widget_ref in refs:
            widget = widget_ref.widget
            widget.border_color = None
            widget.fill_color = None
            try:
                widget.border_width = 0
            except Exception:
                pass
            try:
                widget.button_caption = ""
            except Exception:
                pass
            try:
                widget.update()
            except Exception:
                pass
            self.doc.xref_set_key(widget_ref.xref, "Border", "[0 0 0]")

        mk_type, mk_value = self.doc.xref_get_key(ref.xref, "MK")
        if mk_type == "xref":
            mk_xref = int(mk_value.split()[0])
            self.doc.xref_set_key(mk_xref, "BG", "[]")
            self.doc.xref_set_key(mk_xref, "BC", "[]")
        for widget_ref in refs:
            try:
                widget_ref.widget.update()
            except Exception:
                pass

    def compute_font_size(
        self,
        widget: fitz.Widget,
        text: str,
        field_name: str | None = None,
    ) -> float:
        rect = widget.rect
        font = build_font(getattr(widget, "text_font", None))
        normalized = normalize_text(text)
        multiline = "\n" in normalized or rect.height >= MULTILINE_HEIGHT_THRESHOLD
        if multiline:
            conservative = self._should_use_conservative_multiline_layout(
                font,
                normalized,
                max(1.0, rect.width - MULTILINE_WIDTH_PADDING),
                max(1.0, rect.height - MULTILINE_HEIGHT_PADDING),
            )
            width_padding = (
                CONSERVATIVE_MULTILINE_WIDTH_PADDING
                if conservative
                else MULTILINE_WIDTH_PADDING
            )
            height_padding = (
                CONSERVATIVE_MULTILINE_HEIGHT_PADDING
                if conservative
                else MULTILINE_HEIGHT_PADDING
            )
            width = max(1.0, rect.width - width_padding)
            height = max(1.0, rect.height - height_padding)
        else:
            conservative = False
            width = max(1.0, rect.width - singleline_width_padding(rect))
            height = max(1.0, rect.height - SINGLELINE_HEIGHT_PADDING)

        lower = MIN_FONT_SIZE
        upper = min(MAX_FONT_SIZE, max(lower, height))
        if multiline:
            upper = min(
                upper,
                CONSERVATIVE_MULTILINE_MAX_FONT_SIZE
                if conservative
                else MULTILINE_MAX_FONT_SIZE,
            )

        def fits(size: float) -> bool:
            if multiline:
                return fits_multiline(
                    text,
                    font,
                    size,
                    width,
                    height,
                    explicit_line_height=(
                        CONSERVATIVE_EXPLICIT_MULTILINE_LINE_HEIGHT
                        if conservative
                        else EXPLICIT_MULTILINE_LINE_HEIGHT
                    ),
                    wrapped_line_height=(
                        CONSERVATIVE_WRAPPED_MULTILINE_LINE_HEIGHT
                        if conservative
                        else WRAPPED_MULTILINE_LINE_HEIGHT
                    ),
                )
            return fits_single_line(text, font, size, width, height)

        if not fits(lower):
            return lower

        for _ in range(18):
            middle = (lower + upper) / 2
            if fits(middle):
                lower = middle
            else:
                upper = middle
        return round(lower, 1)

    def autosize_text_fields(self, mode: str = "filled") -> int:
        if mode == "none":
            return 0

        updated = 0
        for field_name, refs in self.widgets_by_name.items():
            for text_ref in (ref for ref in refs if ref.field_type == "Text"):
                text = normalize_text(text_ref.widget.field_value)
                has_value = bool(text.strip())
                if mode == "filled" and not has_value:
                    continue
                if not text:
                    continue

                target_size = self.compute_font_size(text_ref.widget, text, field_name)
                updated += self._apply_text_widget_font_size(text_ref, target_size)
        updated += self.normalize_compact_row_fonts()
        updated += self.normalize_equal_font_groups()
        return updated

    def _compact_row_sample_text(self, ref: WidgetRef) -> str:
        value = normalize_text(ref.widget.field_value).strip()
        if value:
            return value
        return "11"

    def normalize_compact_row_fonts(self) -> int:
        refs_by_page: dict[int, list[tuple[float, WidgetRef]]] = defaultdict(list)
        for field_name, refs in self.widgets_by_name.items():
            if not any(pattern.match(field_name) for pattern in COMPACT_ROW_FONT_PATTERNS):
                continue
            for text_ref in (ref for ref in refs if ref.field_type == "Text"):
                page_number = text_ref.page_number
                center_y = (text_ref.y0 + text_ref.y1) / 2
                refs_by_page[page_number].append((center_y, text_ref))

        updated = 0
        for page_refs in refs_by_page.values():
            clusters: list[tuple[float, list[WidgetRef]]] = []
            for center_y, ref in sorted(page_refs, key=lambda item: item[0]):
                if not clusters or abs(center_y - clusters[-1][0]) > COMPACT_ROW_Y_TOLERANCE:
                    clusters.append((center_y, [ref]))
                    continue
                prev_center, cluster_refs = clusters[-1]
                cluster_refs.append(ref)
                new_center = (prev_center * (len(cluster_refs) - 1) + center_y) / len(cluster_refs)
                clusters[-1] = (new_center, cluster_refs)

            for _, refs in clusters:
                if len(refs) < 2:
                    continue
                target_size = min(
                    self.compute_font_size(ref.widget, self._compact_row_sample_text(ref))
                    for ref in refs
                )
                for ref in refs:
                    updated += self._apply_text_widget_font_size(ref, target_size)
        return updated

    def normalize_equal_font_groups(self) -> int:
        grouped_refs: dict[tuple[int, str], list[WidgetRef]] = defaultdict(list)
        for field_name, refs in self.widgets_by_name.items():
            text_ref = next((ref for ref in refs if ref.field_type == "Text"), None)
            if text_ref is None:
                continue
            for group_name, pattern in EQUAL_FONT_GROUP_PATTERNS:
                if pattern.match(field_name):
                    grouped_refs[(text_ref.page_number, group_name)].append(text_ref)
                    break

        updated = 0
        for refs in grouped_refs.values():
            if len(refs) < 2:
                continue
            target_size = min(
                self.compute_font_size(ref.widget, self._compact_row_sample_text(ref))
                for ref in refs
            )
            for ref in refs:
                updated += self._apply_text_widget_font_size(ref, target_size)
        return updated

    def _apply_text_widget_font_size(self, ref: WidgetRef, size: float) -> int:
        widget = ref.widget
        current_size = float(getattr(widget, "text_fontsize", 0.0) or 0.0)
        if abs(current_size - size) < 0.05:
            return 0

        widget.text_fontsize = size
        widget.update()

        updated = 1
        target_da = build_da(
            self.doc.xref_get_key(ref.xref, "DA")[1]
            if self.doc.xref_get_key(ref.xref, "DA")[0] == "string"
            else "",
            size,
        )
        for xref in self.field_xrefs_by_name.get(ref.name, []):
            if xref == ref.xref:
                continue
            da_type, da_value = self.doc.xref_get_key(xref, "DA")
            current_da = da_value if da_type == "string" else ""
            if current_da == target_da:
                continue
            self.doc.xref_set_key(xref, "DA", f"({target_da})")
            updated += 1
        return updated

    def field_value(self, field_name: str) -> str:
        field_name = self._resolve_text_field_name(field_name)
        for ref in self.widgets_by_name.get(field_name, []):
            if ref.field_type == "Text":
                return normalize_text(ref.widget.field_value)
        return ""

    def checkbox_checked(self, field_name: str) -> bool:
        field_name = self._resolve_checkbox_field_name(field_name)
        for ref in self.widgets_by_name.get(field_name, []):
            if ref.field_type == "CheckBox":
                value = str(ref.widget.field_value or "")
                return value not in ("", "Off")
        return False

    def list_fields(self) -> list[FieldInfo]:
        fields: list[FieldInfo] = []
        for field_name, refs in self.widgets_by_name.items():
            for ref in refs:
                fields.append(
                    FieldInfo(
                        name=field_name,
                        field_type=ref.field_type,
                        value=self.field_value(RAW_FIELD_PREFIX + field_name),
                        checked=self.checkbox_checked(RAW_FIELD_PREFIX + field_name),
                        font_size=float(getattr(ref.widget, "text_fontsize", 12.0) or 12.0),
                        page_number=ref.page_number,
                        x0=ref.x0,
                        y0=ref.y0,
                        x1=ref.x1,
                        y1=ref.y1,
                    )
                )
        return sorted(fields, key=lambda item: (item.page_number, item.y0, item.x0, item.name))
