"""Library presentation, using the editor's existing page shell and styles."""
from __future__ import annotations

import html
import uuid
from urllib.parse import quote

if __package__:
    from .pdf_library import KINDS, PdfLibrary
else:
    from pdf_library import KINDS, PdfLibrary


def render_library(library: PdfLibrary, system: str, kind: str, query: str, message: str, session: str) -> str:
    catalog = library.catalog()
    systems = {entry['id']:entry['name'] for entry in catalog['systems']}
    options = ''.join(f'<option value="{html.escape(identifier)}"{" selected" if identifier==system else ""}>{html.escape(name)}</option>' for identifier,name in systems.items())
    kinds = ''.join(f'<option value="{identifier}"{" selected" if identifier==kind else ""}>{name}</option>' for identifier,name in KINDS.items())
    cards = []
    for item in library.list_items(system,kind,query):
        identifier = quote(item['id'])
        capabilities = item.get('capabilities',{})
        counts = capabilities.get('fields',{})
        field_parts = [f'{label}: {counts[name]}' for name,label in (('Text','текст'),('CheckBox','флажки'),('Button','кнопки / изображения')) if counts.get(name)]
        field_note = ', '.join(field_parts) if field_parts else 'PDF без поддерживаемых полей · просмотр'
        if capabilities.get('readonly_fields'):
            field_note += f" · только чтение: {capabilities['readonly_fields']}"
        scope = 'Личный' if item['private'] else 'Из проекта'
        binding = 'Сопоставление полей настроено' if item.get('profile') else 'Фактические поля PDF'
        create = ''
        if item['kind'] in ('template','character'):
            action = 'Открыть личный лист' if item['kind']=='character' else 'Создать рабочую копию'
            create = f'''<form method="post" action="/library/create">
              <input type="hidden" name="session" value="{html.escape(session)}">
              <input type="hidden" name="id" value="{identifier}">
              <input type="hidden" name="copy_id" value="{uuid.uuid4().hex}">
              <button type="submit">{action}</button></form>'''
        cards.append(f'''<article class="picker-card library-card">
          <img class="library-preview" loading="lazy" src="/library/preview?id={identifier}" alt="Первая страница: {html.escape(item['title'])}">
          <h2>{html.escape(item['title'])}</h2>
          <p class="entry-meta">{html.escape(' · '.join(systems[s] for s in item['systems']))} · {KINDS[item['kind']]} · {scope}</p>
          <p>{capabilities.get('pages','?')} стр. · {field_note}</p><p class="entry-meta">{html.escape(binding)}</p>
          <div class="actions">{create}<a class="btn secondary" href="/library/pdf?id={identifier}" target="_blank" rel="noopener">Просмотреть PDF</a></div>
        </article>''')
    status = f'<div class="status">{html.escape(message)}</div>' if message else ''
    all_system_options = ''.join(f'<option value="{html.escape(identifier)}">{html.escape(name)}</option>' for identifier,name in systems.items())
    new_system_state = " hidden disabled" if systems else ""
    all_kind_options = ''.join(f'<option value="{identifier}">{name}</option>' for identifier,name in KINDS.items() if identifier!='character')
    return f'''<div class="wrap">
      <div class="topbar"><div><div class="title">Библиотека НРИ</div><div class="meta">Шаблоны, книги правил и памятки по системам</div></div>
        <div class="actions"><a class="btn secondary" href="/">Редактор</a><a class="btn secondary" href="/choose-pdf">Мои PDF</a></div></div>
      {status}
      <form class="picker-card library-filters" method="get" action="/library">
        <label>Система<select name="system"><option value="">Все системы</option>{options}</select></label>
        <label>Материал<select name="kind"><option value="">Все виды</option>{kinds}</select></label>
        <label>Поиск<input name="q" value="{html.escape(query)}" placeholder="Название материала"></label><button>Найти</button>
      </form>
      <details class="picker-card library-add"><summary>Добавить PDF в библиотеку</summary>
        <form class="library-form" method="post" action="/library/add" enctype="multipart/form-data">
          <input type="hidden" name="session" value="{html.escape(session)}">
          <label>PDF<input type="file" name="pdf" accept="application/pdf,.pdf" required></label>
          <label>Название<input name="title" required></label>
          <label>Система<select name="system" onchange="const block=this.form.querySelector('fieldset');block.hidden=this.value!=='new';block.disabled=block.hidden">{all_system_options}<option value="new">Другая НРИ</option></select></label>
          <label>Вид материала<select name="kind">{all_kind_options}</select></label>
          <fieldset{new_system_state}><legend>Для другой НРИ</legend><label>ID системы<input name="new_system" placeholder="Например, fate-core" pattern="[a-z0-9][a-z0-9-]*" required></label><label>Название системы<input name="system_name" placeholder="Например, Fate Core" required></label></fieldset>
          <details><summary>Дополнительное сопоставление полей</summary><label>Профиль JSON<input type="file" name="profile" accept="application/json,.json"></label><p>Необязательно: без профиля доступны фактические поля PDF. Профиль задаёт логические имена, а не правила игры.</p></details>
          <p>Файл сохраняется локально и не попадает в Git. Исходный PDF не меняется. Для заполнения шаблона создаётся рабочая копия. PDF без полей доступен для просмотра.</p>
          <button type="submit">Добавить PDF</button>
        </form>
      </details>
      <div class="library-grid">{''.join(cards) or '<p>Материалы не найдены. Измени фильтр или добавь PDF.</p>'}</div>
    </div>'''
