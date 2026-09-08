"""Synthetic Word fixtures; private production scripts are never checked in."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from organizer import parse_script
from script_import import read_script_file


WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def write_docx(path, body, styles=None, core=None, namespace=WORD):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>')
        if styles is not None:
            archive.writestr("word/styles.xml", f'<w:styles xmlns:w="{namespace}">{styles}</w:styles>')
        if core is not None:
            archive.writestr("docProps/core.xml", '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">' + core + '</cp:coreProperties>')
    return path


class ScriptImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_custom_title_metadata_table_and_timed_shots_preserve_order(self):
        path = write_docx(self.base / "餐厅拍摄.docx", '''
            <w:p><w:r><w:t>餐厅系列</w:t></w:r></w:p>
            <w:p><w:pPr><w:pStyle w:val="customTitle"/></w:pPr><w:r><w:t>晚班交接</w:t></w:r></w:p>
            <w:tbl><w:tr><w:tc><w:p><w:r><w:t>成片时长 20 秒</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>出镜人物 店长 / 服务员</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
            <w:p><w:r><w:t>01  0-8秒  环境开场</w:t></w:r></w:p>
            <w:p><w:r><w:t>镜头：门店外观。</w:t></w:r></w:p>
            <w:p><w:r><w:t>店长：今天开始交接。</w:t></w:r></w:p>
            <w:p><w:r><w:t>02  9-20秒  收尾</w:t></w:r></w:p>
            <w:p><w:r><w:t>镜头：厨房特写。</w:t></w:r></w:p>
            <w:p><w:r><w:t>服务员：准备好了。</w:t></w:r></w:p>
        ''', styles='<w:style w:styleId="customTitle"><w:name w:val="Script Title"/></w:style>', core='<dc:title>过时的标题</dc:title>')
        original = path.read_bytes()
        result = read_script_file(path)
        self.assertEqual(result["title"], "晚班交接")
        self.assertEqual(result["source_name"], path.name)
        self.assertEqual(result["warnings"], [])
        self.assertIn("成片时长 20 秒\t出镜人物 店长 / 服务员", result["body"])
        sections = parse_script(result["title"], result["body"])
        self.assertEqual(len(sections), 2)
        self.assertEqual("".join(item["text"] for item in sections), result["body"])
        self.assertIn("店长：今天开始交接。", sections[0]["text"])
        self.assertIn("服务员：准备好了。", sections[1]["text"])
        self.assertEqual(path.read_bytes(), original)

    def test_title_style_inheritance_core_and_filename_fallbacks(self):
        styles = '<w:style w:styleId="parent"><w:name w:val="标题"/></w:style><w:style w:styleId="custom"><w:name w:val="自定义"/><w:basedOn w:val="parent"/></w:style>'
        body = '<w:p><w:pPr><w:pStyle w:val="custom"/></w:pPr><w:r><w:t>门店故事</w:t></w:r></w:p>'
        self.assertEqual(read_script_file(write_docx(self.base / "a.docx", body, styles))["title"], "门店故事")
        body = '<w:p><w:r><w:t>正文</w:t></w:r></w:p>'
        self.assertEqual(read_script_file(write_docx(self.base / "b.docx", body, core='<dc:title>正式标题</dc:title>'))["title"], "正式标题")
        self.assertEqual(read_script_file(write_docx(self.base / "文件名.DOCX", body))["title"], "文件名")

    def test_table_content_controls_nested_table_and_soft_breaks(self):
        path = write_docx(self.base / "表格.docx", '''
            <w:p><w:r><w:t>前文</w:t><w:tab/><w:t>字段</w:t><w:br/><w:t>续行</w:t></w:r></w:p>
            <w:sdt><w:sdtContent><w:tbl><w:tr><w:tc><w:p><w:r><w:t>第一格</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>嵌套格</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:tc><w:tc><w:p><w:r><w:t>第二格</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:sdtContent></w:sdt>
            <w:p><w:r><w:t>后文</w:t></w:r></w:p>
        ''')
        self.assertEqual(read_script_file(path)["body"], "前文\t字段\n续行\n第一格\n嵌套格\t第二格\n后文")

    def test_strict_ooxml_and_hyperlink_display_text(self):
        path = write_docx(self.base / "严格.docx", '<w:p><w:hyperlink><w:r><w:t>显示文字</w:t></w:r></w:hyperlink></w:p>', namespace="http://purl.oclc.org/ooxml/wordprocessingml/main")
        self.assertEqual(read_script_file(path)["body"], "显示文字")

    def test_revisions_and_omitted_images_are_reported_without_executing(self):
        path = write_docx(self.base / "修订.docx", '''<w:p>
            <w:del><w:r><w:delText>删除的台词</w:delText></w:r></w:del>
            <w:ins><w:r><w:t>新增的台词</w:t></w:r></w:ins>
            <w:r><w:pict><w:txbxContent><w:p><w:r><w:t>文本框</w:t></w:r></w:p></w:txbxContent></w:pict></w:r>
            </w:p>''')
        result = read_script_file(path)
        self.assertEqual(result["body"], "新增的台词")
        self.assertEqual(len(result["warnings"]), 2)

    def test_bad_unsupported_missing_and_image_only_files_are_actionable(self):
        bad = self.base / "损坏.docx"
        bad.write_bytes(b"not a zip")
        image_only = write_docx(self.base / "扫描.docx", '<w:p><w:r><w:drawing/></w:r></w:p>')
        for path in (bad, image_only, self.base / "不存在.docx", self.base / "旧版.doc"):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                read_script_file(path)

    def test_file_xml_text_limits_and_entity_declarations(self):
        path = write_docx(self.base / "限制.docx", '<w:p><w:r><w:t>场景描述</w:t></w:r></w:p>')
        for constant in ("MAX_FILE_BYTES", "MAX_XML_BYTES", "MAX_TEXT_CHARS"):
            with self.subTest(constant=constant), patch("script_import." + constant, 2), self.assertRaises(ValueError):
                read_script_file(path)
        with ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY x "entity">]><x/>')
        with self.assertRaisesRegex(ValueError, "不受支持"):
            read_script_file(path)


if __name__ == "__main__":
    unittest.main()
