#!/usr/bin/env python3
"""One catalog for public templates, system materials and private additions."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import fitz

if __package__:
    from .pdf_file_state import FileConflict, locked_files, read_snapshot, publish
    from .template_profiles import PROJECT_ROOT, detect_profile, load_profiles, profile_from_data
else:
    from pdf_file_state import FileConflict, locked_files, read_snapshot, publish
    from template_profiles import PROJECT_ROOT, detect_profile, load_profiles, profile_from_data

KINDS = {'template':'Шаблон', 'reference':'Памятка', 'rules':'Книга правил', 'homebrew':'Домашние правила', 'character':'Личный лист'}


def inspect_pdf(data: bytes) -> dict:
    with fitz.open(stream=data, filetype='pdf') as document:
        if document.is_encrypted or not len(document):
            raise ValueError('PDF должен быть непустым и доступным без пароля')
        fields = Counter()
        readonly = 0
        for page in document:
            for widget in page.widgets() or ():
                fields[widget.field_type_string] += 1
                readonly += bool(widget.field_flags & fitz.PDF_FIELD_IS_READ_ONLY)
        return {'pages':len(document), 'fields':dict(fields), 'readonly_fields':readonly,
                'sha256':hashlib.sha256(data).hexdigest()}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.tmp_catalog_', suffix='.json', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PdfLibrary:
    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root.resolve()
        self.public_catalog = self.root/'library/catalog.json'
        self.local_catalog = self.root/'templates/local/library/catalog.json'

    def _read(self, path: Path) -> dict:
        if not path.exists():
            return {'schema_version':1, 'systems':[], 'items':[]}
        data = json.loads(path.read_text())
        if not isinstance(data,dict) or data.get('schema_version') != 1 or not isinstance(data.get('systems'),list) or not isinstance(data.get('items'),list):
            raise ValueError(f'Некорректный каталог: {path}')
        return data

    def catalog(self) -> dict:
        systems = {}
        items = []
        ids = set()
        for path, private in ((self.public_catalog,False),(self.local_catalog,True)):
            data = self._read(path)
            for system in data['systems']:
                if not isinstance(system,dict) or not isinstance(system.get('id'),str) or not isinstance(system.get('name'),str):
                    raise ValueError('Некорректная система в каталоге')
                identifier = system['id']
                if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', identifier) or not system.get('name'):
                    raise ValueError('Некорректная система в каталоге')
                if identifier in systems and systems[identifier] != system:
                    raise ValueError(f'Система определена дважды: {identifier}')
                systems[identifier] = system
            for item in data['items']:
                if not isinstance(item,dict) or not all(isinstance(item.get(key),str) for key in ('id','title','kind','path')) or not isinstance(item.get('systems'),list) or not all(isinstance(s,str) for s in item['systems']):
                    raise ValueError('Некорректная запись материала в каталоге')
                if item.get('profile') is not None and not isinstance(item['profile'],str):
                    raise ValueError(f'Некорректный профиль материала: {item["id"]}')
                if item['id'] in ids or not re.fullmatch(r'[a-z0-9][a-z0-9-]*',item['id']):
                    raise ValueError('Повторный или некорректный ID материала')
                if item['kind'] not in KINDS or not item.get('systems') or not item.get('title'):
                    raise ValueError(f'Некорректный материал: {item["id"]}')
                ids.add(item['id'])
                resolved = self.resolve_path(item['path'])
                local_root = (self.root/'templates/local').resolve()
                if private != (local_root in resolved.parents):
                    raise ValueError(f'Публичный и личный каталоги перепутаны: {item["id"]}')
                items.append({**item,'private':private})
        for item in items:
            if not set(item['systems']).issubset(systems):
                raise ValueError(f'Неизвестная система материала: {item["id"]}')
        return {'schema_version':1,'systems':list(systems.values()),'items':items}

    def resolve_path(self, raw: str) -> Path:
        relative = Path(raw)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Путь материала должен находиться внутри проекта')
        path = (self.root/relative).resolve()
        if self.root not in path.parents or path.suffix.lower() != '.pdf':
            raise ValueError('Материал должен быть PDF внутри проекта')
        return path

    def item(self, identifier: str) -> dict:
        return next((item for item in self.catalog()['items'] if item['id']==identifier), None) or self._missing(identifier)

    def _missing(self, identifier: str):
        raise ValueError(f'Материал не найден: {identifier}')

    def list_items(self, system: str = '', kind: str = '', query: str = '') -> list[dict]:
        return [item for item in self.catalog()['items']
                if (not system or system in item['systems']) and (not kind or kind==item['kind'])
                and (not query or query.casefold() in (item['title']+' '+item['path']).casefold())]

    def _validate_profile(self, identifier: str | None, data: bytes, profile_data: dict | None = None):
        profiles = load_profiles(self.root)
        with fitz.open(stream=data,filetype='pdf') as doc:
            fields = {widget.field_name:widget.field_type_string for page in doc for widget in page.widgets() or ()}
            page_text = doc[0].get_text()
        profile = profile_from_data(profile_data) if profile_data is not None else (profiles.get(identifier) if identifier else detect_profile(fields,page_text,profiles))
        if identifier and profile is None:
            raise ValueError(f'Профиль не найден: {identifier}')
        if profile:
            profile.validate_fields(fields)
            if not profile.matches(fields,page_text):
                raise ValueError(f'Маркеры профиля {profile.id} не соответствуют PDF')
            if profile_data and profile.id in profiles and profiles[profile.id] != profile:
                raise ValueError(f'Профиль уже существует с другими настройками: {profile.id}')
            if profile_data is not None:
                detect_profile(fields,page_text,{**profiles,profile.id:profile})
        return profile

    def add(self, data: bytes, filename: str, system: str, title: str, kind: str = 'template',
            system_name: str = '', profile_data: dict | None = None) -> dict:
        if kind not in KINDS or not title.strip() or not re.fullmatch(r'[a-z0-9][a-z0-9-]*',system):
            raise ValueError('Укажи название, вид материала и ID системы латиницей с дефисами')
        if Path(filename).suffix.lower() != '.pdf':
            raise ValueError('Нужен файл с расширением .pdf')
        capabilities = inspect_pdf(data)
        profile = self._validate_profile(None,data,profile_data)
        if profile and profile.system != system:
            raise ValueError('Профиль относится к другой системе')
        created = []
        with locked_files(self.local_catalog):
            current = self.catalog()
            for item in current['items']:
                if item['private'] and item['systems']==[system] and item['kind']==kind and item.get('profile')==(profile.id if profile else None) and item['title']==title.strip() and item.get('capabilities',{}).get('sha256')==capabilities['sha256']:
                    return item
            local = self._read(self.local_catalog)
            if system not in {entry['id'] for entry in current['systems']}:
                if not system_name.strip():
                    raise ValueError('Для новой системы нужно название')
                local['systems'].append({'id':system,'name':system_name.strip()})
            identifier = 'local-'+uuid.uuid4().hex
            target = self.root/'templates/local/library/files'/system/(identifier+'.pdf')
            record = {'id':identifier,'title':title.strip(),'systems':[system],'kind':kind,
                      'path':str(target.relative_to(self.root)),'profile':profile.id if profile else None,
                      'capabilities':capabilities}
            try:
                if profile_data and profile:
                    profiles = load_profiles(self.root)
                    profile_target = self.root/'templates/local/library/profiles'/(profile.id+'.json')
                    if profile.id not in profiles:
                        write_json(profile_target,profile_data)
                        created.append(profile_target)
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                created.append(target)
                local['items'].append(record)
                write_json(self.local_catalog,local)
            except Exception:
                for path in reversed(created):
                    path.unlink(missing_ok=True)
                raise
        return {**record,'private':True}

    def create_working_copy(self, identifier: str, copy_id: str | None = None) -> Path:
        item = self.item(identifier)
        source = self.resolve_path(item['path'])
        if item['kind']=='character':
            return source
        token = copy_id or uuid.uuid4().hex
        if not re.fullmatch(r'[a-f0-9]{32}',token):
            raise ValueError('Некорректный идентификатор создания листа')
        target = self.root/'templates/local/characters'/item['systems'][0]/(identifier+'-'+token+'.pdf')
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.is_file():
            return target
        snapshot = read_snapshot(source)
        inspect_pdf(snapshot.data)
        self._validate_profile(item.get('profile'),snapshot.data)
        fd,name = tempfile.mkstemp(prefix='.tmp_copy_',suffix='.pdf',dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(snapshot.data)
            try:
                publish(temporary,target,{source:snapshot.version,target:None})
            except FileConflict:
                if not target.is_file():
                    raise
                # A repeated creation request may find an already edited copy.
                # Keep it; never publish template bytes over the user's changes.
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def check(self) -> dict:
        catalog = self.catalog()
        profiles = load_profiles(self.root)
        errors = []
        for item in catalog['items']:
            try:
                data = read_snapshot(self.resolve_path(item['path'])).data
                actual = inspect_pdf(data)
                expected = item.get('capabilities', {})
                if item['kind']=='character':
                    actual.pop('sha256',None)
                    expected = {k:v for k,v in expected.items() if k!='sha256'}
                if actual != expected:
                    raise ValueError('Свойства PDF изменились; обнови запись каталога')
                profile = self._validate_profile(item.get('profile'),data)
                if profile and profile.system not in item['systems']:
                    raise ValueError('Профиль относится к другой системе')
            except Exception as exc:
                errors.append({'id':item['id'],'error':str(exc)})
        return {'items':len(catalog['items']),'profiles':len(profiles),'errors':errors}


def main() -> int:
    parser = argparse.ArgumentParser(description='Библиотека шаблонов и материалов НРИ. Новые файлы хранятся только локально.')
    commands = parser.add_subparsers(dest='command',required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--system',default='')
    listing.add_argument('--kind',choices=KINDS,default='')
    listing.add_argument('--query',default='')
    inspect = commands.add_parser('inspect')
    inspect.add_argument('pdf',type=Path)
    add = commands.add_parser('add')
    add.add_argument('pdf',type=Path)
    add.add_argument('--system',required=True)
    add.add_argument('--system-name',default='')
    add.add_argument('--title',required=True)
    add.add_argument('--kind',choices=KINDS,default='template')
    add.add_argument('--profile-json',type=Path)
    copy = commands.add_parser('copy')
    copy.add_argument('id')
    commands.add_parser('check')
    args = parser.parse_args()
    library = PdfLibrary()
    try:
        if args.command=='list':result = library.list_items(args.system,args.kind,args.query)
        elif args.command=='inspect':result = inspect_pdf(read_snapshot(args.pdf).data)
        elif args.command=='add':
            profile = json.loads(args.profile_json.read_text()) if args.profile_json else None
            result = library.add(read_snapshot(args.pdf).data,args.pdf.name,args.system,args.title,args.kind,args.system_name,profile)
        elif args.command=='copy':result = {'path':str(library.create_working_copy(args.id))}
        else:result = library.check()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if isinstance(result,dict) and result.get('errors') else 0
    except (ValueError,OSError,fitz.FileDataError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__':
    raise SystemExit(main())
