"""Declarative form profiles; the PDF engine does not own game-system rules."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FormProfile:
    id: str
    system: str
    required_fields: tuple[str, ...]
    text_markers: tuple[str, ...]
    text_fields: dict[str, str]
    checkbox_fields: dict[str, str]
    image_fields: tuple[str, ...]
    compact_rows: tuple[re.Pattern, ...]
    equal_font_groups: dict[str, re.Pattern]

    def matches(self, fields: Mapping[str, str], page_text: str) -> bool:
        return set(self.required_fields).issubset(fields) and all(
            marker in page_text for marker in self.text_markers
        )

    def validate_fields(self, fields: Mapping[str, str]) -> None:
        if not set(self.required_fields).issubset(fields):
            raise ValueError(f"Профиль {self.id} не соответствует полям PDF")
        for expected, mapping in (("Text", self.text_fields), ("CheckBox", self.checkbox_fields)):
            for name in mapping.values():
                if fields.get(name) != expected:
                    raise ValueError(f"Профиль {self.id}: поле {name!r} должно иметь тип {expected}")


def profile_from_data(data: dict) -> FormProfile:
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Неподдерживаемая версия профиля')
    for name in ('id', 'system'):
        pattern = r'[a-z0-9][a-z0-9-]*' if name == 'system' else r'[a-z0-9][a-z0-9_-]*'
        if not isinstance(data.get(name), str) or not re.fullmatch(pattern, data[name]):
            raise ValueError(f'Некорректный {name} профиля')
    arrays = {}
    for name in ('required_fields', 'text_markers', 'image_fields', 'compact_rows'):
        values = data.get(name, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f'Некорректный список {name}')
        arrays[name] = tuple(values)
    if not arrays['required_fields']:
        raise ValueError('Профилю нужны required_fields для безопасного распознавания')
    mappings = {}
    for name in ('text_fields', 'checkbox_fields', 'equal_font_groups'):
        values = data.get(name, {})
        if not isinstance(values, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and v for k,v in values.items()):
            raise ValueError(f'Некорректное сопоставление {name}')
        mappings[name] = values
    try:
        compact_rows = tuple(re.compile(pattern) for pattern in arrays['compact_rows'])
        equal_font_groups = {name:re.compile(pattern) for name,pattern in mappings['equal_font_groups'].items()}
    except re.error as exc:
        raise ValueError(f'Некорректное регулярное выражение в профиле: {exc}') from exc
    return FormProfile(
        data['id'], data['system'], arrays['required_fields'], arrays['text_markers'],
        mappings['text_fields'], mappings['checkbox_fields'], arrays['image_fields'],
        compact_rows, equal_font_groups,
    )


def load_profiles(root: Path = PROJECT_ROOT) -> dict[str, FormProfile]:
    profiles = {}
    for directory in (root/'library/profiles', root/'templates/local/library/profiles'):
        for path in sorted(directory.glob('*.json')):
            profile = profile_from_data(json.loads(path.read_text()))
            if profile.id in profiles:
                raise ValueError(f'Повторный профиль: {profile.id}')
            profiles[profile.id] = profile
    return profiles


def detect_profile(fields: Mapping[str, str], page_text: str, profiles: Mapping[str, FormProfile]) -> FormProfile | None:
    matches = [profile for profile in profiles.values() if profile.matches(fields, page_text)]
    if len(matches) > 1:
        raise ValueError('PDF соответствует нескольким профилям: ' + ', '.join(p.id for p in matches))
    if not matches:
        return None
    matches[0].validate_fields(fields)
    return matches[0]
