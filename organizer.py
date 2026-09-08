"""Reviewable script-to-material organization using local metadata only.

Planning never opens media. Applying appends ordinary category and collaboration
events in one local transaction, so the existing peer sync also carries results.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid

from category_store import _data as category_data, _name_key
from collaboration_store import CollaborationConflictError, _heads, _identity, _text
from indexer import root_key


MAX_SECTIONS = 50
MAX_PAIRS = 1000
_HEADING = re.compile(
    r"^\s*(?:(?:第\s*)?(?:镜头|分镜|场景|段落|章节|场次)\s*(?:[0-9一二三四五六七八九十百零〇]+|[:：])"
    r"|第\s*[0-9一二三四五六七八九十百零〇]+\s*(?:镜|幕|场|段|章)"
    r"|[（(]?(?:\d{1,3}|[一二三四五六七八九十百]+)[)）、.．])")
_MARKDOWN = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_KEYWORDS = re.compile(r"(?:关键词|关键字|keywords?)\s*[:：]\s*(.*)", re.I)
_STOP = frozenset(("镜头", "分镜", "场景", "段落", "画面", "拍摄", "展示", "然后", "以及", "一个", "我们", "他们",
                   "这个", "那个", "可以", "进行", "使用", "最后", "首先", "接着", "视频", "素材", "关键词", "关键字",
                   "mp4", "mov", "jpg", "png", "jpeg", "wav", "mp3", "the", "and", "with", "from", "this", "that"))
_SPLIT_STOP = re.compile("|".join(sorted((term for term in _STOP if re.search(r"[\u4e00-\u9fff]", term)), key=len, reverse=True)))
_SHOT_NUMBER = re.compile(r"^\s*(?P<number>\d{1,3})(?:[.、)]\s*|\s+)(?P<rest>.+)$")
_TIME_VALUE = r"[+-]?\d+(?::\d+){0,2}(?:\.\d+)?"
_SHOT_TIME = re.compile(
    rf"^\s*(?P<start>{_TIME_VALUE})\s*(?P<start_unit>秒|s)?\s*"
    rf"[-–—~～至]\s*(?P<end>{_TIME_VALUE})(?P<end_unit>\s*(?:秒|s))?(?P<tail>.*)$", re.I)
_VISUAL_LABEL = re.compile(r"^\s*(?:镜头(?:调度)?|分镜|画面|动作|行动|拍摄|景别|运镜|场景|表演)\s*(?:\d+)?\s*[:：]")
_DIALOGUE_LABEL = re.compile(r"^\s*(?:(?:结尾|片尾|结束)字幕|(?:对白|对话|台词|旁白|独白|口播|字幕|文案)\s*(?:[:：]|$))")
_SPEAKER_LABEL = re.compile(r"^\s*[\w\u4e00-\u9fff·]{1,16}(?:\s*[（(][^）)\r\n]{1,30}[）)])?\s*[:：]")


def _fold(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _title_body(title, body):
    title = _text(title, "脚本标题", 200, required=True)
    body = _text(body, "脚本正文", 100000, multiline=True)
    if not body.strip():
        raise ValueError("请先填写脚本正文")
    return title, body


def _terms(values, *, explicit=True):
    """Exclude numbering/camera timestamps; every returned term is explainable."""
    result = []
    for value in values:
        value = _fold(value).strip(" \t\r\n#：:，,。.;；\"'（）()[]【】")
        if not value or len(value) > 60 or value in _STOP or not any(char.isalpha() for char in value):
            continue
        if len(value) < (1 if explicit and re.search(r"[\u4e00-\u9fff]", value) else 2):
            continue
        if value not in result:
            result.append(value)
        if len(result) == 40:
            break
    return result


def _extract_keywords(title, text):
    explicit = _KEYWORDS.findall(text)
    if explicit:
        return _terms(re.split(r"[,，、;；|/\s]+", " ".join(explicit)))
    source = _SPLIT_STOP.sub(" ", title + " " + text)
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9_-]{1,59}", source)
    # Long Chinese prose does not contain word separators. Short literal spans
    # allow a filename such as 门店外观.mov to match 拍摄门店外观, without claiming
    # that any visual analysis occurred. Full phrases rank above their subspans.
    phrases = _terms(chunks, explicit=False)
    grams = []
    for chunk in chunks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk) and len(chunk) > 2:
            for width in (4, 3, 2):
                grams.extend(chunk[index:index + width] for index in range(len(chunk) - width + 1))
    return _terms([*phrases, *grams], explicit=False)


def _section_title(text, index):
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    markdown = _MARKDOWN.match(first)
    if markdown:
        first = markdown.group(1)
    return first[:60] if first else f"段落 {index}"


def _section(title, text, index, heading, keywords):
    identity = hashlib.sha256(f"{index}\0{text}".encode("utf-8")).hexdigest()
    # Always reserve section numbering so long script titles cannot merge
    # distinct section categories after the 40-character limit.
    prefix = title[:14]
    suffix = f" · {index:02d} {heading}"
    category = prefix + suffix[:40 - len(prefix)]
    return dict(section_id=identity, title=heading, text=text,
                keywords=keywords, category_name=category)


def _seconds(value, number):
    parts = value.split(":")
    error = f"第 {number} 镜的时间范围不正确，请使用非负秒数或 分:秒 格式"
    # Bound numeric conversion independently of Python's integer-string limit.
    if len(value) > 30 or value.startswith("-"):
        raise ValueError(error)
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise ValueError(error) from None
    if any(part < 0 for part in numbers) or any(part >= 60 for part in numbers[1:]):
        raise ValueError(error)
    total = sum(part * 60 ** power for power, part in enumerate(reversed(numbers)))
    return int(total) if total.is_integer() else total


def _timed_heading(line):
    """Recognize numbered shot ranges, never a bare date or spoken timecode."""
    line = unicodedata.normalize("NFKC", line.rstrip("\r\n"))
    markdown = _MARKDOWN.match(line)
    numbered = _SHOT_NUMBER.match(markdown.group(1) if markdown else line)
    if not numbered:
        return None
    timed = _SHOT_TIME.match(numbered.group("rest"))
    if not timed:
        return None
    # Plain numeric ranges without a unit are ambiguous list items or dates.
    if not (timed.group("start_unit") or timed.group("end_unit") or ":" in timed.group("start") or ":" in timed.group("end")):
        return None
    tail = timed.group("tail")
    if tail and not timed.group("end_unit") and tail[0] not in " \t:：|｜、":
        return None
    number = numbered.group("number")
    start, end = _seconds(timed.group("start"), number), _seconds(timed.group("end"), number)
    if end < start:
        raise ValueError(f"第 {number} 镜的结束时间早于开始时间，请修正时间范围")
    heading = tail.strip(" \t:：|｜、")[:60] or f"镜头 {number}"
    return dict(shot_number=number, time_range=numbered.group("rest")[:timed.start("tail")].strip(),
                start_seconds=start, end_seconds=end, title=heading)


def _shooting_content(text):
    """Keep direction/dialogue views separate; the original text remains intact."""
    visual, dialogue = [], []
    mode = "visual"
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            # A paragraph break ends an unlabeled dialogue continuation.
            mode = "visual"
            continue
        if _KEYWORDS.match(stripped):
            continue
        if _VISUAL_LABEL.match(line) or stripped.startswith(("（", "(")):
            mode = "visual"
        elif _DIALOGUE_LABEL.match(line):
            mode = "dialogue"
        elif _SPEAKER_LABEL.match(line) or stripped.startswith(("“", '"', "「", "『")):
            # A speaker's paragraph does not turn the following, unlabelled
            # action paragraph into dialogue. Explicit subtitle/voiceover
            # labels above may introduce a multi-line text block instead.
            dialogue.append(line)
            mode = "visual"
            continue
        (visual if mode == "visual" else dialogue).append(line)
    return "".join(visual).strip(), "".join(dialogue).strip()


def _timed_sections(title, body, lines):
    shots, position = [], 0
    for line in lines:
        heading = _timed_heading(line)
        if heading:
            shots.append((position, position + len(line), heading))
        position += len(line)
    if not shots:
        return None
    if len(shots) > MAX_SECTIONS:
        raise ValueError("脚本最多拆分为 50 个分镜或段落，请分批整理")
    context = body[:shots[0][0]]
    sections = []
    for index, (start, content_start, heading) in enumerate(shots, 1):
        end = shots[index][0] if index < len(shots) else len(body)
        content = body[content_start:end]
        visual, dialogue = _shooting_content(content)
        # Explicit keywords outrank all inferred terms. Otherwise the shooting
        # directions provide evidence, not a character's unrelated dialogue or
        # document-level character/genre metadata.
        explicit = [line for line in content.splitlines() if _KEYWORDS.match(line.strip())]
        keywords = _extract_keywords("", "\n".join(explicit)) if explicit else _extract_keywords("", visual)
        if not keywords and not visual and not explicit:
            keywords = _extract_keywords(heading["title"], "")
        section = _section(title, body[0 if index == 1 else start:end], index, heading["title"], keywords)
        section.update(heading, format="timed_shooting", visual_text=visual, dialogue_text=dialogue,
                       context_text=context)
        sections.append(section)
    return sections


def parse_script(title, body):
    """Split shooting ranges or ordinary sections, preserving every body character.

    A numbered, explicit time range takes precedence over the inner 镜头/画面
    labels. Timed sections also expose shot/time/direction/dialogue/context
    fields for preview; these are derived views, not changes to the script.
    """
    title, body = _title_body(title, body)
    lines = body.splitlines(keepends=True)
    timed = _timed_sections(title, body, lines)
    if timed is not None:
        return timed
    starts = []
    position = 0
    for line in lines:
        if _MARKDOWN.match(line.rstrip("\r\n")) or _HEADING.match(line):
            starts.append(position)
        position += len(line)
    if not starts:
        starts = [match.end() for match in re.finditer(r"\r?\n[ \t]*(?:\r?\n)+", body) if match.end() < len(body)]
        if not starts and len(lines) > 1:
            # A keyword label belongs to the immediately preceding prose line.
            position = 0
            for line in lines:
                if position and line.strip() and not _KEYWORDS.match(line.strip()):
                    starts.append(position)
                position += len(line)
    if starts and not body[:starts[0]].strip():
        starts[0] = 0
    bounds = sorted({0, *starts, len(body)})
    pieces = [body[first:last] for first, last in zip(bounds, bounds[1:])]
    # Preserve whitespace chunks by attaching them, never silently clipping body.
    texts = []
    for piece in pieces:
        if piece.strip() or not texts:
            texts.append(piece)
        else:
            texts[-1] += piece
    if len(texts) > MAX_SECTIONS:
        raise ValueError("脚本最多拆分为 50 个分镜或段落，请分批整理")
    sections = []
    for index, text in enumerate(texts, 1):
        heading = _section_title(text, index)
        sections.append(_section(title, text, index, heading, _extract_keywords(heading, text)))
    return sections


class ScriptOrganizer:
    def __init__(self, service):
        self.service = service
        self.db_path = service.db_path
        self.library_key = root_key(service.root)

    def _check_root(self):
        if root_key(self.service.root) != self.library_key or self.service.collaboration.library_key != self.library_key or self.service.categories.library_key != self.library_key:
            raise ValueError("素材库已切换，请在当前素材库重新打开素材整理器")

    def _connect(self, *, readonly=False):
        self._check_root()
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        if readonly:
            db.execute("PRAGMA query_only=ON")
        return db

    def _script(self, db, script_id, expected_heads=None):
        _identity(script_id, "脚本 ID")
        record = self.service.collaboration._get(db, script_id)
        if record is None or record["kind"] != "script":
            raise ValueError("脚本不存在或不属于当前素材库")
        if record["archived"]:
            raise ValueError("脚本已归档，请恢复后再整理")
        if len(record["heads"]) != 1:
            raise CollaborationConflictError("脚本存在并行版本，请先合并脚本后重新生成整理计划")
        if expected_heads is not None and _heads(expected_heads) != record["heads"]:
            raise CollaborationConflictError("脚本已被修改，请保留当前选择并重新载入脚本")
        return record

    def scripts(self, query="", offset=0, limit=30):
        self._check_root()
        return self.service.collaboration.list_records("script", query=query, offset=offset, limit=limit)

    def search(self, query="", offset=0, limit=30):
        self._check_root()
        query = _text(query, "搜索内容", 500)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("素材分页参数不正确")
        return self.service.page(query=query, offset=offset, limit=limit)

    @staticmethod
    def _section_terms(section):
        if not isinstance(section, dict):
            raise ValueError("分镜格式不正确")
        if "keywords" not in section:
            return _extract_keywords(str(section.get("title", "")), str(section.get("text", "")))
        keywords = section["keywords"]
        if not isinstance(keywords, list) or len(keywords) > 40 or any(not isinstance(term, str) or len(term) > 60 for term in keywords):
            raise ValueError("关键词须为最多 40 项、每项不超过 60 字的列表")
        return _terms(keywords)

    @staticmethod
    def _score(row, categories, terms, bound):
        score, reasons = 0, []
        for label, source, weight in (("文件名", row["name"], 8), ("路径", row["relative_path"], 4),
                                      ("分类", "\n".join(category["name"] for category in categories), 6)):
            normalized = _fold(source)
            matches = sorted((term for term in terms if term in normalized), key=lambda term: (-len(term), term))
            evidence = []
            for term in matches:
                if not any(term in longer for longer in evidence):
                    evidence.append(term)
            if evidence:
                score += sum(weight * (1 + min(len(term), 10) / 10) for term in evidence)
                reasons.append(label + "包含" + "、".join(f"“{term}”" for term in evidence[:4]))
        if row["asset_id"] in bound:
            reasons.append("已绑定当前脚本（请确认是否适合本段）")
            score += 0.1
        return round(score, 3), reasons

    def _match_many(self, db, sections, bound, limit):
        terms = [self._section_terms(section) for section in sections]
        best, totals = [[] for _ in sections], [0 for _ in sections]
        if not any(terms) and not bound:
            return [(items, 0) for items in best]
        # Stream one local metadata snapshot for all sections; bound memory is
        # O(256 assets + sections * limit), independent of total library size.
        cursor = db.execute("SELECT * FROM assets WHERE root_key=? ORDER BY relative_path COLLATE NOCASE,asset_id", (self.library_key,))
        while rows := cursor.fetchmany(256):
            identities = [row["asset_id"] for row in rows]
            categories = {identity: [] for identity in identities}
            placeholders = ",".join("?" for _ in identities)
            for category in db.execute("""SELECT m.asset_id,c.name FROM category_memberships m JOIN category_entities c
                ON c.library_key=m.library_key AND c.category_id=m.category_id
                WHERE c.library_key=? AND c.archived=0 AND m.asset_id IN (""" + placeholders + ") ORDER BY c.name_key,c.category_id", [self.library_key, *identities]):
                categories[category["asset_id"]].append(dict(category))
            for row in rows:
                for index, keywords in enumerate(terms):
                    score, reasons = self._score(row, categories[row["asset_id"]], keywords, bound)
                    if score <= 0:
                        continue
                    totals[index] += 1
                    candidate = dict(row)
                    candidate.update(score=score, reasons=reasons)
                    best[index].append(candidate)
                    best[index].sort(key=lambda item: (-item["score"], _fold(item["relative_path"]), item["asset_id"]))
                    if len(best[index]) > limit:
                        best[index].pop()
        self._hydrate(db, best)
        return list(zip(best, totals))

    def _hydrate(self, db, best):
        all_ids = sorted({item["asset_id"] for items in best for item in items})
        hydrated = self.service.categories.for_assets(all_ids, db=db)
        thumbs = {}
        for start in range(0, len(all_ids), 400):
            chunk = all_ids[start:start + 400]
            for row in db.execute("SELECT * FROM thumbnails WHERE asset_id IN (" + ",".join("?" for _ in chunk) + ")", chunk):
                thumbs[row["asset_id"]] = row
        for items in best:
            for item in items:
                thumb = thumbs.get(item["asset_id"])
                item["thumbnail"] = thumb["cache_path"] if thumb and thumb["file_hash"] == item["file_hash"] else ""
                item["thumbnail_status"] = thumb["status"] if thumb and thumb["file_hash"] == item["file_hash"] else "pending"
                item["categories"] = hydrated[item["asset_id"]]

    def _match_page(self, db, section, bound, limit, offset):
        terms = self._section_terms(section)
        if not terms and not bound:
            return [], 0

        def score(identity, name, path, category_names):
            row = {"asset_id": identity, "name": name, "relative_path": path}
            categories = [{"name": category_names}] if category_names else []
            return self._score(row, categories, terms, bound)[0]

        # Later pages must not retain the whole prefix in Python. Let SQLite
        # spill its ordered working set to local temporary storage with a
        # bounded cache, while retaining this same read-only index snapshot.
        # Only the requested <=100 assets are hydrated into Python objects.
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA temp.cache_size=-2048")
        db.create_function("organizer_score", 4, score, deterministic=True)
        db.create_function("organizer_fold", 1, _fold, deterministic=True)
        ranked = """WITH ranked AS (
            SELECT a.*,organizer_score(a.asset_id,a.name,a.relative_path,(
                SELECT group_concat(name,char(10)) FROM (
                    SELECT c.name FROM category_memberships m JOIN category_entities c
                    ON c.library_key=m.library_key AND c.category_id=m.category_id
                    WHERE m.library_key=a.root_key AND m.asset_id=a.asset_id AND c.archived=0
                    ORDER BY c.name_key,c.category_id
                )
            )) AS score FROM assets a WHERE a.root_key=?
        ) """
        total = db.execute(ranked + "SELECT count(*) FROM ranked WHERE score>0", (self.library_key,)).fetchone()[0]
        if offset >= total:
            return [], total
        items = [dict(row) for row in db.execute(ranked + """SELECT * FROM ranked WHERE score>0
            ORDER BY score DESC,organizer_fold(relative_path),asset_id LIMIT ? OFFSET ?""",
            (self.library_key, limit, offset))]
        self._hydrate(db, [items])
        for item in items:
            item["score"], item["reasons"] = self._score(item, item["categories"], terms, bound)
        return items, total

    def match(self, section, script_id=None, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("候选数量应为 1 至 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("候选分页偏移量不正确")
        with closing(self._connect(readonly=True)) as db:
            db.execute("BEGIN")
            script = self._script(db, script_id) if script_id else None
            bound = set(script["asset_ids"] if script else [])
            if offset:
                return self._match_page(db, section, bound, limit, offset)
            return self._match_many(db, [section], bound, limit)[0]

    def plan(self, title, body, script_id=None, expected_heads=None):
        title, body = _title_body(title, body)
        sections = parse_script(title, body)
        with closing(self._connect(readonly=True)) as db:
            db.execute("BEGIN")
            script = self._script(db, script_id, expected_heads) if script_id else None
            if script and (script["title"] != title or script["body"] != body):
                raise CollaborationConflictError("脚本内容与保存版本不一致，请保存脚本或作为新脚本整理")
            if not script and expected_heads not in (None, [], ()):
                raise ValueError("新脚本不应带有其他脚本的版本信息")
            matches = self._match_many(db, sections, set(script["asset_ids"] if script else []), 20)
        for section, (candidates, total) in zip(sections, matches):
            section.update(candidates=candidates, candidate_total=total, selected_ids=[])
        return dict(title=title, body=body, script_id=script_id, expected_heads=list(script["heads"] if script else []),
                    sections=sections, library_key=self.library_key, _target_script_id=script_id or uuid.uuid4().hex,
                    _source_title=title, _source_body=body)

    def apply(self, plan, selections):
        """Atomically add reviewed memberships/bindings; exact reapply is a no-op.

        On success only, the mutable plan receives script_id and expected_heads.
        UI callers can alternatively copy these fields from the returned script.
        """
        self._check_root()
        if not isinstance(plan, dict) or plan.get("library_key") != self.library_key:
            raise ValueError("整理计划不属于当前素材库，请重新生成")
        title, body = _title_body(plan.get("title"), plan.get("body"))
        if title != plan.get("_source_title") or body != plan.get("_source_body"):
            raise ValueError("生成计划后脚本内容已改变，请保留选择并重新生成计划")
        section_ids = {section["section_id"] for section in parse_script(title, body)}
        if not isinstance(selections, list) or len(selections) > MAX_SECTIONS:
            raise ValueError("整理选择须为最多 50 个分镜的列表")
        groups, seen, pairs = [], set(), set()
        for selection in selections:
            if not isinstance(selection, dict) or set(selection) != {"section_id", "category_name", "asset_ids"}:
                raise ValueError("整理选择格式不正确")
            identity = selection["section_id"]
            if not isinstance(identity, str) or identity not in section_ids or identity in seen:
                raise ValueError("分镜不存在或重复，请重新生成整理计划")
            seen.add(identity)
            assets = selection["asset_ids"]
            if not isinstance(assets, list) or len(assets) > MAX_PAIRS:
                raise ValueError("每组素材必须为最多 1000 项的列表")
            assets = sorted({_identity(asset, "素材 ID", digest=True) for asset in assets})
            if not assets:
                continue
            category = category_data({"name": selection["category_name"]})["name"]
            if category in ("全部", "未分类", "全部素材"):
                raise ValueError("分类名称不能使用“全部”或“未分类”等筛选名称")
            key = _name_key(category)
            pairs.update((key, asset) for asset in assets)
            if len(pairs) > MAX_PAIRS:
                raise ValueError("一次最多整理 1000 个素材与分类关联，请分批整理")
            groups.append((category, key, assets))
        if not groups:
            raise ValueError("请至少为一个分镜勾选素材后再应用")
        selected = sorted({asset for _, asset in pairs})
        target_id = _identity(plan.get("_target_script_id"), "脚本 ID")
        script_id = plan.get("script_id")
        if script_id is not None and script_id != target_id:
            raise ValueError("整理计划的脚本标识已改变，请重新生成")
        expected = _heads(plan.get("expected_heads"))
        created = changed = 0
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = self._script(db, script_id, expected) if script_id else None
            if current and (current["title"] != title or current["body"] != body):
                raise CollaborationConflictError("脚本内容已改变，请重新载入脚本后整理")
            if not current and self.service.collaboration._get(db, target_id):
                raise CollaborationConflictError("此计划已创建脚本，请重新载入该脚本后整理")
            if not current and expected:
                raise ValueError("新脚本不能引用已有版本")
            bindings = sorted(set(current["asset_ids"] if current else []) | set(selected))
            if len(bindings) > 1000:
                raise ValueError("脚本绑定素材合计不能超过 1000 个，请拆分脚本后整理")
            for start in range(0, len(selected), 400):
                chunk = selected[start:start + 400]
                count = db.execute("SELECT count(*) FROM assets WHERE root_key=? AND asset_id IN (" + ",".join("?" for _ in chunk) + ")", [self.library_key, *chunk]).fetchone()[0]
                if count != len(chunk):
                    raise ValueError("有素材不在本机当前素材库中，请先同步或扫描后重新选择")
            # A conflict's deterministic preview shows only one head. Inspect
            # every active head name before reusing/creating categories, so a
            # concurrent rename cannot accidentally produce a duplicate.
            conflicted_names = set()
            for row in db.execute("SELECT category_id,heads_json FROM category_entities WHERE library_key=? AND archived=0", (self.library_key,)):
                heads = json.loads(row["heads_json"])
                if len(heads) < 2:
                    continue
                for version in db.execute("SELECT revision,data_json FROM category_revisions WHERE library_key=? AND category_id=?",
                                          (self.library_key, row["category_id"])):
                    if version["revision"] in heads:
                        value = json.loads(version["data_json"])
                        if not value["archived"]:
                            conflicted_names.add(_name_key(value["name"]))
            existing = {}
            for category, key, _ in groups:
                if key in existing:
                    continue
                if key in conflicted_names:
                    raise ValueError(f"分类“{category}”存在并行版本，请先合并分类")
                rows = db.execute("SELECT * FROM category_entities WHERE library_key=? AND archived=0 AND name_key=?", (self.library_key, key)).fetchall()
                if len(rows) > 1:
                    raise ValueError(f"分类“{category}”有多个同名记录，请先在管理分类中处理重名")
                record = self.service.categories._record(rows[0]) if rows else None
                if record and record["conflict_count"]:
                    raise ValueError(f"分类“{category}”存在并行版本，请先合并分类")
                existing[key] = record
            for category, key, _ in groups:
                if existing[key] is None:
                    existing[key] = self.service.categories.save({"name": category}, db=db)
                    created += 1
            for _, key, assets in groups:
                changed += self.service.categories.assign(assets, [existing[key]["category_id"]], db=db)["changed"]
            added = len(set(bindings) - set(current["asset_ids"] if current else []))
            if current and not added:
                script = current
            else:
                data = {"asset_ids": bindings} if current else dict(title=title, body=body, asset_ids=bindings, author=self.service.device_id)
                script = self.service.collaboration.save("script", data, entity_id=target_id, expected_heads=expected, db=db)
            result = dict(script=script, categories_created=created, memberships_changed=changed,
                          assets_bound=added, sections_applied=len(groups))
        # Never advance the snapshot until every local write/outbox event commits.
        plan["script_id"] = script["entity_id"]
        plan["expected_heads"] = list(script["heads"])
        return result
