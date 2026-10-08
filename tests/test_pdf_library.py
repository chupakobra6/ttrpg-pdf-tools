from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import base64
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.pdf_form_editor import PdfFormEditor
from scripts.pdf_form_web_editor import AppState
from scripts.pdf_library import PdfLibrary, inspect_pdf, write_json
from scripts.template_profiles import detect_profile, profile_from_data
from test_pdf_file_state import make_pdf
import test_pdf_form_web_editor as web_tests


def fixture(root: Path) -> tuple[PdfLibrary, bytes]:
    pdf = root/'source.pdf'
    make_pdf(pdf)
    write_json(root/'library/catalog.json', {
        'schema_version':1, 'systems':[{'id':'example-game','name':'Example Game'}], 'items':[],
    })
    return PdfLibrary(root), pdf.read_bytes()


def profile_data() -> dict:
    return {'schema_version':1,'id':'example-sheet','system':'example-game',
            'required_fields':['Name','Ready'],'text_markers':[],
            'text_fields':{'hero':'Name'},'checkbox_fields':{'prepared':'Ready'}}


class PdfLibraryTests(unittest.TestCase):
    def test_import_filter_new_system_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            library, data = fixture(Path(tmp))
            item = library.add(data,'sheet.pdf','example-game','Custom sheet')
            self.assertTrue(item['private'])
            self.assertIsNone(item['profile'])
            self.assertEqual(library.add(data,'sheet.pdf','example-game','Custom sheet')['id'],item['id'])
            self.assertEqual(len(library.list_items(query='CUSTOM')),1)
            self.assertFalse(library.list_items(system='other-game'))
            with self.assertRaises(ValueError):
                library.add(data,'sheet.pdf','other-game','Other sheet')
            library.add(data,'sheet.pdf','other-game','Other sheet',system_name='Other Game')
            self.assertEqual(len(library.list_items()),2)
            self.assertEqual(library.check()['errors'],[])
            self.assertEqual(json.loads(library.public_catalog.read_text())['items'],[])

    def test_custom_profile_survives_import_copy_edit_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library, data = fixture(root)
            item = library.add(data,'sheet.pdf','example-game','Mapped sheet',profile_data=profile_data())
            target = library.create_working_copy(item['id'])
            editor = PdfFormEditor(target,profile_root=root)
            self.addCleanup(editor.close)
            self.assertEqual(editor.template_profile,'example-sheet')
            editor.set_text('hero','Changed name')
            editor.set_checkbox('prepared',True)
            editor.autosize_text_fields()
            editor.save()
            reopened = PdfFormEditor(target,profile_root=root)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.field_value('hero'),'Changed name')
            self.assertTrue(reopened.checkbox_checked('prepared'))
            self.assertEqual(library.resolve_path(item['path']).read_bytes(),data)
            self.assertEqual(library.check()['errors'],[])

    def test_invalid_inputs_do_not_publish_files_or_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library, data = fixture(root)
            cases = [
                (b'not a PDF','sheet.pdf',None),
                (data,'sheet.txt',None),
                (data,'sheet.pdf',{**profile_data(),'required_fields':['Missing']}),
                (data,'sheet.pdf',{**profile_data(),'text_fields':{'hero':'Ready'}}),
                (data,'sheet.pdf',{**profile_data(),'system':'another-game'}),
                (data,'sheet.pdf',[]),
                (data,'sheet.pdf',{**profile_data(),'compact_rows':['[']}),
            ]
            for content, filename, profile in cases:
                with self.subTest(filename=filename,profile=profile), self.assertRaises((ValueError,fitz.FileDataError)):
                    library.add(content,filename,'example-game','Invalid',profile_data=profile)
            self.assertFalse(library.local_catalog.exists())
            self.assertFalse(list((root/'templates/local').rglob('*.pdf')))

    def test_repeated_copy_keeps_edits_and_new_request_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            library, data = fixture(Path(tmp))
            item = library.add(data,'sheet.pdf','example-game','Sheet')
            token = 'a'*32
            first = library.create_working_copy(item['id'],token)
            editor = PdfFormEditor(first)
            editor.set_text('Name','User edit')
            editor.save()
            editor.close()
            saved = first.read_bytes()
            self.assertEqual(library.create_working_copy(item['id'],token),first)
            self.assertEqual(first.read_bytes(),saved)
            second = library.create_working_copy(item['id'])
            self.assertNotEqual(first,second)
            self.assertEqual(second.read_bytes(),data)
            self.assertEqual(library.resolve_path(item['path']).read_bytes(),data)

    def test_concurrent_import_and_copy_have_one_result_per_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            library, data = fixture(Path(tmp))
            with ThreadPoolExecutor(max_workers=4) as pool:
                items = list(pool.map(lambda _:library.add(data,'sheet.pdf','example-game','Same request'),range(8)))
                copies = list(pool.map(lambda _:library.create_working_copy(items[0]['id'],'b'*32),range(8)))
            self.assertEqual(len({item['id'] for item in items}),1)
            self.assertEqual(len(set(copies)),1)
            self.assertEqual(library.check()['errors'],[])
            self.assertEqual(copies[0].read_bytes(),data)

    def test_failed_catalog_write_rolls_back_pdf_and_new_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library, data = fixture(root)
            original_write = write_json
            def fail_catalog(path, record):
                if path == library.local_catalog:
                    raise OSError('Catalog unavailable')
                original_write(path,record)
            with patch('scripts.pdf_library.write_json',side_effect=fail_catalog), self.assertRaises(OSError):
                library.add(data,'sheet.pdf','example-game','Sheet',profile_data=profile_data())
            self.assertFalse(list((root/'templates/local').rglob('*.pdf')))
            self.assertFalse(list((root/'templates/local/library/profiles').glob('*.json')))
            self.assertFalse(library.local_catalog.exists())

    def test_path_and_privacy_validation(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as external:
            root = Path(tmp)
            library, data = fixture(root)
            outside = Path(external)/'outside.pdf'
            outside.write_bytes(data)
            (root/'escape.pdf').symlink_to(outside)
            for raw in ('../outside.pdf',str(outside),'escape.pdf','library/catalog.json'):
                with self.subTest(path=raw), self.assertRaises(ValueError):
                    library.resolve_path(raw)
            item = library.add(data,'sheet.pdf','example-game','Private')
            public = json.loads(library.public_catalog.read_text())
            public['items'] = [{k:v for k,v in item.items() if k!='private'}]
            write_json(library.public_catalog,public)
            with self.assertRaisesRegex(ValueError,'каталоги перепутаны'):
                library.catalog()

    def test_profile_ambiguity_and_wrong_mapping_are_rejected(self):
        one = profile_from_data(profile_data())
        two = profile_from_data({**profile_data(),'id':'other-sheet'})
        with self.assertRaisesRegex(ValueError,'нескольким'):
            detect_profile({'Name':'Text','Ready':'CheckBox'},'',{one.id:one,two.id:two})
        with self.assertRaisesRegex(ValueError,'тип Text'):
            detect_profile({'Name':'CheckBox','Ready':'CheckBox'},'',{one.id:one})

    def test_ambiguous_profile_import_is_rejected_before_catalog_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            library,data = fixture(Path(tmp))
            library.add(data,'one.pdf','example-game','One',profile_data=profile_data())
            original = library.local_catalog.read_bytes()
            with self.assertRaisesRegex(ValueError,'нескольким'):
                library.add(data,'two.pdf','example-game','Two',profile_data={**profile_data(),'id':'other-sheet'})
            self.assertEqual(library.local_catalog.read_bytes(),original)
            self.assertEqual(library.check()['errors'],[])

    def test_readonly_and_wrong_type_never_change_saved_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library, data = fixture(root)
            pdf = root/'readonly.pdf'
            with fitz.open(stream=data,filetype='pdf') as doc:
                page = doc[0]
                locked = fitz.Widget()
                locked.field_name = 'Locked'
                locked.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                locked.field_flags = fitz.PDF_FIELD_IS_READ_ONLY
                locked.field_value = 'Keep original'
                locked.text_fontsize = 8
                locked.rect = fitz.Rect(20,120,180,150)
                page.add_widget(locked)
                doc.save(pdf)
            editor = PdfFormEditor(pdf)
            self.addCleanup(editor.close)
            for action in (lambda:editor.set_text('Locked','Overwrite'),lambda:editor.set_text('Ready','Wrong type'),lambda:editor.set_checkbox('Name',True),lambda:editor.set_portrait_image(b'bad image','Name')):
                with self.assertRaises(ValueError):action()
            with self.assertRaises(KeyError):editor.set_text('Missing','Missing')
            editor.set_text('Name','Writable')
            editor.autosize_text_fields('all')
            editor.save()
            with fitz.open(pdf) as doc:
                fields = {w.field_name:(w.field_value,w.text_fontsize) for w in doc[0].widgets()}
                self.assertEqual(fields['Locked'],('Keep original',8))
                self.assertEqual(fields['Name'][0],'Writable')
            self.assertEqual(inspect_pdf(pdf.read_bytes())['readonly_fields'],1)


class LibraryHttpTests(unittest.TestCase):
    _start_server = web_tests.PdfFormWebEditorTests._start_server
    _request = web_tests.PdfFormWebEditorTests._request
    _multipart_body = web_tests.PdfFormWebEditorTests._multipart_body
    _form_tokens = web_tests.PdfFormWebEditorTests._form_tokens

    def test_start_upload_preview_copy_edit_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library, data = fixture(root)
            state = AppState(None,root/'templates/local','filled',1.0,library_root=root)
            server,_ = self._start_server(state)
            status,headers,_ = self._request(server,'GET','/')
            self.assertEqual((status,headers['Location']),(303,'/library'))
            self.assertEqual(self._request(server,'GET','/page/0.png')[0],404)
            status,_,page = self._request(server,'GET','/library')
            self.assertEqual(status,200)
            self.assertIn('Библиотека НРИ'.encode(),page)
            body,content_type = self._multipart_body(
                {'session':state.session_id,'title':'HTTP template','system':'example-game','kind':'template'},
                {'pdf':('custom.pdf',data,'application/pdf'), 'profile':('profile.json',json.dumps(profile_data()).encode(),'application/json')},
            )
            status,headers,_ = self._request(server,'POST','/library/add',body,{'Content-Type':content_type})
            self.assertEqual(status,303)
            item = library.list_items()[0]
            for route,expected_mime in (('pdf','application/pdf'),('preview','image/png')):
                status,headers,rendered = self._request(server,'GET',f'/library/{route}?id={item["id"]}')
                self.assertEqual(status,200)
                self.assertEqual(headers['Content-Type'],expected_mime)
                self.assertTrue(rendered)
            create = urllib.parse.urlencode({'session':state.session_id,'id':item['id'],'copy_id':'c'*32})
            status,_,_ = self._request(server,'POST','/library/create',create,{'Content-Type':'application/x-www-form-urlencoded'})
            self.assertEqual(status,303)
            target = state.pdf_path
            body,content_type = self._multipart_body({**self._form_tokens(server),'text:Name':'Edited via HTTP','check:Ready':'on'}, {})
            status,_,_ = self._request(server,'POST','/save',body,{'Content-Type':content_type})
            self.assertEqual(status,303)
            before_repeat = target.read_bytes()
            self.assertEqual(self._request(server,'POST','/library/create',create,{'Content-Type':'application/x-www-form-urlencoded'})[0],303)
            self.assertEqual(target.read_bytes(),before_repeat)
            self.assertEqual(library.resolve_path(item['path']).read_bytes(),data)
            reopened = PdfFormEditor(target,profile_root=root)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.field_value('hero'),'Edited via HTTP')
            self.assertTrue(reopened.checkbox_checked('prepared'))
            restart,_ = self._start_server(AppState(None,state.picker_root,'filled',1.0,library_root=root))
            self.assertIn(b'HTTP template',self._request(restart,'GET','/library')[2])
            self.assertEqual(library.check()['errors'],[])

    def test_library_error_is_utf8_and_failed_upload_keeps_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library, data = fixture(root)
            state = AppState(None,root,'filled',1.0,library_root=root)
            server,_ = self._start_server(state)
            status,_,page = self._request(server,'GET','/library/pdf?id=missing')
            self.assertEqual(status,400)
            self.assertIn('Материал не найден'.encode(),page)
            for session,filename in ((state.session_id,'invalid.txt'),('old-session','valid.pdf')):
                body,content_type = self._multipart_body({'session':session,'title':'Keep draft','system':'example-game','kind':'template'}, {'pdf':(filename,data,'application/pdf')})
                status,_,page = self._request(server,'POST','/library/add',body,{'Content-Type':content_type})
                self.assertEqual(status,400 if filename.endswith('.txt') else 409)
                self.assertIn(b'Keep draft',page)
                self.assertIn(b'files_base64',base64.b64decode(re.search(rb'data:application/json;base64,([^\']+)',page)[1]))
            self.assertFalse(library.list_items())


if __name__ == '__main__':
    unittest.main()
