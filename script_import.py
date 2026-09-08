"""Read editable shooting scripts without requiring Office or opening links.

Only text is imported. The source document stays untouched and no media, macros,
external relationships, or embedded objects are executed or extracted.
"""
from __future__ import annotations

from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 100000
_WORD = frozenset(("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                   "http://purl.oclc.org/ooxml/wordprocessingml/main"))


def _tag(element):
    if not isinstance(element.tag, str) or not element.tag.startswith("{"):
        return ""
    namespace, _, name = element.tag[1:].partition("}")
    return name if namespace in _WORD else ""


def _attribute(element, name, default=""):
    if element is not None:
        for namespace in _WORD:
            value = element.get("{" + namespace + "}" + name)
            if value is not None:
                return value
    return default


def _child(element, name):
    return next((item for item in element if _tag(item) == name), None) if element is not None else None


def _xml(archive, name, *, optional=False):
    try:
        record = archive.getinfo(name)
    except KeyError:
        if optional:
            return None
        raise ValueError("Word 文件缺少正文，请重新另存为 .docx") from None
    if record.file_size > MAX_XML_BYTES:
        raise ValueError("Word 正文或样式过大，请拆分脚本后导入")
    with archive.open(record) as stream:
        data = stream.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ValueError("Word 正文或样式过大，请拆分脚本后导入")
    # ElementTree does not fetch external entities. Reject DTD/entity declarations
    # as well so document input cannot trigger entity expansion.
    normalized = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in normalized or b"<!ENTITY" in normalized:
        raise ValueError("Word 文档 XML 格式不受支持，请重新另存为 .docx")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise ValueError("Word 文件内容损坏，请在 Word 中重新保存后导入") from None


def _paragraph(element):
    result = []

    def visit(node):
        name = _tag(node)
        if name in ("del", "moveFrom", "drawing", "pict", "txbxContent"):
            return
        if name == "t":
            result.append(node.text or "")
        elif name in ("tab", "ptab"):
            result.append("\t")
        elif name in ("br", "cr"):
            result.append("\n")
        elif name == "noBreakHyphen":
            result.append("\u2011")
        elif name == "softHyphen":
            result.append("\u00ad")
        else:
            for child in node:
                visit(child)

    visit(element)
    return "".join(result)


def _blocks(element):
    """Yield paragraph/table blocks in document order, including content controls."""
    for child in element:
        name = _tag(child)
        if name in ("p", "tbl"):
            yield child
        elif name not in ("del", "moveFrom", "sectPr", "tblPr", "tcPr", "trPr", "tblGrid"):
            yield from _blocks(child)


def _rows(table):
    for child in table:
        name = _tag(child)
        if name == "tr":
            yield child
        elif name in ("sdt", "sdtContent", "customXml", "ins", "moveTo"):
            yield from _rows(child)


def _cells(row):
    for child in row:
        name = _tag(child)
        if name == "tc":
            yield child
        elif name in ("sdt", "sdtContent", "customXml", "ins", "moveTo"):
            yield from _cells(child)


def _table_text(table):
    lines = []
    for row in _rows(table):
        cells = []
        for cell in _cells(row):
            cells.append("\n".join(_paragraph(block) if _tag(block) == "p" else _table_text(block)
                                   for block in _blocks(cell)))
        lines.append("\t".join(cells))
    return "\n".join(lines)


def _title_styles(styles):
    records = {}
    if styles is None:
        return {"Title", "标题", "ScriptTitle"}
    for style in styles:
        if _tag(style) == "style":
            identity = _attribute(style, "styleId")
            name = _attribute(_child(style, "name"), "val", identity)
            parent = _attribute(_child(style, "basedOn"), "val")
            records[identity] = (name, parent)
    result = set()
    for identity in records:
        seen, current = set(), identity
        while current in records and current not in seen:
            seen.add(current)
            name, parent = records[current]
            normalized = re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", name).casefold())
            if normalized in ("title", "scripttitle", "documenttitle", "标题", "脚本标题", "文档标题"):
                result.add(identity)
                break
            current = parent
    return result


def _validate_text(title, body):
    if not body.strip():
        raise ValueError("文档没有可导入的正文文字；图片或扫描件需先转换为文字")
    if len(body) > MAX_TEXT_CHARS:
        raise ValueError("脚本正文超过 100000 字，请拆分文件后导入")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in body):
        raise ValueError("脚本含有不支持的控制字符，请重新保存为 Word 文档")
    title = title.strip()
    if not title or len(title) > 200 or any(ord(char) < 32 for char in title):
        raise ValueError("脚本标题需为 1–200 字，请修改文档标题后重新导入")
    return title, body


def read_script_file(path):
    """Return title/body/source_name/warnings; do not save a script or plan."""
    path = Path(path)
    if path.suffix.casefold() != ".docx":
        raise ValueError("当前支持 Word .docx 脚本；请将 .doc 或其他格式另存为 .docx 后导入")
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Word 文件超过 32 MB，请移除不需要的图片或拆分后导入")
        with ZipFile(path) as archive:
            body_xml = _xml(archive, "word/document.xml")
            body_node = _child(body_xml, "body")
            if body_node is None:
                raise ValueError("Word 文件缺少正文，请重新另存为 .docx")
            styles = _title_styles(_xml(archive, "word/styles.xml", optional=True))
            title, lines, text_size = "", [], 0
            for block in _blocks(body_node):
                if _tag(block) == "p":
                    value = _paragraph(block)
                    style = _attribute(_child(_child(block, "pPr"), "pStyle"), "val")
                    if not title and style in styles and value.strip():
                        title = value.strip()
                else:
                    value = _table_text(block)
                lines.append(value)
                text_size += len(value) + 1
                if text_size > MAX_TEXT_CHARS + 1:
                    raise ValueError("脚本正文超过 100000 字，请拆分文件后导入")
            if not title:
                props = _xml(archive, "docProps/core.xml", optional=True)
                node = props.find("{http://purl.org/dc/elements/1.1/}title") if props is not None else None
                title = node.text.strip() if node is not None and node.text and node.text.strip() else path.stem
            title, body = _validate_text(title, "\n".join(lines))
            warnings = []
            if any(_tag(node) in ("drawing", "pict", "txbxContent", "altChunk") for node in body_xml.iter()):
                warnings.append("图片、文本框及嵌入对象未导入，请核对正文是否完整。")
            if any(_tag(node) in ("ins", "del", "moveFrom", "moveTo") for node in body_xml.iter()):
                warnings.append("文档含修订，已按接受修订后的正文导入，请核对。")
            return dict(title=title, body=body, source_name=path.name, warnings=warnings)
    except ValueError:
        raise
    except (BadZipFile, KeyError, RuntimeError, NotImplementedError):
        raise ValueError("无法读取 Word 文件；请确认是未加密的 .docx，并在 Word 中重新保存") from None
    except OSError:
        raise ValueError("无法读取脚本文件，请确认文件存在且有访问权限") from None
