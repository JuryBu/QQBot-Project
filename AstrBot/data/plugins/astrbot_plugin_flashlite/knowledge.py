"""
Knowledge 知识缓存系统
Flash Lite 自动维护的实时对话缓存

核心特征：
- 按窗口分区（GroupMessage:xxx / FriendMessage:xxx）
- 每次 Flash Lite 触发时更新
- 全量发送给主模型（每次请求带上完整 Knowledge）
- 内容简短（200-500 字/窗口）

文档: Plan_1_memory.md Knowledge 部分
"""

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from astrbot.api import logger

# Knowledge 持久化路径
KNOWLEDGE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Knowledge")
)
KNOWLEDGE_FILE = os.path.join(KNOWLEDGE_DIR, "knowledge_cache.json")

# 默认配置
MAX_SUMMARY_CHARS = 500       # 每个窗口摘要最大字数
MAX_ACTIVE_WINDOWS = 20       # 最多跟踪窗口数
INACTIVE_EXPIRE_HOURS = 72    # 不活跃窗口过期时间（小时）


class KnowledgeCache:
    """Knowledge 知识缓存

    结构:
    {
        "last_updated": "ISO时间",
        "windows": {
            "GroupMessage:<GROUP_B>": {
                "name": "群名",
                "summary": "最近话题摘要",
                "active_users": ["张三", "李四"],
                "mood": "轻松闲聊",
                "recent_topics": ["话题1", "话题2"],
                "last_active_ts": unix_timestamp,
            },
            ...
        },
        "recent_operations": ["操作1", "操作2"]
    }
    """

    def __init__(self):
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        self._cache: Dict[str, Any] = self._load()

    # ========================
    # 读写
    # ========================

    def _load(self) -> Dict[str, Any]:
        """从文件加载 Knowledge"""
        if os.path.exists(KNOWLEDGE_FILE):
            try:
                with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Knowledge 加载失败，重建: {e}")

        return {
            "last_updated": datetime.now().isoformat(),
            "windows": {},
            "recent_operations": [],
            "user_profiles": {},
        }

    def _save(self):
        """持久化到文件"""
        self._cache["last_updated"] = datetime.now().isoformat()
        try:
            with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Knowledge 保存失败: {e}")

    # ========================
    # 窗口更新
    # ========================

    def update_window(
        self,
        window_key: str,
        summary: str,
        active_users: Optional[List[str]] = None,
        mood: Optional[str] = None,
        recent_topics: Optional[List[str]] = None,
        name: Optional[str] = None,
    ):
        """更新某个窗口的 Knowledge

        Args:
            window_key: 窗口标识（如 "GroupMessage:<GROUP_B>"）
            summary: 话题摘要（200-500 字）
            active_users: 活跃用户列表
            mood: 群氛围
            recent_topics: 最近话题
            name: 群名/用户名
        """
        windows = self._cache.setdefault("windows", {})

        # 截断摘要
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + "..."

        existing = windows.get(window_key, {})
        windows[window_key] = {
            "name": name or existing.get("name", ""),
            "summary": summary,
            "active_users": active_users or existing.get("active_users", []),
            "mood": mood or existing.get("mood", ""),
            "recent_topics": recent_topics or existing.get("recent_topics", []),
            "last_active_ts": time.time(),
        }

        # 清理过期窗口
        self._cleanup_expired()

        # 持久化
        self._save()

        logger.debug(f"Knowledge 更新: {window_key} → '{summary[:50]}...'")

    # ========================
    # 读取
    # ========================

    def get_window(self, window_key: str) -> Optional[Dict[str, Any]]:
        """获取单个窗口的 Knowledge"""
        return self._cache.get("windows", {}).get(window_key)

    def get_all(self) -> Dict[str, Any]:
        """获取完整 Knowledge（用于发送给主模型）"""
        return self._cache

    def get_formatted(self) -> str:
        """获取格式化的 Knowledge 文本（用于拼入请求体）"""
        windows = self._cache.get("windows", {})
        if not windows:
            return "(暂无 Knowledge 缓存)"

        parts = ["## 近期对话知识缓存"]
        for key, info in sorted(
            windows.items(),
            key=lambda x: x[1].get("last_active_ts", 0),
            reverse=True,
        ):
            name = info.get("name", key)
            summary = info.get("summary", "")
            users = ", ".join(info.get("active_users", [])[:5])
            mood = info.get("mood", "")

            # 计算 last_active 人类可读格式
            ts = info.get("last_active_ts", 0)
            ago = self._time_ago(ts)

            parts.append(
                f"\n### {name} ({key})\n"
                f"- 最近活跃: {ago}\n"
                f"- 氛围: {mood}\n"
                f"- 参与者: {users}\n"
                f"- 摘要: {summary}"
            )

        ops = self._cache.get("recent_operations", [])
        if ops:
            parts.append("\n### 近期操作")
            for op in ops[-5:]:
                parts.append(f"- {op}")

        # 用户画像索引（仅列出已知用户，不灌入详情——详情由定向注入机制按需注入）
        profiles = self._cache.get("user_profiles", {})
        if profiles:
            parts.append(f"\n### 已知用户 ({len(profiles)}人)")
            profile_items = sorted(
                profiles.items(),
                key=lambda x: x[1].get("interaction_count", 0),
                reverse=True,
            )[:15]  # 最多列 15 个
            for qq_id, pf in profile_items:
                nick = pf.get("nickname", qq_id)
                count = pf.get("interaction_count", 0)
                parts.append(f"- {nick}({qq_id}) [互动{count}次]")

        return "\n".join(parts)

    def get_prompt_text(self) -> str:
        """get_formatted 的别名，用于 FlashLite systemInstruction"""
        return self.get_formatted()

    # ========================
    # 操作记录
    # ========================

    def add_operation(self, operation: str):
        """添加操作记录"""
        ops = self._cache.setdefault("recent_operations", [])
        ops.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {operation}")
        # 只保留最近 10 条
        if len(ops) > 10:
            self._cache["recent_operations"] = ops[-10:]
        self._save()

    # ========================
    # 用户画像（分层存储）
    # ========================
    #
    # Fact 结构: {summary, content, category, created_at, last_referenced}
    # category: pinned(10) / dynamic(15) / archived(30)
    #
    # 卡片级别: 活跃上限50, 冷卡片→archived
    # archived 卡片/事实: 压缩冷存储，被引用时 reactivate
    #

    def sync_nicknames(self, active_users: List[str]):
        """从 ACTIVE_USERS 列表自动同步昵称到卡片。

        active_users 格式: ["昵称(QQ号)", "昵称2(QQ号2)", ...]
        只更新已有卡片的 nickname，不创建新卡片。
        """
        if not active_users:
            return
        profiles = self._cache.get("user_profiles", {})
        if not profiles:
            return
        changed = False
        for user_str in active_users:
            # 解析 "昵称(QQ号)" 格式
            import re
            match = re.match(r'^(.+?)\((\d+)\)$', user_str.strip())
            if not match:
                continue
            nickname, qq_id = match.group(1).strip(), match.group(2)
            if qq_id in profiles and nickname:
                old_nick = profiles[qq_id].get("nickname", "")
                if old_nick != nickname:
                    profiles[qq_id]["nickname"] = nickname
                    changed = True
                    logger.debug(f"昵称同步: {qq_id} '{old_nick}' → '{nickname}'")
        if changed:
            self._save()

    PROFILE_LIMITS = {
        "pinned_max": 10,
        "dynamic_max": 15,
        "archived_max": 30,
        "active_cards_max": 50,
        "card_cold_days": 14,  # 14天无互动→冷卡片
    }

    def update_user_profile(
        self,
        qq_id: str,
        summary: str,
        content: str = "",
        category: str = "dynamic",
        nickname: str = "",
    ):
        """更新用户画像（由 FlashLite 的 PROFILE_UPDATE 触发）

        Args:
            qq_id: QQ号（如果传入昵称会自动纠正为已有卡片的 QQ 号）
            summary: 简短摘要（10-30字），注入用
            content: 完整内容, 可选
            category: pinned / dynamic（默认）
            nickname: 昵称（可选）
        """
        # === QQ号校验：非纯数字一律拒绝建卡（不再通过昵称反查合并，避免串号脏卡） ===
        if not qq_id.isdigit():
            logger.warning(f"画像跳过: qq_id '{qq_id}' 非纯数字QQ号，拒绝建卡")
            return

        # === Bot QQ 号过滤：不为 bot 自身创建画像 ===
        bot_qq_ids = getattr(self, '_bot_qq_ids', set())
        if qq_id in bot_qq_ids:
            logger.debug(f"画像跳过: {qq_id} 是 bot 自身QQ号")
            return

        profiles = self._cache.setdefault("user_profiles", {})
        now_iso = datetime.now().isoformat()

        profile = profiles.setdefault(qq_id, {
            "nickname": nickname or qq_id,
            "status": "active",  # active / archived
            "facts": [],
            "interaction_count": 0,
            "first_seen": now_iso,
            "last_seen": now_iso,
        })

        # reactivate（如果是 archived 卡片被更新）
        if profile.get("status") == "archived":
            profile["status"] = "active"
            logger.debug(f"卡片 reactivated: {qq_id}")

        if nickname:
            profile["nickname"] = nickname

        # 构造新 Fact
        if summary:
            cat = category if category in ("pinned", "dynamic") else "dynamic"
            new_fact = {
                "summary": summary[:50],
                "content": content[:500] if content else summary,
                "category": cat,
                "created_at": now_iso,
                "last_referenced": now_iso,
            }
            # 检查 summary 是否已存在（粗去重）
            facts = profile.setdefault("facts", [])
            existing = [f for f in facts if f.get("category") != "archived"
                        and f.get("summary", "").strip() == summary.strip()]
            if not existing:
                facts.append(new_fact)
                self._enforce_fact_limits(profile)

        profile["interaction_count"] = profile.get("interaction_count", 0) + 1
        profile["last_seen"] = now_iso

        # 卡片级管理
        self._enforce_card_limits()
        self._save()
        logger.debug(f"画像更新: {qq_id} += '{summary[:30]}'")

    def _enforce_fact_limits(self, profile: Dict):
        """强制 Fact 分类上限——超限的 dynamic→archived→删除"""
        facts = profile.get("facts", [])
        lim = self.PROFILE_LIMITS

        pinned = [f for f in facts if f.get("category") == "pinned"]
        dynamic = [f for f in facts if f.get("category") == "dynamic"]
        archived = [f for f in facts if f.get("category") == "archived"]

        # pinned 超限：按 created_at 淘汰最早的到 dynamic
        if len(pinned) > lim["pinned_max"]:
            pinned.sort(key=lambda f: f.get("created_at", ""))
            overflow = pinned[:len(pinned) - lim["pinned_max"]]
            for f in overflow:
                f["category"] = "dynamic"
            pinned = pinned[len(overflow):]
            dynamic = [f for f in facts if f.get("category") == "dynamic"]

        # dynamic 超限：按 last_referenced LRU 淘汰到 archived
        if len(dynamic) > lim["dynamic_max"]:
            dynamic.sort(key=lambda f: f.get("last_referenced", ""))
            overflow = dynamic[:len(dynamic) - lim["dynamic_max"]]
            for f in overflow:
                f["category"] = "archived"
                # 压缩：archived 只保留 summary，清空 content
                f["content"] = ""
            archived = [f for f in facts if f.get("category") == "archived"]

        # archived 超限：删除最冷的
        if len(archived) > lim["archived_max"]:
            archived.sort(key=lambda f: f.get("last_referenced", ""))
            keep = archived[len(archived) - lim["archived_max"]:]
            discard_set = set(id(f) for f in archived) - set(id(f) for f in keep)
            profile["facts"] = [f for f in facts if id(f) not in discard_set]

    def _enforce_card_limits(self):
        """卡片级管理：活跃卡片超限→冷卡片 archived"""
        profiles = self._cache.get("user_profiles", {})
        lim = self.PROFILE_LIMITS

        active = {k: v for k, v in profiles.items()
                  if v.get("status", "active") == "active"}

        if len(active) <= lim["active_cards_max"]:
            return

        # 按 last_seen LRU 排序
        sorted_keys = sorted(
            active.keys(),
            key=lambda k: active[k].get("last_seen", ""),
        )
        to_archive = sorted_keys[:len(active) - lim["active_cards_max"]]

        for k in to_archive:
            profiles[k]["status"] = "archived"
            # 压缩所有 facts 的 content
            for f in profiles[k].get("facts", []):
                f["content"] = ""
            logger.debug(f"冷卡片 archived: {k}")

    def reactivate_card(self, qq_id: str):
        """激活冷卡片"""
        profiles = self._cache.get("user_profiles", {})
        if qq_id in profiles and profiles[qq_id].get("status") == "archived":
            profiles[qq_id]["status"] = "active"
            profiles[qq_id]["last_seen"] = datetime.now().isoformat()
            self._enforce_card_limits()
            self._save()
            logger.debug(f"卡片 reactivated: {qq_id}")

    def reactivate_fact(self, qq_id: str, fact_summary: str):
        """激活冷 Fact（archived→dynamic）"""
        profile = self._cache.get("user_profiles", {}).get(qq_id)
        if not profile:
            return
        for f in profile.get("facts", []):
            if f.get("category") == "archived" and f.get("summary") == fact_summary:
                f["category"] = "dynamic"
                f["last_referenced"] = datetime.now().isoformat()
                self._enforce_fact_limits(profile)
                self._save()
                logger.debug(f"Fact reactivated: {fact_summary}")
                return

    def get_user_profile(self, qq_id: str) -> Optional[Dict[str, Any]]:
        """获取指定用户的画像"""
        return self._cache.get("user_profiles", {}).get(qq_id)

    def get_user_cards(
        self,
        qq_ids: List[str],
        max_facts: int = 10,
        detail: bool = False,
        include_archived: bool = False,
    ) -> str:
        """定向查询指定用户的卡片信息，返回格式化文本。

        Args:
            qq_ids: QQ号列表
            max_facts: 每人最多返回的 facts 数
            detail: True 返回 content，False 只返回 summary
            include_archived: 是否包含 archived facts
        """
        profiles = self._cache.get("user_profiles", {})
        if not profiles or not qq_ids:
            return ""

        parts = []
        for qq_id in qq_ids:
            pf = profiles.get(qq_id.strip())
            if not pf:
                continue

            # 引用时 reactivate
            if pf.get("status") == "archived":
                self.reactivate_card(qq_id)

            nick = pf.get("nickname", qq_id)
            count = pf.get("interaction_count", 0)
            first = pf.get("first_seen", "?")[:10]
            last = pf.get("last_seen", "?")[:10]
            status = pf.get("status", "active")

            facts = pf.get("facts", [])
            # 过滤分类
            categories = ["pinned", "dynamic"]
            if include_archived:
                categories.append("archived")
            visible = [f for f in facts if f.get("category") in categories]

            # pinned 排前面，dynamic 按 last_referenced 倒序
            pinned_f = [f for f in visible if f.get("category") == "pinned"]
            dynamic_f = sorted(
                [f for f in visible if f.get("category") == "dynamic"],
                key=lambda f: f.get("last_referenced", ""),
                reverse=True,
            )
            archived_f = [f for f in visible if f.get("category") == "archived"]
            ordered = (pinned_f + dynamic_f + archived_f)[:max_facts]

            if detail:
                lines = []
                for f in ordered:
                    cat = f.get("category", "?")
                    badge = {"pinned": "📌", "dynamic": "💬", "archived": "📦"}.get(cat, "")
                    lines.append(f"  {badge} {f.get('summary', '?')}")
                    if f.get("content") and f["content"] != f.get("summary"):
                        lines.append(f"    → {f['content'][:200]}")
                facts_str = "\n".join(lines) if lines else "  (暂无)"
            else:
                lines = []
                for f in ordered:
                    cat = f.get("category", "?")
                    badge = {"pinned": "📌", "dynamic": "💬", "archived": "📦"}.get(cat, "")
                    lines.append(f"  {badge} {f.get('summary', '?')}")
                facts_str = "\n".join(lines) if lines else "  (暂无)"

            # 更新 last_referenced
            for f in ordered:
                f["last_referenced"] = datetime.now().isoformat()

            header = f"### {nick} (QQ:{qq_id})"
            if status == "archived":
                header += " [冷卡片]"
            parts.append(
                f"{header}\n"
                f"互动{count}次 | 首次: {first} | 最近: {last}\n"
                f"{facts_str}"
            )

        if parts:
            self._save()
        return "\n\n".join(parts)

    def get_all_profiles(self) -> Dict[str, Dict[str, Any]]:
        """获取全部用户画像（供前端 API 使用）"""
        return dict(self._cache.get("user_profiles", {}))

    def prepare_review_prompt(self, qq_id: str) -> Optional[str]:
        """为指定用户生成画像 Review 提示词——交给 FlashLite 做语义整理。

        Returns:
            提示词字符串，若该用户 facts 太少则返回 None
        """
        profile = self._cache.get("user_profiles", {}).get(qq_id)
        if not profile:
            return None
        facts = profile.get("facts", [])
        if len(facts) < 3:  # facts 太少无需 review
            return None

        nick = profile.get("nickname", qq_id)
        facts_text = "\n".join(
            f"  [{f.get('category','?')}] {f.get('summary','?')}"
            + (f" → {f.get('content','')[:100]}" if f.get("content") else "")
            for f in facts
        )

        return (
            f"你现在是画像整理助手。以下是用户 {nick}(QQ:{qq_id}) 的全部事实记录，"
            f"共{len(facts)}条。请执行以下整理任务：\n\n"
            f"1. **合并重复/相似事实**：语义相近的合并为一条（保留更完整的）\n"
            f"2. **重新分类**：pinned=身份/固定信息（生日、专业、性格） / dynamic=近期状态/偏好\n"
            f"3. **精炼摘要**：每条 summary 限 10-30 字\n"
            f"4. **淘汰低价值**：删除过于琐碎、一次性的信息\n\n"
            f"当前事实：\n{facts_text}\n\n"
            f"请按以下格式逐行输出整理后的结果（每行一条）：\n"
            f"category:summary|content\n"
            f"例如：\n"
            f"pinned:大三AI专业学生|南师大AI专业，对深度学习感兴趣\n"
            f"dynamic:最近在做AstrBot|正在开发FlashLite插件\n\n"
            f"只输出整理后的结果行，不要输出其它内容。"
        )

    def apply_review_result(self, qq_id: str, model_output: str) -> int:
        """解析 FlashLite 模型的 Review 输出，替换该用户的 facts。

        Args:
            qq_id: QQ号
            model_output: 模型输出的整理结果

        Returns:
            整理后的 facts 数量
        """
        profile = self._cache.get("user_profiles", {}).get(qq_id)
        if not profile:
            return 0

        new_facts = []
        now_iso = datetime.now().isoformat()

        for line in model_output.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("```"):
                continue

            # 解析 category:summary|content
            if ":" in line:
                first_colon = line.index(":")
                cat = line[:first_colon].strip().lower()
                rest = line[first_colon + 1:].strip()

                if cat not in ("pinned", "dynamic", "archived"):
                    # 可能整行就是 summary，按 dynamic 处理
                    cat = "dynamic"
                    rest = line

                if "|" in rest:
                    summ, cont = rest.split("|", 1)
                else:
                    summ, cont = rest, ""

                new_facts.append({
                    "summary": summ.strip()[:50],
                    "content": cont.strip()[:500] if cont.strip() else summ.strip(),
                    "category": cat,
                    "created_at": now_iso,
                    "last_referenced": now_iso,
                })

        if not new_facts:
            logger.warning(f"画像 Review 解析失败，保留原始 facts: {qq_id}")
            return 0

        # 保留 archived facts（模型只整理 active 的）
        old_archived = [f for f in profile.get("facts", [])
                        if f.get("category") == "archived"]

        profile["facts"] = new_facts + old_archived
        self._enforce_fact_limits(profile)
        self._save()
        logger.info(f"画像 Review 完成: {qq_id}, 整理后 {len(new_facts)} 条")
        return len(new_facts)

    def get_review_candidates(self, min_facts: int = 5) -> List[str]:
        """获取需要 Review 的用户 QQ 号列表（facts 数量 >= min_facts 的活跃卡片）"""
        profiles = self._cache.get("user_profiles", {})
        candidates = []
        for qq_id, pf in profiles.items():
            if pf.get("status", "active") != "active":
                continue
            active_facts = [f for f in pf.get("facts", [])
                           if f.get("category") != "archived"]
            if len(active_facts) >= min_facts:
                candidates.append(qq_id)
        return candidates

    def review_profiles_quick(self):
        """降级快速去重——无模型时的子串匹配兜底。"""
        profiles = self._cache.get("user_profiles", {})
        merged_count = 0
        for qq_id, pf in profiles.items():
            facts = pf.get("facts", [])
            if len(facts) < 2:
                continue
            active = [f for f in facts if f.get("category") != "archived"]
            to_remove = set()
            for i in range(len(active)):
                for j in range(i + 1, len(active)):
                    si = active[i].get("summary", "").lower().replace(" ", "")
                    sj = active[j].get("summary", "").lower().replace(" ", "")
                    if si and sj and (si in sj or sj in si):
                        if len(si) >= len(sj):
                            to_remove.add(id(active[j]))
                        else:
                            to_remove.add(id(active[i]))
                        merged_count += 1
            if to_remove:
                pf["facts"] = [f for f in facts if id(f) not in to_remove]
        if merged_count > 0:
            self._save()
            logger.info(f"画像快速去重: 合并了 {merged_count} 条")
        return merged_count

    # ========================
    # 操作记录
    # ========================

    def _cleanup_expired(self):
        """清理过期窗口"""
        now = time.time()
        expired_threshold = now - (INACTIVE_EXPIRE_HOURS * 3600)

        windows = self._cache.get("windows", {})
        expired = [
            k for k, v in windows.items()
            if v.get("last_active_ts", 0) < expired_threshold
        ]

        for k in expired:
            del windows[k]
            logger.debug(f"Knowledge 过期清理: {k}")

        # 保留最多 MAX_ACTIVE_WINDOWS
        if len(windows) > MAX_ACTIVE_WINDOWS:
            sorted_keys = sorted(
                windows.keys(),
                key=lambda k: windows[k].get("last_active_ts", 0),
            )
            for k in sorted_keys[: len(windows) - MAX_ACTIVE_WINDOWS]:
                del windows[k]

    # ========================
    # 工具方法
    # ========================

    @staticmethod
    def _time_ago(ts: float) -> str:
        """将 unix 时间戳转为人类可读的"X分钟前"格式"""
        if ts == 0:
            return "未知"
        diff = time.time() - ts
        if diff < 60:
            return "刚刚"
        elif diff < 3600:
            return f"{int(diff / 60)} 分钟前"
        elif diff < 86400:
            return f"{int(diff / 3600)} 小时前"
        else:
            return f"{int(diff / 86400)} 天前"

    def get_stats(self) -> Dict[str, Any]:
        """统计信息"""
        windows = self._cache.get("windows", {})
        return {
            "total_windows": len(windows),
            "active_windows": sum(
                1 for v in windows.values()
                if time.time() - v.get("last_active_ts", 0) < 86400
            ),
            "file_path": KNOWLEDGE_FILE,
            "last_updated": self._cache.get("last_updated", ""),
        }
