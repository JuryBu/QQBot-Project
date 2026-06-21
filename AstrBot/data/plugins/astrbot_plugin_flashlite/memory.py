"""
Memory 记忆系统 — 参照 mcp-memory-store 架构改造
主模型自主管理的长期持久化记忆

改造要点（对齐 mcp-memory-store）：
- SQLite FTS5 全文索引替代 LIKE 模糊搜索
- CJK 子串/前缀匹配（Python 版 Fuse.js 策略）
- workspace 群号/QQ号隔离
- 去重检测（checkDuplicates）
- 三级 depth 返回（index/summary/full）
- autoSummary 异步 Flash Lite 生成
- pinned 置顶 + 时间范围过滤
- grep 全文搜索
- 概览模式（统计+topTags+pinned列表）

文档: Plan_1_memory.md
"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from astrbot.api import logger

# ============================================================
# 路径配置
# ============================================================
MEMORY_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Memory")
)
MEMORY_DB = os.path.join(MEMORY_DIR, "memory.db")


# ============================================================
# CJK 检测工具 — 对齐 search.ts tokenize()
# ============================================================
_CJK_RANGES = [
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0x3400, 0x4DBF),    # 扩展 A
    (0x20000, 0x2A6DF),  # 扩展 B
    (0x2A700, 0x2B73F),  # 扩展 C
    (0x2B740, 0x2B81F),  # 扩展 D
    (0xF900, 0xFAFF),    # 兼容区
    (0x3000, 0x303F),    # CJK 符号
    (0x3040, 0x309F),    # 平假名
    (0x30A0, 0x30FF),    # 片假名
    (0xAC00, 0xD7AF),    # 韩文音节
]


def _is_cjk_char(ch: str) -> bool:
    """判断单个字符是否为 CJK"""
    cp = ord(ch)
    return any(start <= cp <= end for start, end in _CJK_RANGES)


def _has_cjk(text: str) -> bool:
    """判断文本是否包含 CJK 字符"""
    return any(_is_cjk_char(ch) for ch in text)


def _tokenize(text: str) -> List[str]:
    """智能分词
    - CJK 文本: 2-gram
    - 非 CJK 文本: 空格分词 + 小写
    参照 search.ts tokenize()
    """
    text = text.lower().strip()
    if not text:
        return []

    if _has_cjk(text):
        # CJK: 2-gram 分词（连续 CJK 字符取两两组合）
        tokens = []
        cjk_buf = []
        for ch in text:
            if _is_cjk_char(ch):
                cjk_buf.append(ch)
            else:
                if cjk_buf:
                    s = "".join(cjk_buf)
                    if len(s) >= 2:
                        for i in range(len(s) - 1):
                            tokens.append(s[i:i+2])
                    elif len(s) == 1:
                        tokens.append(s)
                    cjk_buf = []
                # 非 CJK 按空格分
                if ch.strip():
                    pass  # 累积到下面处理
        if cjk_buf:
            s = "".join(cjk_buf)
            if len(s) >= 2:
                for i in range(len(s) - 1):
                    tokens.append(s[i:i+2])
            elif len(s) == 1:
                tokens.append(s)

        # 也提取非 CJK 单词
        non_cjk = re.sub(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]+', ' ', text)
        for w in non_cjk.split():
            if len(w) >= 2:
                tokens.append(w)
        return tokens
    else:
        # 非 CJK: 空格分词
        return [w for w in text.split() if len(w) >= 1]


# ============================================================
# 混合搜索引擎 — Python 版 Fuse.js 策略
# 对齐 search.ts fuseSearch + substringMatchScore
# ============================================================
class MemorySearchEngine:
    """对齐 mcp-memory-store 的混合搜索策略"""

    @staticmethod
    def _substring_score(haystack: str, needle: str) -> float:
        """子串/前缀匹配 + rapidfuzz 模糊匹配评分
        策略: 精确子串 > 前缀匹配 > rapidfuzz 模糊 > 未命中
        - 精确子串命中 → 0.0
        - 前缀匹配(>=3字符) → 0.15
        - rapidfuzz 模糊匹配(>=70分) → 0.3~0.7
        - 未命中 → 1.0
        """
        h = haystack.lower()
        n = needle.lower()
        if n in h:
            return 0.0
        if len(n) >= 3 and h.startswith(n):
            return 0.15
        # rapidfuzz 模糊匹配（纯 C 扩展，速度极快）
        try:
            from rapidfuzz import fuzz
            ratio = fuzz.partial_ratio(n, h)
            if ratio >= 70:
                # 70分→0.7, 100分→0.3 的线性映射
                return 0.7 - (ratio - 70) / 100
        except ImportError:
            pass  # rapidfuzz 未安装时静默降级
        return 1.0

    @staticmethod
    def _entry_best_score(entry: Dict, token: str) -> float:
        """对 entry 的所有可搜索字段取最佳分数"""
        fields = [
            entry.get("title", ""),
            entry.get("search_summary", ""),
            " ".join(entry.get("tags", [])),
        ]
        scores = [MemorySearchEngine._substring_score(f, token) for f in fields if f]
        return min(scores) if scores else 1.0

    @staticmethod
    def search(
        entries: List[Dict],
        query: str,
        limit: int = 10,
    ) -> List[Dict]:
        """混合搜索
        对齐 search.ts fuseSearch:
        - 单词查询: 子串/前缀匹配评分
        - 多词查询: 覆盖率 0.7 + 平均质量 0.3
        """
        if not query or not entries:
            return entries[:limit]

        tokens = _tokenize(query)
        if not tokens:
            return entries[:limit]

        scored = []
        for entry in entries:
            if len(tokens) == 1:
                score = MemorySearchEngine._entry_best_score(entry, tokens[0])
            else:
                # 多词: 参照 search.ts multiWordScore
                hit_scores = []
                for t in tokens:
                    s = MemorySearchEngine._entry_best_score(entry, t)
                    if s < 1.0:
                        hit_scores.append(s)

                if not hit_scores:
                    score = 1.0
                else:
                    coverage = len(hit_scores) / len(tokens)
                    avg_quality = sum(hit_scores) / len(hit_scores)
                    score = 1.0 - coverage * 0.7 - (1.0 - avg_quality) * 0.3 * coverage

            if score < 0.8:  # 阈值过滤
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0])
        return [e for _, e in scored[:limit]]

    @staticmethod
    def check_duplicates(
        entries: List[Dict],
        title: str,
        summary: str = "",
        threshold: float = 0.5,
    ) -> List[Dict]:
        """去重检测 — 对齐 search.ts checkDuplicates
        
        改用分词覆盖率去重：将标题+摘要分词，逐token与entry匹配，
        覆盖率超过 threshold 即视为重复
        """
        check_text = f"{title} {summary}".strip()
        if not check_text:
            return []

        check_tokens = _tokenize(check_text)
        if not check_tokens:
            return []

        duplicates = []
        for entry in entries:
            entry_text = f"{entry.get('title', '')} {entry.get('search_summary', '')}".lower()
            hit = sum(1 for t in check_tokens if t in entry_text)
            coverage = hit / len(check_tokens)
            if coverage >= threshold:
                duplicates.append(entry)
        return duplicates


# ============================================================
# MemoryStore — 核心存储引擎
# 对齐 mcp-memory-store store.ts + query.ts + write.ts
# ============================================================
class MemoryStore:
    """Memory 记忆系统

    按工作区隔离（群号/QQ号/general），支持跨工作区搜索
    SQLite FTS5 全文索引 + 混合搜索引擎
    """

    def __init__(self, api_key: str = "", flash_lite_model: str = ""):
        os.makedirs(MEMORY_DIR, exist_ok=True)
        self._initialized = False
        self._api_key = api_key
        self._flash_lite_model = flash_lite_model
        self._search_engine = MemorySearchEngine()

    async def _ensure_db(self):
        """确保数据库和 FTS5 索引存在"""
        if self._initialized:
            return
        async with aiosqlite.connect(MEMORY_DB) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            # 主表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL DEFAULT 'general',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    tags TEXT DEFAULT '[]',
                    search_summary TEXT DEFAULT '',
                    auto_summary TEXT DEFAULT '',
                    source_pointer TEXT DEFAULT '',
                    pinned BOOLEAN DEFAULT FALSE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # 索引
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_workspace ON memories(workspace)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(updated_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_pinned ON memories(pinned)"
            )
            # FTS5 全文索引
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    title, content, search_summary, tags, auto_summary,
                    content=memories, content_rowid=rowid,
                    tokenize='unicode61 remove_diacritics 2'
                )
            """)
            # FTS5 触发器：插入
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, title, content, search_summary, tags, auto_summary)
                    VALUES (new.rowid, new.title, new.content, new.search_summary, new.tags, new.auto_summary);
                END
            """)
            # FTS5 触发器：更新
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, search_summary, tags, auto_summary)
                    VALUES ('delete', old.rowid, old.title, old.content, old.search_summary, old.tags, old.auto_summary);
                    INSERT INTO memories_fts(rowid, title, content, search_summary, tags, auto_summary)
                    VALUES (new.rowid, new.title, new.content, new.search_summary, new.tags, new.auto_summary);
                END
            """)
            # FTS5 触发器：删除
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, search_summary, tags, auto_summary)
                    VALUES ('delete', old.rowid, old.title, old.content, old.search_summary, old.tags, old.auto_summary);
                END
            """)
            await db.commit()
        self._initialized = True
        logger.info(f"MemoryStore 初始化完成 | DB: {MEMORY_DB}")

    # ========================
    # 写入 — 对齐 write.ts
    # ========================
    async def write(
        self,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
        workspace: str = "general",
        category: str = "general",
        source_pointer: str = "",
        pinned: bool = False,
        search_summary: str = "",
        check_duplicates: bool = True,
    ) -> Dict[str, Any]:
        """写入新记忆
        
        返回: {"id": ..., "duplicates": [...]} 
        对齐 write.ts: 自动去重检测 + autoSummary 异步生成
        """
        await self._ensure_db()

        # 生成 ID
        ts = int(time.time() * 1000)
        hash_suffix = hashlib.md5(f"{title}{ts}".encode()).hexdigest()[:6]
        mem_id = f"mem_{ts}_{hash_suffix}"
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        tags_json = json.dumps(tags or [], ensure_ascii=False)

        # 自动生成搜索摘要（如果未提供）
        if not search_summary:
            search_summary = f"{title} | {' '.join(tags or [])} | {content[:150]}"

        # 去重检测
        dup_result = []
        if check_duplicates:
            existing = await self._get_workspace_entries(workspace)
            dup_result = self._search_engine.check_duplicates(
                existing, title, search_summary
            )
            if dup_result:
                logger.info(f"Memory 去重检测: 发现 {len(dup_result)} 条疑似重复")

        # 内容大小限制 (15KB) — 对齐 write.ts
        if len(content.encode("utf-8")) > 15360:
            content = content[:12000] + "\n\n... (内容已截断，原文超过 15KB)"
            logger.warning(f"Memory 内容超过 15KB 限制，已截断: {title}")

        async with aiosqlite.connect(MEMORY_DB) as db:
            await db.execute(
                """INSERT INTO memories 
                   (id, workspace, title, content, category, tags, search_summary,
                    source_pointer, pinned, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mem_id, workspace, title, content, category, tags_json,
                 search_summary, source_pointer, pinned, now, now),
            )
            await db.commit()

        logger.info(f"Memory 写入: '{title}' → {mem_id} (ws={workspace})")

        # 异步触发 autoSummary 生成
        if self._api_key and self._flash_lite_model:
            asyncio.create_task(self._generate_auto_summary(mem_id, title, content))

        return {
            "id": mem_id,
            "duplicates": [
                {"id": d["id"], "title": d["title"]} for d in dup_result
            ],
        }

    async def _generate_auto_summary(self, mem_id: str, title: str, content: str):
        """异步生成 autoSummary — 对齐 write.ts triggerAutoSummary"""
        try:
            import aiohttp
            prompt = (
                f"请用一句话概括以下记忆内容的核心信息，用于搜索优化，"
                f"包含关键词和核心要点，不超过 200 字：\n\n"
                f"标题: {title}\n内容: {content[:3000]}"
            )
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._flash_lite_model}:generateContent?key={self._api_key}"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300},
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        summary = " ".join(p.get("text", "") for p in parts).strip()
                        if summary:
                            async with aiosqlite.connect(MEMORY_DB) as db:
                                await db.execute(
                                    "UPDATE memories SET auto_summary = ? WHERE id = ?",
                                    (summary, mem_id),
                                )
                                await db.commit()
                            logger.info(f"autoSummary 生成完成: {mem_id}")
        except Exception as e:
            logger.warning(f"autoSummary 生成失败: {e}")

    async def _get_workspace_entries(
        self, workspace: Optional[str] = None
    ) -> List[Dict]:
        """获取指定工作区的所有记忆索引"""
        async with aiosqlite.connect(MEMORY_DB) as db:
            db.row_factory = aiosqlite.Row
            if workspace:
                cursor = await db.execute(
                    "SELECT id, title, tags, search_summary, pinned FROM memories WHERE workspace = ?",
                    (workspace,),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, title, tags, search_summary, pinned FROM memories"
                )
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "search_summary": r["search_summary"] or "",
                "pinned": bool(r["pinned"]),
            }
            for r in rows
        ]

    # ========================
    # 查询 — 对齐 query.ts
    # ========================
    async def query(
        self,
        query: str = "",
        workspace: Optional[str] = None,
        scope: str = "workspace",  # workspace / global
        tags: Optional[List[str]] = None,
        grep: str = "",
        depth: str = "index",  # index / summary / full
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """搜索记忆 — 混合搜索 + FTS5 grep + 三级深度

        对齐 query.ts:
        - scope=global 时跨所有工作区搜索
        - grep 用 FTS5 MATCH 全文检索
        - depth 控制返回详细度
        - after/before 时间范围过滤
        """
        await self._ensure_db()

        # 概览模式（无参调用）
        if not query and not grep and not tags:
            return await self._overview(workspace)

        # 构建基础 SQL 条件
        conditions = []
        params = []

        # 工作区过滤
        if scope == "workspace" and workspace:
            conditions.append("workspace = ?")
            params.append(workspace)

        # 标签过滤
        if tags:
            for tag in tags:
                conditions.append("tags LIKE ?")
                params.append(f'%"{tag}"%')

        # 时间过滤
        if after:
            conditions.append("updated_at >= ?")
            params.append(after)
        if before:
            conditions.append("updated_at <= ?")
            params.append(before)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # grep 模式：FTS5 全文检索
        if grep:
            return await self._grep_search(grep, where, params, depth, limit)

        # 混合搜索模式
        async with aiosqlite.connect(MEMORY_DB) as db:
            db.row_factory = aiosqlite.Row
            select_fields = self._get_fields_for_depth(depth)
            cursor = await db.execute(
                f"SELECT {select_fields} FROM memories {where} "
                f"ORDER BY pinned DESC, updated_at DESC LIMIT ?",
                params + [limit * 3],  # 多取几条供搜索引擎筛选
            )
            rows = await cursor.fetchall()

        entries = [self._row_to_dict(r, depth) for r in rows]

        # 混合搜索引擎筛选排序
        if query:
            entries = self._search_engine.search(entries, query, limit)
        else:
            entries = entries[:limit]

        return {"results": entries, "total": len(entries)}

    async def _grep_search(
        self, grep: str, extra_where: str, extra_params: list,
        depth: str, limit: int,
    ) -> Dict[str, Any]:
        """FTS5 全文检索 — 对齐 search.ts grepInEntries"""
        async with aiosqlite.connect(MEMORY_DB) as db:
            db.row_factory = aiosqlite.Row
            # FTS5 MATCH 搜索
            fts_query = grep.replace('"', '""')
            sql = (
                f"SELECT m.* FROM memories m "
                f"JOIN memories_fts f ON m.rowid = f.rowid "
                f"WHERE memories_fts MATCH ? "
            )
            params = [f'"{fts_query}"']
            if extra_where:
                # 拼接额外条件（去掉 WHERE 前缀）
                extra_cond = extra_where.replace("WHERE ", "AND ", 1)
                sql += extra_cond
                params.extend(extra_params)
            sql += f" ORDER BY rank LIMIT ?"
            params.append(limit)

            try:
                cursor = await db.execute(sql, params)
                rows = await cursor.fetchall()
            except Exception:
                # FTS5 MATCH 语法错误时降级为 LIKE
                logger.warning(f"FTS5 MATCH 失败，降级为 LIKE: {grep}")
                select_fields = self._get_fields_for_depth(depth)
                like_q = f"%{grep}%"
                cursor = await db.execute(
                    f"SELECT {select_fields} FROM memories "
                    f"WHERE content LIKE ? {extra_where.replace('WHERE', 'AND') if extra_where else ''} "
                    f"LIMIT ?",
                    [like_q] + extra_params + [limit],
                )
                rows = await cursor.fetchall()

        return {
            "results": [self._row_to_dict(r, depth) for r in rows],
            "total": len(rows),
            "mode": "grep",
        }

    async def _overview(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """概览模式 — 对齐 query.ts handleOverview"""
        async with aiosqlite.connect(MEMORY_DB) as db:
            # 总数
            if workspace:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM memories WHERE workspace = ?", (workspace,)
                )
            else:
                cursor = await db.execute("SELECT COUNT(*) FROM memories")
            total = (await cursor.fetchone())[0]

            # 工作区分布
            cursor = await db.execute(
                "SELECT workspace, COUNT(*) FROM memories GROUP BY workspace"
            )
            by_workspace = {r[0]: r[1] for r in await cursor.fetchall()}

            # pinned 记忆
            db.row_factory = aiosqlite.Row
            if workspace:
                cursor = await db.execute(
                    "SELECT id, title, tags FROM memories WHERE pinned = 1 AND workspace = ?",
                    (workspace,),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, title, tags FROM memories WHERE pinned = 1"
                )
            pinned = [
                {"id": r["id"], "title": r["title"], "tags": json.loads(r["tags"])}
                for r in await cursor.fetchall()
            ]

            # Top tags
            cursor = await db.execute("SELECT tags FROM memories")
            tag_counter: Dict[str, int] = {}
            for row in await cursor.fetchall():
                for tag in json.loads(row[0] if row[0] else "[]"):
                    tag_counter[tag] = tag_counter.get(tag, 0) + 1
            top_tags = sorted(tag_counter.items(), key=lambda x: -x[1])[:15]

        return {
            "mode": "overview",
            "total": total,
            "by_workspace": by_workspace,
            "pinned": pinned,
            "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        }

    def _get_fields_for_depth(self, depth: str) -> str:
        """根据 depth 确定 SELECT 字段"""
        if depth == "index":
            return "id, workspace, title, tags, search_summary, pinned, updated_at"
        elif depth == "summary":
            return "id, workspace, title, tags, search_summary, category, pinned, created_at, updated_at"
        else:
            return "*"

    def _row_to_dict(self, row, depth: str = "index") -> Dict[str, Any]:
        """将 Row 转为 dict，根据 depth 控制字段"""
        # sqlite3.Row 不支持 .get()，先转 dict
        r = dict(row)
        base = {
            "id": r["id"],
            "workspace": r["workspace"],
            "title": r["title"],
            "tags": json.loads(r["tags"]) if r.get("tags") else [],
            "summary": r.get("search_summary", "") or "",
            "pinned": bool(r.get("pinned", False)),
            "updated_at": r.get("updated_at", ""),
        }
        if depth == "summary":
            base["category"] = r.get("category", "general")
            base["created_at"] = r.get("created_at", "")
            content = r.get("content", "")
            base["line_count"] = content.count("\n") + 1 if content else 0
            base["size_bytes"] = len(content.encode("utf-8")) if content else 0
        elif depth == "full":
            base["content"] = r.get("content", "")
            base["category"] = r.get("category", "general")
            base["auto_summary"] = r.get("auto_summary", "")
            base["source_pointer"] = r.get("source_pointer", "")
            base["created_at"] = r.get("created_at", "")
        return base

    # ========================
    # 读取 — 对齐 query.ts memory_read
    # ========================
    async def read(
        self, mem_id: str, workspace: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """读取完整记忆内容"""
        await self._ensure_db()

        async with aiosqlite.connect(MEMORY_DB) as db:
            db.row_factory = aiosqlite.Row
            if workspace:
                cursor = await db.execute(
                    "SELECT * FROM memories WHERE id = ? AND workspace = ?",
                    (mem_id, workspace),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM memories WHERE id = ?", (mem_id,)
                )
            row = await cursor.fetchone()

        if not row:
            return None

        return self._row_to_dict(row, "full")

    # ========================
    # 更新 — 对齐 update.ts
    # ========================
    async def update(
        self,
        mem_id: str,
        content: Optional[str] = None,
        append: Optional[str] = None,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        pinned: Optional[bool] = None,
        search_summary: Optional[str] = None,
        category: Optional[str] = None,
        workspace: Optional[str] = None,
    ) -> bool:
        """更新记忆"""
        await self._ensure_db()

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        updates = ["updated_at = ?"]
        params: list = [now]

        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if append is not None:
            updates.append("content = content || '\n\n---\n' || ?")
            params.append(f"[{now}]\n{append}")
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if tags is not None:
            updates.append("tags = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if pinned is not None:
            updates.append("pinned = ?")
            params.append(pinned)
        if search_summary is not None:
            updates.append("search_summary = ?")
            params.append(search_summary)
        if category is not None:
            updates.append("category = ?")
            params.append(category)

        params.append(mem_id)
        where_clause = "WHERE id = ?"
        if workspace:
            where_clause += " AND workspace = ?"
            params.append(workspace)

        async with aiosqlite.connect(MEMORY_DB) as db:
            cursor = await db.execute(
                f"UPDATE memories SET {', '.join(updates)} {where_clause}",
                params,
            )
            await db.commit()
            return cursor.rowcount > 0

    # ========================
    # 删除
    # ========================
    async def delete(
        self, mem_id: str, workspace: Optional[str] = None
    ) -> bool:
        """删除记忆"""
        await self._ensure_db()

        async with aiosqlite.connect(MEMORY_DB) as db:
            if workspace:
                cursor = await db.execute(
                    "DELETE FROM memories WHERE id = ? AND workspace = ?",
                    (mem_id, workspace),
                )
            else:
                cursor = await db.execute(
                    "DELETE FROM memories WHERE id = ?", (mem_id,)
                )
            await db.commit()
            return cursor.rowcount > 0

    # ========================
    # 统计
    # ========================
    async def stats(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """获取记忆统计（兼容旧接口）"""
        return await self._overview(workspace)

