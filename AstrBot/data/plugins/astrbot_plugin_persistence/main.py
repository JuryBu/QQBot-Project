import asyncio
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 数据库路径：在项目根目录的 QQ_data/ 下
# __file__ = AstrBot/data/plugins/astrbot_plugin_persistence/main.py
# 需要 4 层 .. 到 AstrBotLauncher-0.1.5.6/，再加 QQ_data/
DB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "QQ_data")
)
DB_PATH = os.path.join(DB_DIR, "messages.db")
IMAGE_DIR = os.path.join(DB_DIR, "images")


@register(
    "astrbot_plugin_persistence",
    "BossLady",
    "全量 QQ 消息持久化插件。拦截所有消息并写入 SQLite，支持撤回标记、多窗口并发写入、冷热数据管理。",
    "1.0.0",
)
class PersistencePlugin(Star):
    """全量 QQ 消息持久化插件

    Design Docs: QQBotPlan/Plan_1_data.md + Plan_1_gaps.md GAP2
    Table Schema: qq_messages + checkpoint_history
    """

    # 异步写入队列大小
    QUEUE_MAX_SIZE = 1000

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # 从 AstrBotConfig 读取配置，有 fallback 默认值
        self._raw_config = config or {}
        self.BATCH_SIZE = self._cfg("batch_size", 20)
        self.BATCH_TIMEOUT = float(self._cfg("batch_timeout_seconds", 2.0))
        self._hot_data_days = self._cfg("hot_data_days", 7)
        self._cold_data_days = self._cfg("cold_data_days", 30)
        self._enable_cold_cleanup = self._cfg("enable_cold_cleanup", True)

        # 图片缓存配置
        self._enable_image_cache = self._cfg("enable_image_cache", True)
        self._image_cache_max_mb = self._cfg("image_cache_max_mb", 500)
        self._image_dir = IMAGE_DIR

        self._db: Optional[aiosqlite.Connection] = None
        self._write_queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX_SIZE)
        self._writer_task: Optional[asyncio.Task] = None
        self._stats = {
            "total_written": 0,
            "total_recalled": 0,
            "errors": 0,
            "last_write_latency_ms": 0.0,
        }
        logger.info(f"PersistencePlugin 初始化 (batch={self.BATCH_SIZE}, timeout={self.BATCH_TIMEOUT}s)")

    def _cfg(self, key: str, default=None):
        """安全读取配置值，兼容 AstrBotConfig 和 dict"""
        if self._raw_config is None:
            return default
        if hasattr(self._raw_config, "get"):
            return self._raw_config.get(key, default)
        return getattr(self._raw_config, key, default)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后初始化数据库和写入线程"""
        await self._init_db()
        # 创建图片缓存目录
        if self._enable_image_cache:
            os.makedirs(self._image_dir, exist_ok=True)
            logger.info(f"图片缓存目录: {self._image_dir}")
        self._writer_task = asyncio.create_task(self._batch_writer())
        # 启动定时分级清理任务
        self._cleanup_task: Optional[asyncio.Task] = asyncio.create_task(self._periodic_cleanup())
        logger.info(f"PersistencePlugin 数据库就绪: {DB_PATH}")

    async def _init_db(self):
        """初始化 SQLite 数据库和表结构"""
        os.makedirs(DB_DIR, exist_ok=True)
        self._db = await aiosqlite.connect(DB_PATH)

        # 启用 WAL 模式（并发友好）
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA cache_size=-8000")  # 8MB cache

        # 创建主消息表
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS qq_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_type TEXT NOT NULL,
                window_id TEXT NOT NULL,
                message_id TEXT,
                sender_id TEXT NOT NULL,
                sender_name TEXT,
                content_text TEXT,
                content_raw TEXT,
                has_image INTEGER DEFAULT 0,
                image_urls TEXT,
                extra_data TEXT,
                is_recalled INTEGER DEFAULT 0,
                recalled_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
                round_id TEXT,
                step_id TEXT,
                receive_seq INTEGER
            )
        """)

        # 迁移：为旧表添加 extra_data 列
        try:
            await self._db.execute("ALTER TABLE qq_messages ADD COLUMN extra_data TEXT")
            logger.info("数据库迁移：已添加 extra_data 列")
        except Exception:
            pass  # 列已存在

        # === F4.1 迁移：为旧表补 round_id/step_id/receive_seq 列（S4 rebuild 前提）===
        # 幂等：先 PRAGMA table_info 查现有列，缺哪列才 ALTER，防重复迁移报 duplicate column。
        # 旧行这三列留 NULL（不影响现有查询）；S4 rebuild 用 (created_at_ms, receive_seq) 排序回填。
        await self._migrate_add_columns(
            "qq_messages",
            {
                "round_id": "TEXT",
                "step_id": "TEXT",
                "receive_seq": "INTEGER",
            },
        )

        # 创建 CHECKPOINT 压缩历史表
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS checkpoint_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_type TEXT NOT NULL,
                window_id TEXT NOT NULL,
                compressed_content TEXT NOT NULL,
                original_msg_range_start INTEGER,
                original_msg_range_end INTEGER,
                compression_ratio REAL,
                token_estimate INTEGER,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
            )
        """)

        # 创建索引
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_window 
            ON qq_messages (window_type, window_id, created_at)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_sender 
            ON qq_messages (sender_id)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_id 
            ON qq_messages (message_id)
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_cp_window 
            ON checkpoint_history (window_type, window_id, created_at)
        """)

        await self._db.commit()
        logger.info("数据库表结构初始化完成")

    async def _migrate_add_columns(self, table: str, columns: Dict[str, str]):
        """幂等迁移：为已有表补列。

        用 PRAGMA table_info 读出当前列集合，只对缺失的列执行 ALTER TABLE ADD COLUMN，
        避免对已迁移过的库重复 ALTER 报 "duplicate column name"。

        Args:
            table: 目标表名（须为可信常量，不接受外部输入）
            columns: {列名: SQLite 类型}，新增列均为 NULL 允许（无 NOT NULL/DEFAULT）
        """
        try:
            cursor = await self._db.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            existing = {row[1] for row in rows}  # row[1] = 列名
        except Exception as e:
            logger.error(f"读取表 {table} 结构失败，跳过迁移: {e}")
            return

        for col, col_type in columns.items():
            if col in existing:
                continue
            try:
                await self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                )
                logger.info(f"数据库迁移：已为 {table} 添加 {col} 列 ({col_type})")
            except Exception as e:
                # 并发/竞态下列可能已存在，幂等吞掉
                logger.debug(f"为 {table} 添加 {col} 列失败（可能已存在）: {e}")

    # ========================
    # 消息拦截（最高优先级）
    # ========================

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def intercept_all_messages(self, event: AstrMessageEvent):
        """拦截所有 QQ 消息，异步写入数据库

        priority=9999 确保在所有其他插件之前拦截。
        不调用 event.stop_event()，让消息继续流向后续插件。
        """
        try:
            raw = event.message_obj.raw_message
            if raw is None:
                return

            # 统一获取值的工具函数
            def _get(obj, key, default=None):
                try:
                    if hasattr(obj, "__getitem__"):
                        return obj[key]
                except (KeyError, TypeError):
                    pass
                return getattr(obj, key, default)

            post_type = _get(raw, "post_type")

            # === 处理撤回事件 ===
            if post_type == "notice":
                notice_type = _get(raw, "notice_type")
                if notice_type in ("group_recall", "friend_recall"):
                    msg_id = _get(raw, "message_id")
                    if msg_id:
                        await self._mark_recalled(str(msg_id))
                return  # notice 事件不写入消息表

            # === 处理普通消息 ===
            if post_type != "message":
                return

            # 解析消息字段
            message_type = _get(raw, "message_type", "group")
            window_type = "group" if message_type == "group" else "private"

            if window_type == "group":
                window_id = str(_get(raw, "group_id", ""))
            else:
                window_id = str(_get(raw, "user_id", ""))

            sender = _get(raw, "sender", {})
            sender_id = str(_get(sender, "user_id", _get(raw, "user_id", "")))
            sender_name = (
                _get(sender, "card")
                or _get(sender, "nickname")
                or ""
            )

            message_id = str(_get(raw, "message_id", ""))

            # 提取文本、图片和额外元数据
            content_text, has_image, image_urls, extra_data = self._extract_content(raw)

            # 异步下载图片到本地缓存
            if has_image and image_urls and self._enable_image_cache:
                local_paths = await self._download_images(
                    image_urls, message_id
                )
                if local_paths:
                    image_urls = local_paths  # 替换为本地路径

            # 序列化原始消息（去敏感字段）
            content_raw = self._safe_serialize_raw(raw)

            # 放入写入队列（非阻塞）
            # F4.1: created_at 升毫秒 (.%f) + receive_seq 单调列(time.time_ns())防同毫秒乱序。
            # round_id/step_id 暂留 NULL（persistence 拦截原始 QQ 消息无 flashlite round_id，
            # 由 S4 rebuild 用 (created_at_ms, receive_seq) 排序回填）。
            record = {
                "window_type": window_type,
                "window_id": window_id,
                "message_id": message_id,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "content_text": content_text,
                "content_raw": content_raw,
                "has_image": 1 if has_image else 0,
                "image_urls": json.dumps(image_urls, ensure_ascii=False) if image_urls else None,
                "extra_data": json.dumps(extra_data, ensure_ascii=False) if extra_data else None,
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
                "round_id": None,
                "step_id": None,
                "receive_seq": time.time_ns(),
            }

            try:
                self._write_queue.put_nowait(("insert", record))
            except asyncio.QueueFull:
                logger.warning("持久化写入队列已满，丢弃消息")
                self._stats["errors"] += 1

        except Exception as e:
            logger.error(f"消息拦截异常: {e}")
            self._stats["errors"] += 1

    # ========================
    # 内容提取
    # ========================

    @staticmethod
    def _extract_content(raw: Any) -> tuple:
        """从 OneBot 消息中提取文本、图片信息、额外元数据

        Returns:
            (content_text, has_image, image_urls, extra_data)
        """
        content_parts = []
        has_image = False
        image_urls = []
        extra_data = {}  # 按类型保存完整元数据

        # OneBot 消息段格式
        message = raw.get("message", []) if isinstance(raw, dict) else getattr(raw, "message", [])

        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    seg_type = seg.get("type", "")
                    data = seg.get("data", {})

                    if seg_type == "text":
                        text = data.get("text", "")
                        if text.strip():
                            content_parts.append(text)

                    elif seg_type == "image":
                        has_image = True
                        url = data.get("url") or data.get("file", "")
                        if url:
                            image_urls.append(url)

                    elif seg_type == "at":
                        qq = data.get("qq", "")
                        content_parts.append(f"[@{qq}]")

                    elif seg_type == "face":
                        face_id = data.get("id", "")
                        try:
                            from astrbot.core.platform.sources.aiocqhttp.qq_face_map import get_face_name
                            face_name = get_face_name(int(face_id)) if face_id else str(face_id)
                        except Exception:
                            face_name = str(face_id)
                        content_parts.append(f"[表情:{face_name}]")

                    elif seg_type == "reply":
                        reply_id = data.get("id", "")
                        content_parts.append(f"[回复:{reply_id}]")

                    elif seg_type == "forward":
                        content_parts.append("[转发消息]")
                        # 保存转发消息原始 nodes（嵌套内容）
                        forward_content = data.get("content") or data.get("id", "")
                        if forward_content:
                            extra_data["forward_content"] = forward_content

                    elif seg_type == "json":
                        # 尝试提取 JSON 卡片标题
                        card_title = ""
                        json_str = data.get("data", "")
                        if json_str:
                            extra_data["json_data"] = json_str
                            if isinstance(json_str, str):
                                try:
                                    import json as _json
                                    jd = _json.loads(json_str)
                                    card_title = (
                                        jd.get("prompt", "")
                                        or jd.get("meta", {}).get("detail_1", {}).get("title", "")
                                        or jd.get("meta", {}).get("news", {}).get("title", "")
                                    )
                                except Exception:
                                    pass
                        content_parts.append(f"[卡片:{card_title}]" if card_title else "[卡片消息]")

                    elif seg_type == "record":
                        content_parts.append("[语音]")
                        # 保存语音 URL
                        voice_url = data.get("url") or data.get("file", "")
                        if voice_url:
                            extra_data["voice_url"] = voice_url

                    elif seg_type == "video":
                        content_parts.append("[视频]")
                        # 保存视频 URL 和文件路径
                        video_url = data.get("url") or data.get("file", "")
                        if video_url:
                            extra_data["video_url"] = video_url

                    elif seg_type == "file":
                        filename = data.get("name", "") or data.get("file", "未知文件")
                        content_parts.append(f"[文件:{filename}]")
                        # 保存文件完整元数据
                        file_info = {
                            "name": filename,
                            "url": data.get("url", ""),
                            "size": data.get("size", 0),
                            "file_id": data.get("id") or data.get("file_id", ""),
                        }
                        extra_data.setdefault("files", []).append(file_info)

                    elif seg_type == "poke":
                        content_parts.append("[戳一戳]")

                    elif seg_type == "mface":
                        # 商城表情/大表情
                        summary = data.get("summary", "")
                        content_parts.append(f"[{summary}]" if summary else "[表情]")

                    else:
                        content_parts.append(f"[{seg_type}]")
        elif isinstance(message, str):
            content_parts.append(message)

        return " ".join(content_parts), has_image, image_urls, extra_data

    # ========================
    # 图片本地缓存
    # ========================

    async def _download_images(self, urls: List[str], msg_id: str) -> List[str]:
        """异步下载图片到本地缓存目录（含 MD5 去重）

        Returns:
            本地相对路径列表（相对于 QQ_data/），下载失败的用 cdn: 前缀保留原 URL
        """
        local_paths = []
        try:
            import aiohttp
        except ImportError:
            logger.warning("图片缓存需要 aiohttp，已回退到 CDN URL")
            return [f"cdn:{u}" for u in urls]

        # 先检查磁盘空间
        await self._maybe_cleanup_images()

        # 加载 hash 索引（hash -> filename 映射）
        hash_index = self._load_hash_index()

        timeout = aiohttp.ClientTimeout(total=10)
        index_dirty = False
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for idx, url in enumerate(urls):
                    if not url or not url.startswith("http"):
                        local_paths.append(f"cdn:{url}")
                        continue
                    try:
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                local_paths.append(f"cdn:{url}")
                                continue
                            data = await resp.read()

                            # MD5 去重：计算 hash
                            file_hash = hashlib.md5(data).hexdigest()

                            if file_hash in hash_index:
                                # 已存在相同内容的图片，直接复用
                                existing = hash_index[file_hash]
                                existing_path = os.path.join(self._image_dir, existing)
                                if os.path.isfile(existing_path):
                                    local_paths.append(f"images/{existing}")
                                    self._stats["images_deduped"] = self._stats.get("images_deduped", 0) + 1
                                    continue
                                else:
                                    # 文件被清理了，移除索引
                                    del hash_index[file_hash]

                            # 新图片，正常保存
                            ext = ".jpg"
                            ct = resp.headers.get("content-type", "")
                            if "png" in ct:
                                ext = ".png"
                            elif "gif" in ct:
                                ext = ".gif"
                            elif "webp" in ct:
                                ext = ".webp"
                            filename = f"{msg_id}_{idx}{ext}"
                            filepath = os.path.join(self._image_dir, filename)
                            with open(filepath, "wb") as f:
                                f.write(data)

                            # 更新 hash 索引
                            hash_index[file_hash] = filename
                            index_dirty = True

                            local_paths.append(f"images/{filename}")
                            self._stats["images_cached"] = self._stats.get("images_cached", 0) + 1
                    except Exception as dl_err:
                        logger.debug(f"图片下载失败 [{idx}]: {dl_err}")
                        local_paths.append(f"cdn:{url}")
        except Exception as sess_err:
            logger.warning(f"图片下载会话异常: {sess_err}")
            return [f"cdn:{u}" for u in urls]

        # 保存更新后的 hash 索引
        if index_dirty:
            self._save_hash_index(hash_index)

        return local_paths

    def _load_hash_index(self) -> Dict[str, str]:
        """加载图片 hash 索引"""
        index_path = os.path.join(self._image_dir, "_hash_index.json")
        if os.path.isfile(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_hash_index(self, index: Dict[str, str]):
        """保存图片 hash 索引"""
        index_path = os.path.join(self._image_dir, "_hash_index.json")
        try:
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"hash 索引保存失败: {e}")

    async def _maybe_cleanup_images(self):
        """检查图片缓存目录大小，超过限制时清理最旧文件"""
        max_bytes = self._image_cache_max_mb * 1024 * 1024
        if not os.path.isdir(self._image_dir):
            return
        try:
            files = []
            total_size = 0
            for fname in os.listdir(self._image_dir):
                fpath = os.path.join(self._image_dir, fname)
                if os.path.isfile(fpath):
                    stat = os.stat(fpath)
                    files.append((fpath, stat.st_mtime, stat.st_size))
                    total_size += stat.st_size

            if total_size <= max_bytes:
                return

            # 按修改时间排序，最旧的在前
            files.sort(key=lambda x: x[1])
            removed = 0
            while total_size > max_bytes * 0.8 and files:  # 清到 80%
                fpath, _, fsize = files.pop(0)
                try:
                    os.remove(fpath)
                    total_size -= fsize
                    removed += 1
                except OSError:
                    pass
            if removed:
                logger.info(f"图片缓存清理: 删除 {removed} 个旧文件，当前 {total_size/1024/1024:.1f}MB")
        except Exception as e:
            logger.debug(f"图片清理异常: {e}")

    @staticmethod
    def _safe_serialize_raw(raw: Any) -> str:
        """安全序列化原始消息，处理不可序列化的对象"""
        try:
            if isinstance(raw, dict):
                # 移除可能过大或敏感的字段
                safe_raw = {k: v for k, v in raw.items() if k not in ("raw_message",)}
                return json.dumps(safe_raw, ensure_ascii=False, default=str)
            else:
                return str(raw)
        except Exception:
            return "{}"

    # ========================
    # 撤回处理
    # ========================

    async def _mark_recalled(self, message_id: str):
        """标记消息为已撤回"""
        try:
            self._write_queue.put_nowait((
                "recall",
                {
                    "message_id": message_id,
                    "recalled_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                },
            ))
            self._stats["total_recalled"] += 1
            logger.info(f"消息撤回标记已入队: {message_id}")
        except asyncio.QueueFull:
            logger.warning("写入队列已满，撤回标记丢失")

    # ========================
    # 批量写入器（后台任务）
    # ========================

    async def _batch_writer(self):
        """后台批量写入任务，攒够 BATCH_SIZE 条或等待 BATCH_TIMEOUT 后批量写入"""
        while True:
            try:
                batch = []
                deadline = time.monotonic() + self.BATCH_TIMEOUT

                # 尽量攒批
                while len(batch) < self.BATCH_SIZE:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        item = await asyncio.wait_for(
                            self._write_queue.get(), timeout=remaining
                        )
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break

                if not batch or not self._db:
                    continue

                start_ts = time.monotonic()

                # 分类处理
                inserts = [r for op, r in batch if op == "insert"]
                recalls = [r for op, r in batch if op == "recall"]

                # F4.1: 落盘前按 receive_seq 排序，防同毫秒/乱序入队破坏 rebuild 顺序。
                # 旧记录可能无 receive_seq（理论上本批都是新写入，兜底用 0）。
                inserts.sort(key=lambda r: r.get("receive_seq") or 0)

                if inserts:
                    await self._db.executemany(
                        """INSERT INTO qq_messages
                           (window_type, window_id, message_id, sender_id, sender_name,
                            content_text, content_raw, has_image, image_urls, extra_data, created_at,
                            round_id, step_id, receive_seq)
                           VALUES (:window_type, :window_id, :message_id, :sender_id, :sender_name,
                                   :content_text, :content_raw, :has_image, :image_urls, :extra_data, :created_at,
                                   :round_id, :step_id, :receive_seq)
                        """,
                        inserts,
                    )

                if recalls:
                    for r in recalls:
                        await self._db.execute(
                            """UPDATE qq_messages 
                               SET is_recalled = 1, recalled_at = :recalled_at
                               WHERE message_id = :message_id
                            """,
                            r,
                        )

                await self._db.commit()

                elapsed_ms = (time.monotonic() - start_ts) * 1000
                self._stats["total_written"] += len(inserts)
                self._stats["last_write_latency_ms"] = round(elapsed_ms, 2)

                if len(inserts) > 0:
                    logger.debug(
                        f"持久化写入 {len(inserts)} 条消息, "
                        f"{len(recalls)} 条撤回标记, "
                        f"耗时 {elapsed_ms:.1f}ms"
                    )

            except asyncio.CancelledError:
                # 优雅退出前清空队列
                await self._flush_remaining()
                break
            except Exception as e:
                logger.error(f"批量写入异常: {e}")
                self._stats["errors"] += 1
                await asyncio.sleep(1)  # 错误后短暂等待

    async def _flush_remaining(self):
        """退出前清空队列中剩余的消息"""
        if not self._db:
            return
        remaining = []
        while not self._write_queue.empty():
            try:
                remaining.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if remaining:
            inserts = [r for op, r in remaining if op == "insert"]
            recalls = [r for op, r in remaining if op == "recall"]

            # F4.1: 退出路径同样按 receive_seq 排序后补列写入，与 _batch_writer 保持一致。
            inserts.sort(key=lambda r: r.get("receive_seq") or 0)

            try:
                if inserts:
                    await self._db.executemany(
                        """INSERT INTO qq_messages
                           (window_type, window_id, message_id, sender_id, sender_name,
                            content_text, content_raw, has_image, image_urls, extra_data, created_at,
                            round_id, step_id, receive_seq)
                           VALUES (:window_type, :window_id, :message_id, :sender_id, :sender_name,
                                   :content_text, :content_raw, :has_image, :image_urls, :extra_data, :created_at,
                                   :round_id, :step_id, :receive_seq)
                        """,
                        inserts,
                    )
                if recalls:
                    for r in recalls:
                        await self._db.execute(
                            """UPDATE qq_messages 
                               SET is_recalled = 1, recalled_at = :recalled_at
                               WHERE message_id = :message_id
                            """,
                            r,
                        )
                await self._db.commit()
                logger.info(f"退出前清空了 {len(inserts)} 条消息, {len(recalls)} 条撤回")
            except Exception as e:
                logger.error(f"清空残留消息失败: {e}")

    # ========================
    # 查询接口（供 Sandbox QQ_data_original 使用）
    # ========================

    @filter.command("qq_stats", alias={"消息统计"})
    async def show_stats(self, event: AstrMessageEvent):
        """显示持久化统计信息"""
        if not self._db:
            yield event.plain_result("❌ 数据库未初始化")
            return

        # 总消息数
        cursor = await self._db.execute("SELECT COUNT(*) FROM qq_messages")
        total = (await cursor.fetchone())[0]

        # 今日消息数
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM qq_messages WHERE created_at >= ?", (today,)
        )
        today_count = (await cursor.fetchone())[0]

        # 活跃窗口数
        cursor = await self._db.execute(
            "SELECT COUNT(DISTINCT window_id) FROM qq_messages WHERE created_at >= ?",
            (today,),
        )
        active_windows = (await cursor.fetchone())[0]

        # 撤回消息数
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM qq_messages WHERE is_recalled = 1"
        )
        recalled = (await cursor.fetchone())[0]

        # 数据库大小
        db_size_mb = os.path.getsize(DB_PATH) / (1024 * 1024) if os.path.exists(DB_PATH) else 0

        stats_msg = (
            f"📊 消息持久化统计\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📝 总消息数: {total:,}\n"
            f"📅 今日消息: {today_count:,}\n"
            f"💬 活跃窗口: {active_windows}\n"
            f"↩️ 撤回消息: {recalled:,}\n"
            f"💾 数据库大小: {db_size_mb:.1f} MB\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⏱ 写入延迟: {self._stats['last_write_latency_ms']}ms\n"
            f"✅ 本次运行: {self._stats['total_written']:,} 写入\n"
            f"❌ 错误计数: {self._stats['errors']}"
        )
        yield event.plain_result(stats_msg)

    # ========================
    # 生命周期
    # ========================

    async def terminate(self):
        """插件终止时安全关闭"""
        if self._writer_task and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, '_cleanup_task') and self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._db:
            await self._db.close()
            logger.info(
                f"PersistencePlugin 已关闭 | "
                f"共写入 {self._stats['total_written']} 条消息"
            )

    # ========================
    # 分级清理（定时任务）
    # ========================

    def _load_storage_policy(self) -> Dict[str, Any]:
        """从 astrbot_plugin_flashlite/config.json 读取持久化策略配置"""
        config_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..",
                         "astrbot_plugin_flashlite", "config.json")
        )
        try:
            if os.path.isfile(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                return config.get("storage_policy", {})
        except Exception as e:
            logger.debug(f"读取持久化策略配置失败: {e}")
        return {}

    async def _periodic_cleanup(self):
        """定时执行分级清理（每6小时检查一次）

        分级策略：
        - 热存储 (0 ~ hot_days 天): 完整保留所有字段
        - 冷存储 (hot_days ~ cold_days 天): 裁剪 content_raw 节省空间
        - 归档清理 (> archive_days 天): 物理删除记录
        """
        CLEANUP_INTERVAL = 6 * 3600  # 6 小时

        # 启动后等待 60 秒再首次执行（等数据库稳定）
        await asyncio.sleep(60)

        while True:
            try:
                await self._run_tiered_cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时分级清理异常: {e}")

            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _run_tiered_cleanup(self):
        """执行一次分级清理"""
        policy = self._load_storage_policy()
        hot_days = policy.get("hot_days", 7)
        cold_days = policy.get("cold_days", 30)
        archive_days = policy.get("archive_days", 90)
        enable = policy.get("enable_auto_cleanup", True)

        if not enable:
            logger.debug("分级清理已禁用")
            return

        if not os.path.exists(DB_PATH):
            return

        now = datetime.now()
        cold_cutoff = (now - timedelta(days=hot_days)).strftime("%Y-%m-%dT%H:%M:%S")
        archive_cutoff = (now - timedelta(days=archive_days)).strftime("%Y-%m-%dT%H:%M:%S")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL")

            # 1. 归档清理：删除超过 archive_days 的消息
            cursor = await db.execute(
                "DELETE FROM qq_messages WHERE created_at < ?",
                (archive_cutoff,),
            )
            archived_count = cursor.rowcount

            # 2. 冷存储裁剪：对 hot_days ~ archive_days 范围内的消息裁剪 content_raw
            #    只裁剪还有 content_raw 的记录（避免重复处理）
            cursor = await db.execute(
                """UPDATE qq_messages
                   SET content_raw = NULL
                   WHERE created_at < ? AND created_at >= ?
                     AND content_raw IS NOT NULL""",
                (cold_cutoff, archive_cutoff),
            )
            cold_count = cursor.rowcount

            await db.commit()

        if archived_count > 0 or cold_count > 0:
            logger.info(
                f"分级清理完成: 归档删除 {archived_count} 条 (>{archive_days}天), "
                f"冷存储裁剪 {cold_count} 条 (>{hot_days}天), "
                f"策略: hot={hot_days}d/cold={cold_days}d/archive={archive_days}d"
            )
            self._stats["cleanup_archived"] = self._stats.get("cleanup_archived", 0) + archived_count
            self._stats["cleanup_cold"] = self._stats.get("cleanup_cold", 0) + cold_count
        else:
            logger.debug("分级清理: 无需处理")


# 向后兼容
Main = PersistencePlugin
