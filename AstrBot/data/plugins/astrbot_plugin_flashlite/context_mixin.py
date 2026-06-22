"""ContextMixin —— FlashLiteEngine 上下文/提示构建相关方法（S2.5 薄壳法拆分）

由 main.py 的 FlashLiteEngine 多继承（class FlashLiteEngine(ContextMixin, Star)）。
本文件方法全部通过共享 self 访问 FlashLiteEngine 在 __init__ 建立的状态/方法，
不在此重复声明字段、不写 __init__。

⚠️ 薄壳法约定：带 @filter 装饰器的 handler（inject_flashlite_context）装饰器+壳留 main.py，
方法体搬到此处改名 _inject_flashlite_context_impl（无装饰器，普通方法，靠继承被 self 调用）。
无装饰器的纯逻辑方法直接整体搬入本文件。
"""

import asyncio
import json
import os
import re
from typing import TYPE_CHECKING, Any, Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest

# SANDBOX_ROOT 供 _inject_flashlite_context_impl 读取 sandbox env.json 使用
try:
    from .tool_registry import SANDBOX_ROOT
except ImportError:
    from tool_registry import SANDBOX_ROOT

# S3 F1.5: dangling tool_calls 读侧防御（约束4：fallback 路径同样要兜底防 400）
try:
    from .checkpoint import _repair_tool_call_pairs
except ImportError:
    from checkpoint import _repair_tool_call_pairs

if TYPE_CHECKING:
    # 依赖契约（运行期由 FlashLiteEngine 主类经 self 提供，静态检查仅作提示）：
    #   方法：self._register_quoted_vars / self._persist_bot_reply / self._call_flash_lite
    #         self._extract_new_messages / self._cfg
    #   字段：self._memory / self._knowledge / self._knowledge_cache / self._t_file_mgr
    #         self._agent_builder / self._tool_registry / self._sandbox / self._stats
    #         self._max_context_for_judgment
    pass


class ContextMixin:
    """FlashLiteEngine 上下文/提示构建 mixin（纯移动，逻辑零改动）。"""

    async def _build_memory_mini_index(self) -> str:
        """构建 Memory 迷你索引供 FlashLite 使用（思路 C）
        
        返回格式：
        ## Memory 索引（共 N 条）
        [1] "标题" [pinned] #分类 #标签1 #标签2
        [2] "标题" #分类
        ...
        """
        if not self._memory:
            return ""
        
        try:
            # 使用 _get_workspace_entries(None) 获取所有条目索引
            entries = await self._memory._get_workspace_entries(None)
            if not entries:
                return ""
            
            # 排序：pinned 优先，然后按 title 字母序（稳定排序）
            entries.sort(key=lambda e: (not e.get('pinned', False), e.get('title', '')))
            
            # 超过 100 条时截断：保留 pinned + 最近的
            MAX_INDEX = 100
            if len(entries) > MAX_INDEX:
                pinned = [e for e in entries if e.get('pinned')][:MAX_INDEX]  # M-5: 先限制 pinned
                remaining = max(0, MAX_INDEX - len(pinned))
                non_pinned = [e for e in entries if not e.get('pinned')][:remaining]
                entries = pinned + non_pinned
            
            lines = [f"## Memory 索引（共 {len(entries)} 条 可用 MEMORY_HINT 序号精确召回）"]
            for i, entry in enumerate(entries, 1):
                pin_mark = " [pinned]" if entry.get('pinned') else ""
                tags = entry.get('tags', [])
                tag_str = " ".join(f"#{t}" for t in tags[:3]) if tags else ""
                title = entry.get('title', '无标题')
                lines.append(f'[{i}] "{title}"{pin_mark} {tag_str}')
            
            lines.append("")
            lines.append("MEMORY_HINT 用法：输出序号精确指定需要召回的记忆 如 MEMORY_HINT=1,3,7")
            lines.append("没有相关记忆时不要输出 MEMORY_HINT 或留空")
            return "\n".join(lines)
        
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"Memory 迷你索引构建失败: {e}")
            return ""

    def _build_flash_lite_system(self) -> str:
        """构建 FlashLite 中断引擎的 systemInstruction（纯静态，用于 KVCache 命中）"""

        return (
            "# 身份\n"
            "你是 Flash Lite 中断引擎（CPU 中断处理器），负责高频处理 QQ 对话上下文。\n\n"
            "# 消息格式说明\n"
            "你收到的消息上下文使用以下格式：\n"
            "- 群聊消息: [时间] 昵称(QQ号): 内容\n"
            "- 私聊消息: [时间] 昵称(QQ号): 内容（一对一对话 没有群号）\n"
            "- Bot（老板娘）消息: [时间] 老板娘 [BOT]: 内容\n"
            "关键信息：\n"
            "- QQ号是跨群唯一标识——同一用户在不同群可能用不同昵称 但QQ号相同\n"
            "- [BOT] 标记的消息是你的主模型（老板娘）的回复 不是用户消息\n"
            "- 判断'和老板娘有关'时 看 [BOT] 标记和对话语境 而非只看名字\n"
            "- 窗口标识格式：群聊=GroupMessage:群号 私聊=FriendMessage:QQ号\n\n"
            "# 核心职责（每次调用都要执行）\n"
            "1. Knowledge 更新：根据最新对话内容 判断是否需要更新当前窗口的 summary/mood\n"
            "   - 群聊窗口标识: GroupMessage:群号\n"
            "   - 私聊窗口标识: FriendMessage:QQ号\n"
            "2. 主模型触发判断：决定是否需要唤醒主模型来回复（见下方触发条件）\n"
            "3. Memory 召回提示：如果对话涉及已有记忆条目 输出对应序号\n"
            "4. 用户画像更新：如果发现用户新的个人信息 标记更新\n"
            "5. 卡片注入指定：指定本次需要注入主模型的用户卡片QQ号\n\n"
            "# CHECKPOINT 说明\n"
            "CHECKPOINT 压缩由系统自动管理（CheckpointManager 基于 token 估算自动触发）\n"
            "你不需要判断是否需要 CHECKPOINT 也不需要在输出中标注 CHECKPOINT 状态\n"
            "系统会在每次处理消息后自动检查 token 量是否超限 超限则自动压缩\n\n"
            "# 触发主模型的条件\n"
            "## 群聊场景\n"
            "满足以下任一条件即触发（TRIGGER_MAIN=true）：\n"
            "- 消息中 @老板娘 或使用唤醒词（如'老板娘'三个字）\n"
            "- 消息明确回复了 [BOT] 标记的消息\n"
            "- 消息直接向老板娘提问或说话（含疑问句+称呼或对话指向）\n"
            "- 用户请求老板娘做某事（搜索/画图/查询等）\n"
            "- 涉及老板娘之前参与的话题 且用户期待她继续参与\n\n"
            "不触发的情况（TRIGGER_MAIN=false）：\n"
            "- 群友之间的闲聊（即使偶尔提到'老板娘'但不是在和她说话）\n"
            "- 纯表情包/图片/链接分享（无明确对话意图）\n"
            "- 老板娘 [BOT] 已经回复过的同一话题且无新问题\n"
            "- 系统通知/入群退群等非对话消息\n\n"
            "## 私聊场景\n"
            "私聊消息来自用户直接和老板娘一对一对话：\n"
            "- 私聊几乎总是需要回复（TRIGGER_MAIN=true） 因为用户在直接和老板娘说话\n"
            "- 以下情况可以不回复（TRIGGER_MAIN=false）：\n"
            "  · 用户只发了文件/图片/链接 没有附带任何文字（纯传文件）\n"
            "  · 系统自动发送的通知类消息\n"
            "- 私聊的 ACTIVE_USERS 只有对话者一人\n\n"
            "# 输出格式（严格遵守）\n\n"
            "## 模式一：消息判断（默认模式）\n"
            "当你收到消息上下文并需要判断是否触发主模型时 使用此格式 每行独占一行：\n"
            "```\n"
            "TRIGGER_MAIN=true 或 TRIGGER_MAIN=false\n"
            "KNOWLEDGE_SUMMARY=<本窗口最新一句话摘要 20字以内>\n"
            "KNOWLEDGE_MOOD=<当前氛围 如 活跃/平静/争论>\n"
            "ACTIVE_USERS=<当前活跃用户列表 格式: 昵称(QQ号) 多个用逗号分隔>\n"
            "MEMORY_HINT=<需要召回的记忆序号 如1,3,7 没有则留空>\n"
            "PROFILE_UPDATE=<QQ号(纯数字):category:summary|content 没有则留空 category=pinned(固定信息)/dynamic(近期动态)>\n"
            "INJECT_CARDS=<需要注入主模型的用户QQ号 多个用逗号分隔 无则留空>\n"
            "CONTEXT_SUMMARY=<给主模型的上下文摘要 包含关键发言者(QQ号)+核心内容+附件信息 50字以内>\n"
            "```\n"
            "标记行之外可以有简短的判断理由。\n\n"
            "## 模式二：对话压缩（CHECKPOINT 压缩任务）\n"
            "当你收到对话压缩任务时 不要使用上述标记行格式 而是直接输出结构化摘要文本。\n"
            "压缩任务的详细格式和原则由任务提示本身指定 你只需按其要求输出即可。\n\n"
            "⚠️ 用户标识格式规范（所有涉及用户的字段必须遵守）：\n"
            "- 格式: 昵称(QQ号) 如 柚子(<ADMIN_QQ>)\n"
            "- QQ号是唯一标识——同一用户可能有多个昵称但QQ号不变\n"
            "- ⛔ PROFILE_UPDATE 的第一个字段必须是纯数字QQ号，绝对不能用昵称！\n"
            "  ✅ 正确: PROFILE_UPDATE=<ADMIN_QQ>:dynamic:喜欢看番\n"
            "  ❌ 错误: PROFILE_UPDATE=Jury_鸽姬布:dynamic:喜欢看番\n"
            "- 如果消息中找不到用户的QQ号，就不要输出 PROFILE_UPDATE\n"
            "- ACTIVE_USERS/PROFILE_UPDATE/CONTEXT_SUMMARY 中的用户都要带QQ号\n"
            "- 昵称自动同步: 系统会从 ACTIVE_USERS 中提取最新昵称自动更新到用户卡片，无需手动维护\n\n"
            "# 任务执行指南\n\n"
            "## 消息判断任务（群聊场景）\n"
            "当 user contents 标注\"窗口类型: 群聊\"时，按以下规则判断：\n"
            "1. 如果有人明确 @ 了老板娘或使用了唤醒词 → TRIGGER_MAIN=true\n"
            "2. 如果唤醒词出现在引用、比喻、讨论第三方内容中（不是在和老板娘说话）→ TRIGGER_MAIN=false\n"
            "3. 如果是普通闲聊与老板娘完全无关 → TRIGGER_MAIN=false\n"
            "4. knowledge_update 始终要更新（反映最新话题）\n\n"
            "## 消息判断任务（私聊场景）\n"
            "当 user contents 标注\"窗口类型: 私聊\"时，按以下规则判断：\n"
            "1. 私聊几乎总是需要回复（TRIGGER_MAIN=true） 因为用户直接和老板娘一对一对话\n"
            "2. 以下情况可以不回复（TRIGGER_MAIN=false）：\n"
            "   - 用户只发了文件/图片/链接 没有附带任何文字（纯传文件）\n"
            "   - 系统自动发送的通知类消息\n"
            "3. knowledge_update 也要更新（记录私聊在聊什么）\n\n"
            "## Memory 召回指南\n"
            "MEMORY_HINT 用法：输出序号精确指定需要召回的记忆 如 MEMORY_HINT=1,3,7\n"
            "没有相关记忆时不要输出 MEMORY_HINT 或留空\n"
            "索引排序规则：pinned 优先 → title 字母序 上限 100 条\n"
        )

    def _build_tool_model_system(self) -> str:
        """构建工具模型子代理的 systemInstruction（纯静态，用于 KVCache 命中）"""

        # 工具列表
        tool_list = ""
        if hasattr(self, '_tool_registry'):
            try:
                base_tools = self._tool_registry.get_builtin_tools()
                tool_names = [t.get('name', '?') for t in base_tools.values()] if isinstance(base_tools, dict) else []
                tool_list = ", ".join(tool_names) if tool_names else "view_file, modify_file, sandbox_exec, search, memory_write, memory_query, web_fetch, generate_image"
            except Exception:
                tool_list = "view_file, modify_file, sandbox_exec, search, memory_write, memory_query, web_fetch, generate_image"
        else:
            tool_list = "view_file, modify_file, sandbox_exec, search, memory_write, memory_query, web_fetch, generate_image"

        sandbox_path = ""
        if self._sandbox:
            sandbox_path = str(getattr(self._sandbox, '_root', 'Sandbox/'))

        return (
            "# 身份与体系认知\n"
            "你是工具执行模型（子代理），在 AstrBot 体系的 Sandbox 空间内完成主模型分配的任务。\n"
            "主模型（老板娘）通过两种方式调用你：\n"

            "  1. task_set 后台任务: 主模型创建任务后你被自动唤醒 完成后报告通过 task_report 反馈\n"

            "  2. browser_agent 委托: 主模型直接调用你完成指定任务 你的最终文本输出会直接作为结果返回给主模型\n"

            "     browser_agent 场景下 文件/截图等产物用 Sandbox 路径指针标记(如 [文件: workspace/xxx.md]) 方便主模型引用\n\n"

            "# 工作环境\n"
            f"- Sandbox 根目录: {sandbox_path}\n"
            "- workspace/: 你的工作空间 可自由读写创建\n"
            "- workspace/drafts/: 草稿纸目录——用于记录执行计划、中间结果、debug 笔记等\n"
            "  用法: agent_draft(filename='plan.md', content='## 执行计划\\n1. ...') 写入\n"
            "  用法: agent_draft(filename='plan.md') 读取\n"
            "  建议每个任务开始时先写计划 结束时写总结\n"
            "- workspace/custom_tools/: 自定义工具脚本\n"
            "- workspace/task_reports/: 任务报告输出目录——最终结果必须写到这里\n"
            "- base_tools/: 基础工具定义文件（只读 JSON Schema）\n"
            "- base_tools/system_report/: 系统维护日志（受保护区域 仅定期 Review 时可写入）\n\n"
            "# 可用工具分类\n"
            "## 核心三件套（始终可用）\n"
            "- agent_view_file: 读取 Sandbox 内任意文件\n"
            "- agent_modify_file: 创建或修改 Sandbox 内文件\n"
            "- agent_draft: 读写你的专属草稿纸\n\n"
            "## 扩展工具（按任务需要使用）\n"
            f"{tool_list}\n"
            "  这些工具通过 agent_xxx 方式调用 如 agent_search, agent_web_fetch 等\n\n"
            "# base_tools 规范\n"
            "base_tools/ 下的 .tool.json 文件定义了工具接口\n"
            "格式: {name, description, parameters: {type, properties, required}, timeout_ms}\n"
            "这些文件只读 但你可以在 workspace/custom_tools/ 下创建新 .tool.json 扩展\n\n"
            "# 系统维护工具\n"
            "- system_report: 写入维护日志到 base_tools/system_report/（受保护区域）\n"
            "  🔒 仅在定期 Review 任务中可调用 其它场景调用会被系统拒绝\n"
            "  参数: content(markdown维护报告), report_type(daily/review/alert)\n"
            "  Review 结束后 base_tools/ 自动恢复只读\n\n"
            "# 定期 Review 职责\n"
            "系统会按设定周期（控制面板可调 默认24小时）自动唤醒你执行 Sandbox 定期维护：\n"
            "1. 列出 workspace/ 下所有文件和目录 记录文件数量和总大小\n"
            "2. 重复文件检查：扫描同一目录下内容相同但文件名不同的文件 合并为一个并删除重复项（仅对同目录下的文件执行 跨目录不合并）\n"
            "3. 位置整理：检查文件是否在合理位置（如 task_reports 不该出现在 drafts 中 反之亦然） 将错位文件移动到正确目录\n"
            "4. 临时垃圾清除：删除确认无用的临时文件——包括意外产生的临时文件、drafts/中超过7天的非重要文件、空文件、损坏文件等 记录删除了什么\n"
            "5. 检查 task_reports/ 中已完成但未归档的报告 列出清单\n"
            "6. 检查异常文件(超大/不该存在的) 标记处理\n"
            "7. 调用 system_report(content=维护报告, report_type='review') 写入维护日志\n\n"
            "system_report 日志格式要求:\n"
            "  content 应为 Markdown 包含以下段落:\n"
            "  ## 维护概况 — 一句话总结本次维护结果\n"
            "  ## 文件统计 — workspace 文件数/总大小/新增/删除\n"
            "  ## 重复文件处理 — 发现并合并了哪些重复文件\n"
            "  ## 文件位置整理 — 移动了哪些错位文件及原因\n"
            "  ## 清理记录 — 删除了哪些临时/垃圾文件及原因\n"
            "  ## 异常发现 — 有无异常文件或问题\n"
            "  ## 待处理建议 — 需要关注但本次未处理的事项\n\n"
            "注意: 非 Review 场景下 base_tools/ 对你完全只读 system_report 调用被拒绝\n"
            "你执行完上述 7 步后正常结束即可 系统会自动关闭 Review 权限\n\n"
            "# 工具使用场景指南\n"
            "信息获取: agent_web_fetch(url, mode) 支持 text/rich/tables/download\n"
            "文件处理: agent_view_file + agent_modify_file 链式读写\n"
            "代码执行: agent_sandbox_exec(code, language) 运行 Python/Shell\n"
            "数据搜索: agent_search(query, scope) 搜索 Sandbox 内文件内容\n"
            "记忆系统: agent_memory_write / agent_memory_query 持久化跨任务知识\n"
            "复杂任务: 先 agent_draft 写计划 然后分步执行 再 agent_draft 记录结果\n\n"
            "# 工作原则\n"
            "- 每步完成后用 agent_draft 简要记录进度（防止上下文丢失）\n"
            "- 文件间引用使用路径指针 不要全文复制\n"
            "- 遇到错误不放弃 尝试替代方案 记录错误原因\n"
            "- 需要安装 Python 包时用 agent_sandbox_exec 执行 pip install\n"
            "- task_set 任务: 最终成果写入 workspace/task_reports/ 作为对主模型的交付\n"

            "- browser_agent 委托: 最终结果直接以文本输出 文件产物用路径指针标记\n"

            "- 单步工具调用超时 30s 超大任务拆分成多步\n"
        )

    def _build_judgment_prompt(
        self,
        group_id: str,
        context: str,
        trigger_type: str,
        trigger_content: Optional[str] = None,
        sender_name: Optional[str] = None,
        window_type: str = "group",
    ) -> str:
        """构建 Flash Lite 判断 prompt（纯数据，判断规则已在 system prompt 静态区）"""
        knowledge = self._knowledge_cache.get(group_id, "暂无记录")

        if window_type == "private":
            window_label = "私聊"
            window_key_label = f"FriendMessage:{group_id}"
        else:
            window_label = "群聊"
            window_key_label = f"GroupMessage:{group_id}"

        prompt = f"""窗口类型: {window_label}
窗口标识: {window_key_label}
上次话题摘要: {knowledge}

## 最近{window_label}记录
{context}

## 触发信息
触发类型: {trigger_type}
{"触发内容: " + trigger_content if trigger_content else ""}
{"发送者: " + sender_name if sender_name else ""}"""

        return prompt

    @staticmethod
    def _parse_judgment(raw_result: str) -> Dict[str, Any]:
        """解析 Flash Lite 返回的标记行判断结果

        支持格式：
        TRIGGER_MAIN=true/false
        KNOWLEDGE_SUMMARY=<摘要>
        KNOWLEDGE_MOOD=<氛围>
        MEMORY_HINT=<关键词>
        PROFILE_UPDATE=<QQ号:category:summary|content>
        """
        result = {
            "should_trigger": False,
            "knowledge_update": "",
            "knowledge_mood": "",
            "reason": "",
            "context_summary": "",
            "memory_hint": "",
            "profile_update": "",
            "inject_cards": "",
            "active_users": [],
        }

        # 标记行解析（主模式）
        for line in raw_result.split("\n"):
            line = line.strip()
            if line.startswith("TRIGGER_MAIN="):
                val = line.split("=", 1)[1].strip().lower()
                result["should_trigger"] = val == "true"
            elif line.startswith("KNOWLEDGE_SUMMARY="):
                result["knowledge_update"] = line.split("=", 1)[1].strip()
                result["context_summary"] = result["knowledge_update"]
            elif line.startswith("KNOWLEDGE_MOOD="):
                result["knowledge_mood"] = line.split("=", 1)[1].strip()
            elif line.startswith("MEMORY_HINT="):
                result["memory_hint"] = line.split("=", 1)[1].strip()
            elif line.startswith("PROFILE_UPDATE="):
                result["profile_update"] = line.split("=", 1)[1].strip()
            elif line.startswith("INJECT_CARDS="):
                result["inject_cards"] = line.split("=", 1)[1].strip()
            elif line.startswith("ACTIVE_USERS="):
                raw_users = line.split("=", 1)[1].strip()
                if raw_users:
                    result["active_users"] = [u.strip() for u in raw_users.split(",") if u.strip()]
            elif line.startswith("CONTEXT_SUMMARY="):
                result["context_summary"] = line.split("=", 1)[1].strip()

        # 如果标记行没解析到 trigger，尝试 JSON 降级
        if not any(l.strip().startswith("TRIGGER_MAIN=") for l in raw_result.split("\n")):
            try:
                json_match = re.search(r"\{[^{}]*\}", raw_result, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    result["should_trigger"] = bool(data.get("should_trigger", False))
                    result["knowledge_update"] = data.get("knowledge_update", "")
                    result["context_summary"] = data.get("context_summary", "")
                    result["reason"] = data.get("reason", "")
            except (json.JSONDecodeError, ValueError):
                # 最终降级：关键词匹配
                if "trigger" in raw_result.lower() and "true" in raw_result.lower():
                    result["should_trigger"] = True
                result["reason"] = "降级文本解析"

        # 提取理由（标记行之外的文本）
        if not result["reason"]:
            non_marker_lines = [
                l.strip() for l in raw_result.split("\n")
                if l.strip() and not l.strip().startswith(("TRIGGER_", "KNOWLEDGE_", "MEMORY_", "PROFILE_", "INJECT_", "CONTEXT_", "```"))
            ]
            result["reason"] = " ".join(non_marker_lines)[:200] if non_marker_lines else ""

        return result

    async def _get_recent_context(self, group_id: str, window_type: str = "group") -> str:
        """获取最近的消息上下文（支持群聊/私聊）

        从 persistence 数据库读取用户消息和 bot 回复，合并排序输出完整上下文。
        """
        try:
            import aiosqlite
            db_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "QQ_data", "messages.db")
            )
            if os.path.exists(db_path):
                async with aiosqlite.connect(db_path) as db:
                    # 读取用户消息（含 sender_id 用于区分身份）
                    cursor = await db.execute(
                        """SELECT sender_name, content_text, created_at, sender_id 
                           FROM qq_messages 
                           WHERE window_id = ? AND window_type = ?
                           ORDER BY created_at DESC 
                           LIMIT ?""",
                        (group_id, window_type, self._max_context_for_judgment),
                    )
                    user_rows = await cursor.fetchall()

                    # 读取 bot 回复（同一张表，sender_id='bot'）
                    cursor2 = await db.execute(
                        """SELECT sender_name, content_text, created_at, sender_id 
                           FROM qq_messages 
                           WHERE window_id = ? AND window_type = ? AND sender_id = 'bot'
                           ORDER BY created_at DESC 
                           LIMIT ?""",
                        (group_id, window_type, self._max_context_for_judgment // 2),
                    )
                    bot_rows = await cursor2.fetchall()

                if user_rows or bot_rows:
                    # 合并所有消息并按时间排序
                    all_rows = list(user_rows or []) + list(bot_rows or [])
                    # 去重（bot 回复也在 user_rows 的查询范围内）
                    seen = set()
                    unique_rows = []
                    for row in all_rows:
                        key = (row[0], row[1], row[2])  # (sender, text, ts)
                        if key not in seen:
                            seen.add(key)
                            unique_rows.append(row)
                    # 按 created_at 排序（时间正序）
                    unique_rows.sort(key=lambda r: r[2])
                    # 取最近 N 条
                    recent = unique_rows[-self._max_context_for_judgment:]
                    lines = []
                    for row in recent:
                        name = row[0]
                        text = row[1]
                        ts = row[2]
                        sender_id = row[3] if len(row) > 3 else ""
                        time_str = ts.split("T")[1] if "T" in ts else ts
                        # Bot 消息用 [BOT] 标记，用户消息用 昵称(QQ号) 格式
                        if sender_id == "bot":
                            lines.append(f"[{time_str}] {name} [BOT]: {text}")
                        elif sender_id:
                            lines.append(f"[{time_str}] {name}({sender_id}): {text}")
                        else:
                            lines.append(f"[{time_str}] {name}: {text}")
                    return "\n".join(lines)

        except Exception as e:
            logger.debug(f"从 persistence 读取上下文失败: {e}")

        return "(暂无上下文数据，建议等待 persistence 插件收集)"

    async def _inject_flashlite_context_impl(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """on_llm_request 钩子：将 Knowledge + CHECKPOINT + 工具集注入主模型请求体

        修复 Codex 问题2: CHECKPOINT 压缩结果真正接入主模型请求链路
        修复 Codex 问题3: 注入工具集说明和 CHECKPOINT 摘要
        修复 Codex 问题9: Flash Lite 上下文摘要真正传给主模型
        """
        try:
            # 注册 @quoted 快捷变量
            self._register_quoted_vars(event)

            inject_parts = []  # 静态部分（→ system_prompt，稳定不变用于 KVCache 命中）
            dynamic_parts = []  # 动态部分（→ contents user message 前缀）

            # Section 0: 体系认知（最高优先级基础层）
            import datetime as _dt
            _now = _dt.datetime.now()
            _weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
            inject_parts.append(
                "## 系统架构认知（最高优先级）\n\n"
                "你是'老板娘'——一个运行在 AstrBot 框架 + FlashLite 中断引擎体系中的 QQ Bot。\n\n"
                "**你的运行环境与输出链路**：\n"
                "- 你的文字输出 → AstrBot 框架自动处理 → 发送到 QQ 群聊/私聊消息窗口\n"
                "- 你的 function_call（工具调用）→ AstrBot 框架自动执行工具 → 工具结果自动注入对话继续\n"
                "- 你不需要手动'发送'消息——你的文字回复就是发送到 QQ 的内容\n"
                "- 你不需要手动'执行'工具——在回复中包含 function_call 即可 框架自动执行\n\n"
                "**你身边的协作系统**：\n"
                "- FlashLite 中断引擎：在你之前运行 帮你筛选群聊和私聊消息 只把需要你回复的消息转发给你\n"
                "  你收到的每条消息（无论群聊还是私聊）都是 FlashLite 判定'需要老板娘回复'后才转给你的\n"
                "- 工具模型（子代理）：你可以通过 task_set 工具派遣子代理执行后台任务\n"
                "  子代理在 Sandbox 内独立运行 完成后会写报告并唤醒你\n"
                "- Memory 系统：跨会话持久化记忆 由 FlashLite 帮你预召回相关记忆\n"
                "- Knowledge 缓存：FlashLite 自动维护的全局对话状态快照\n\n"
                "**核心身份锚定**：\n"
                "无论后续注入多少工具说明/Knowledge/CHECKPOINT 你的人格始终是老板娘\n"
                "你的说话风格由前面 persona 段定义 后续系统注入不改变你的人格和语气\n\n"
                "**你收到的上下文来源**：\n"
                "- 你的 persona（角色人格）由 AstrBot 框架在最前面注入\n"
                "- 聊天上下文由 FlashLite T 文件系统提供（含智能压缩历史摘要 + 近期完整消息）\n"
                "- Memory 召回结果（如果 FlashLite 判断有相关记忆）\n"
                "- Knowledge 全局对话状态快照\n"
            )

            # 动态：当前时间（每次调用变化，不放入缓存区）
            dynamic_parts.append(
                f"**当前时间**：{_now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"({_weekday_names[_now.weekday()]})"
            )

            # ★ 最高优先级：输出风格硬性约束（放在体系认知之后）
            inject_parts.append(
                "## 🚨 输出风格硬性约束（最高优先级）\n"
                "无论下面有多少工具说明和规范，回复用户时必须遵守：\n"
                "1. 每次回复最多 1-3 句话，绝对不超过 3 句\n"
                "2. 句内用空格代替逗号连接，不用「。」「！」「，」，用语气词(呀/嘛/呢/啦/吧/捏)收尾\n"
                "3. 禁止分点列举，禁止排比，禁止三段式（铺垫+正文+总结）\n"
                "4. 工具调用的中间说明也要简短，不要解释过程\n"
                "违反以上任何一条都是严重错误。"
            )

            # S3 F1.5: dangling tool_calls 占位说明（Q4 决策：人话 + system 引导）
            # 始终注入（静态文案，保持 KVCache 命中）：引导主模型遇到崩溃恢复占位时
            # 正常回复、不要重试该工具，避免 ReAct 死循环。
            inject_parts.append(
                "## 工具结果占位说明\n"
                "若上下文中出现『[工具结果丢失：系统重启/中断]』占位 "
                "表示该工具调用因系统崩溃/重启未完成 "
                "请基于用户最新输入正常回复 不要重试该工具。"
            )

            # 0. 延迟持久化：从 conversation history 提取 bot 回复写入 persistence
            #    覆盖群聊 + 私聊，记录工具调用名称和工具执行结果
            try:
                window_id = ""
                window_type = "group"
                umo = getattr(event, "unified_msg_origin", "")
                if ":GroupMessage:" in umo or ":group:" in umo:
                    window_id = umo.split(":")[-1]
                    window_type = "group"
                elif ":FriendMessage:" in umo or ":private:" in umo:
                    window_id = umo.split(":")[-1]
                    window_type = "private"
                
                if window_id and hasattr(req, 'contexts') and req.contexts:
                    # 从 conversation history 中找最近的 assistant 回复
                    for msg in reversed(req.contexts):
                        role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
                        if role == "assistant":
                            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
                            # 提取工具调用摘要
                            tool_summary = ""
                            tool_calls = msg.get("tool_calls", []) if isinstance(msg, dict) else getattr(msg, "tool_calls", [])
                            if tool_calls:
                                tool_names = []
                                for tc in tool_calls:
                                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                                    tool_names.append(fn.get("name", "unknown"))
                                tool_summary = ", ".join(tool_names)
                            
                            # 收集本轮 tool 执行结果（紧跟在 assistant 后的 tool role 消息）
                            tool_results = []
                            idx = req.contexts.index(msg) if msg in req.contexts else -1
                            if idx >= 0:
                                for subsequent in req.contexts[idx + 1:]:
                                    sr = subsequent.get("role", "") if isinstance(subsequent, dict) else getattr(subsequent, "role", "")
                                    if sr == "tool":
                                        tr_content = subsequent.get("content", "") if isinstance(subsequent, dict) else getattr(subsequent, "content", "")
                                        if tr_content:
                                            tool_results.append(tr_content[:200])  # 每个结果截断200字
                                    elif sr == "assistant":
                                        break  # 遇到下一个 assistant 就停止
                            
                            if tool_results:
                                tool_summary += f" → 结果: {'; '.join(tool_results[:3])}"
                            
                            if content or tool_summary:
                                asyncio.create_task(
                                    self._persist_bot_reply(window_id, content or "", tool_summary, window_type)
                                )
                            break  # 只处理最近一条
            except Exception as e:
                logger.debug(f"延迟持久化 bot 回复失败: {e}")

            # 1. Knowledge 全局缓存
            knowledge_text = self._knowledge.get_formatted()
            if knowledge_text and knowledge_text != "(暂无 Knowledge 缓存)":
                dynamic_parts.append(knowledge_text)  # 动态：每次变化

            # 2. Flash Lite 上下文摘要 + 最近消息原文（动态）
            summary = None
            if hasattr(event, "get_extra"):
                summary = event.get_extra("flashlite_context_summary", None)
                reason = event.get_extra("flashlite_trigger_reason", None)
                recent_msgs = event.get_extra("flashlite_recent_messages", None)
                if summary or recent_msgs:
                    ctx_block = "## 当前对话上下文\n"
                    if reason:
                        ctx_block += f"触发原因: {reason}\n"
                    if summary:
                        ctx_block += f"FlashLite 摘要: {summary}\n"
                    if recent_msgs:
                        ctx_block += f"\n### 最近消息原文\n{recent_msgs}\n"
                        ctx_block += "(以上是群聊中最近的消息 格式: [时间] 昵称(QQ号): 内容)\n"
                    dynamic_parts.append(ctx_block)  # 动态

            # 2.5 FIX-2: Memory 被动召回结果
            if hasattr(event, "get_extra"):
                memory_recall = event.get_extra("memory_recall", None)
                if memory_recall:
                    dynamic_parts.append(
                        f"## Memory 召回\n"
                        f"以下是与当前对话相关的历史记忆：\n{memory_recall}"
                    )  # 动态

            # 2.8 FIX-4+: 定向用户卡片注入
            try:
                card_qq_ids = []
                # 私聊：自动注入本人卡片
                if hasattr(event, "message_obj") and event.message_obj:
                    raw = getattr(event.message_obj, "raw_message", None)
                    if raw:
                        msg_type = raw.get("message_type", "group") if isinstance(raw, dict) else "group"
                        if msg_type == "private":
                            uid = raw.get("user_id", "") if isinstance(raw, dict) else ""
                            if uid:
                                card_qq_ids.append(str(uid))

                # FlashLite 指定的卡片
                if hasattr(event, "get_extra"):
                    inject_cards_str = event.get_extra("inject_cards", "")
                    if inject_cards_str:
                        for cid in inject_cards_str.split(","):
                            cid = cid.strip()
                            if cid and cid not in card_qq_ids:
                                card_qq_ids.append(cid)

                # 上限 5 张卡片
                card_qq_ids = card_qq_ids[:5]
                if card_qq_ids and self._knowledge:
                    # 私聊用户用多 facts，其他用户少 facts
                    cards_text = self._knowledge.get_user_cards(
                        qq_ids=card_qq_ids,
                        max_facts=10,
                    )
                    if cards_text:
                        dynamic_parts.append(
                            f"## 用户卡片\n"
                            f"以下是与当前对话相关的用户画像：\n{cards_text}"
                        )  # 动态
            except Exception as e:
                logger.warning(f"卡片注入异常: {e}")

            # 3. T 文件 CHECKPOINT 系统 v2：替换 req.contexts
            window_key = None
            if hasattr(event, "message_obj") and event.message_obj:
                raw = getattr(event.message_obj, "raw_message", None)
                if raw:
                    msg_type = raw.get("message_type", "group") if isinstance(raw, dict) else "group"
                    if msg_type == "group":
                        gid = raw.get("group_id", "") if isinstance(raw, dict) else ""
                        window_key = f"GroupMessage:{gid}"
                    else:
                        uid = raw.get("user_id", "") if isinstance(raw, dict) else ""
                        window_key = f"FriendMessage:{uid}"

            t_file_active = False
            if window_key and self._t_file_mgr:
                try:
                    # === H-1 修复: 两阶段锁 ===

                    # S3 F3.3 Phase1 第一件事: flush_buffer（注入前清空内存 buffer）
                    # route_message 把每条 user 消息 buffer_message 进内存 buffer（易失，
                    # 仅 WAL 兜底），尚未取号落盘。注入前若不先 flush，下面的
                    # _extract_new_messages / build_llm_contexts 会漏掉这些未落盘 buffer
                    # 消息 → 划轮缺号、record 漏条。flush_buffer 自带窗口锁且走唯一取号
                    # 入口 _append_messages_inner（路径 A），故必须在下方 _get_lock 之前
                    # 调用——它内部会再取同一把锁，嵌套会死锁。
                    try:
                        await self._t_file_mgr.flush_buffer(window_key)
                    except Exception as _fbe:
                        logger.warning(f"[T-FILE] {window_key}: Phase1 flush_buffer 异常 {_fbe}")

                    # Phase 1（锁内，毫秒级）: 原子化 load → extract → append → save
                    async with self._t_file_mgr._get_lock(window_key):
                        t_file = await self._t_file_mgr.load(window_key)

                        # 从 req.contexts 增量提取新消息追加到 T 文件
                        new_msgs = self._extract_new_messages(req.contexts, t_file)
                        if new_msgs:
                            t_file = await self._t_file_mgr._append_messages_unlocked(
                                window_key, t_file, new_msgs
                            )
                            await self._t_file_mgr.save(window_key, t_file)
                            logger.debug(f"[T-FILE] {window_key}: 追加 {len(new_msgs)} 条新消息")

                    # Phase 2（锁外，秒级）: S4 R2 旧 T1 覆盖式压缩已退役 → record 增量聚合。
                    # 组装 record_cfg（面板可调参数透传给 compose_record / force_seal /
                    # 接力中止）；缺键由 record 模块 DEFAULT 兜底。
                    _record_cfg = {
                        "record_compose_token_limit": self._cfg(
                            "record_compose_token_limit",
                            self._cfg("checkpoint_limit",
                                      self._cfg("checkpoint_token_limit", 50000)),
                        ),
                        "rg_target_rounds": self._cfg("rg_target_rounds", 8),
                        "rg_force_seal_rounds": self._cfg("rg_force_seal_rounds", 15),
                        "rg_force_seal_tokens": self._cfg("rg_force_seal_tokens", 24000),
                        "rg_force_seal_age": self._cfg("rg_force_seal_age", 40),
                        "rg_max_batch_chars": self._cfg("rg_max_batch_chars", 60000),
                        "rg_max_batch_tokens": self._cfg("rg_max_batch_tokens", 16000),
                        "compress_delta_floor": self._cfg("compress_delta_floor", 200),
                        "record_max_relay_rounds": self._cfg("record_max_relay_rounds", 3),
                    }
                    t_file, compress_result = await self._t_file_mgr.compress_if_needed(
                        window_key=window_key,
                        t_file=t_file,
                        flash_lite_caller=self._call_flash_lite,
                        token_limit=self._cfg("checkpoint_limit", self._cfg("checkpoint_token_limit", 50000)),
                        record_cfg=_record_cfg,
                    )

                    if compress_result:
                        self._stats["checkpoints"] = self._stats.get("checkpoints", 0) + 1
                        logger.info(f"[CHECKPOINT] {window_key}: 压缩完成 {compress_result}")

                    # 核心：替换 req.contexts 为 T 文件构建的上下文
                    # S4 R3(D7)：注入主路径传 window_key → 切 record 视图（已聚合读
                    # record 概要块 + 末尾未聚合原文）；record 空/坏 build_llm_contexts
                    # 内部自动 fallback 全量原文，端到端不崩。checkpoint 内部触发/接力
                    # 判定不传 window_key，维持全量 token 口径。
                    _original_contexts = req.contexts  # 保存原始引用（用于 assistant 补录）
                    req.contexts = self._t_file_mgr.build_llm_contexts(
                        t_file, window_key=window_key
                    )
                    t_file_active = True
                    logger.info(f"[T-FILE] {window_key}: req.contexts 已替换为 T 文件内容 ({len(req.contexts)} 条)")

                    # S3 F3.4: assistant 补录 fallback（防 on_llm_response 钩子失效漏录）
                    # 主路径已前移到 on_llm_response 钩子（track_main_model_cost），
                    # 回复送出那一刻立即 buffer_message → 锚点不再滞后一轮（设计§1.1）。
                    # 但 on_llm_response 钩子只拿得到「最终那次」LLMResponse（最后一步
                    # 纯文本 assistant），拿不到 run_context.messages 中的中间 ReAct step
                    # （assistant.tool_calls + 配对 tool）。故这里保留「下次 on_llm_request
                    # 从上一轮 contexts 反查」作为 fallback：
                    #   (1) 补全 on_llm_response 没覆盖的中间 ReAct step；
                    #   (2) on_llm_response 钩子整体失效时兜底补最终 assistant。
                    # 关键（约束7 step 原子）：从「最后一条 assistant」起到 contexts 末尾
                    # 视为一个完整 step（assistant[+tool_calls] + 后续所有配对 tool），
                    # 一次性 append——绝不分两次落盘半个 step（崩溃→下次 provider 400）。
                    try:
                        t_msgs = t_file.get("messages", [])
                        last_t_role = t_msgs[-1].get("role") if t_msgs else None
                        # 仅当本轮 Phase1 未从 contexts 提取到新消息（new_msgs 已涵盖）、
                        # 且 T 文件尾部不是 assistant（说明上一轮回复尚未落盘）才反查
                        if last_t_role != "assistant" and len(new_msgs) == 0:
                            # 定位 _original_contexts 中最后一个 assistant 的下标
                            _last_asst_idx = -1
                            for _i in range(len(_original_contexts) - 1, -1, -1):
                                if _original_contexts[_i].get("role") == "assistant":
                                    _last_asst_idx = _i
                                    break
                            if _last_asst_idx >= 0:
                                # 完整 step = 该 assistant + 其后所有 role=tool（配对结果）
                                _step = [_original_contexts[_last_asst_idx]]
                                for _sub in _original_contexts[_last_asst_idx + 1:]:
                                    if _sub.get("role") == "tool":
                                        _step.append(_sub)
                                    else:
                                        break
                                # 去重：与 on_llm_response 已 buffer 的最终 assistant 撞车时
                                # 跳过（content 相同且无 tool_calls 视为同一条纯文本回复）。
                                # _append_messages_inner 的 message_id 去重是第二道兜底。
                                _asst = _step[0]
                                _asst_content = _asst.get("content", "")
                                _asst_has_tc = bool(_asst.get("tool_calls"))
                                _last_asst_t = next(
                                    (m for m in reversed(t_msgs) if m.get("role") == "assistant"),
                                    None
                                )

                                # S3 端到端修复(fallback 重复补录): content 归一化后再比较。
                                # T 文件里 assistant content 可能是 str(on_llm_response 钩子
                                # 存的纯文本)或 list(多模态), 而 fallback 从 contexts 取的是
                                # OpenAI list 格式 [{'type':'text','text':...}]。直接 == 因格式
                                # 不一致恒为 False → 去重失效 → 同一条回复被钩子+fallback 落两次
                                # (实测螃蟹笑话 step5/step7 重复且错位到下一轮)。
                                def _norm_c(_c):
                                    if isinstance(_c, list):
                                        return " ".join(
                                            _p.get("text", "") for _p in _c
                                            if isinstance(_p, dict) and _p.get("type") == "text"
                                        ).strip()
                                    return str(_c or "").strip()

                                _is_dup = (
                                    not _asst_has_tc
                                    and _last_asst_t is not None
                                    and _norm_c(_last_asst_t.get("content")) == _norm_c(_asst_content)
                                )
                                if not _is_dup:
                                    # 一次性 append 整个 step（约束7 原子落盘）
                                    await self._t_file_mgr.append_messages(
                                        window_key, _step
                                    )
                                    logger.debug(
                                        f"[T-FILE] {window_key}: fallback 补录 step "
                                        f"(assistant+{len(_step) - 1} tool, 原子落盘)"
                                    )
                    except Exception as _ae:
                        logger.debug(f"[T-FILE] assistant 补录 fallback 异常: {_ae}")

                except Exception as e:
                    logger.error(f"[T-FILE] {window_key}: 处理异常 {e}，保持原始 req.contexts")
                    t_file_active = False

            # 3.5 S3 F1.5: fallback 路径 dangling tool_calls 防御（约束4 / C1 critical）
            # T 文件未接管时（异常 fallback / window_key 缺失 / _t_file_mgr 未就绪），
            # req.contexts 保持原始未修复值。读侧 last-mile 必须无条件兜底，
            # 否则崩溃重启残留的未配对 tool_calls 会让 provider 整窗 400。
            # （T 文件路径的 build_llm_contexts 已自带修复，此处仅兜 fallback。）
            if not t_file_active and hasattr(req, "contexts") and req.contexts:
                try:
                    _repaired, _repairs = _repair_tool_call_pairs(req.contexts)
                    if _repairs:
                        req.contexts = _repaired
                        logger.warning(
                            f"[T-FILE] fallback 路径 dangling tool_calls 修复 "
                            f"{len(_repairs)} 处 → {_repairs}"
                        )
                except Exception as _fe:
                    logger.error(f"[T-FILE] fallback dangling 修复异常: {_fe}")

            # 4. 修复 Codex 问题3: 注入工具集说明（brief 模式）
            tool_section = self._agent_builder._build_tool_section("brief")
            if tool_section:
                inject_parts.append(tool_section)

            # 5. QQ 聊天风格 + 工具调用规范 + Sandbox 环境
            inject_parts.append(
                "## 回复格式要求\n"
                "### 日常聊天（默认模式）\n"
                "- 你正在 QQ 群聊/私聊中对话 保持简短口语化\n"
                "- 你的输出会被分段系统自动处理：按空格切分→短句合并→长句拆分→逐条延迟发送\n"
                "- 所以你只需要控制：用空格隔开语义段 总输出控制在 2-3 个短句(每句≤40字)\n"
                "- 避免使用中文全角标点（。！？，；：） 它们会干扰分段切割效果 改用空格做自然停顿 半角 ! ? 做语气\n"
                "- 不要使用 Markdown 标题/列表/代码块等格式（分段系统有 MD 清洗 但最好别用）\n\n"
                "### 长内容输出（非聊天场景）\n"
                "当用户要求长报告/长解答/解析内容/概括总结/格式化输出/代码/分析等 需要大段内容时：\n"
                "1. 在 Sandbox 内用 modify_file 创建美观的 .md / .html / .pdf 文件\n"
                "   - Markdown: 用标题、列表、代码块等丰富排版\n"
                "   - HTML: 可用 CSS 样式做精美页面\n"
                "2. 写完后用 web_fetch(url='file://workspace/xxx.md', mode='screenshot') 自检确认排版无误\n"
                "3. 确认无误后用 upload_data(path='workspace/xxx.md') 将文件发送到 QQ\n"
                "4. 同时用简短一句话回复用户说明文件内容（如'报告写好了 看看呀~'）\n"
                "5. 判断标准：如果你的回复超过 3 句话 就应该转为文件输出模式\n\n"
                "## 工具调用规范（最高优先级 — 严格遵守）\n"
                "你拥有 function calling 能力。你的回复中可以包含 tool_call，框架会自动执行并返回结果。\n"
                "【核心规则】当用户请求需要获取外部信息、执行操作或生成内容时，你必须在回复中包含对应的 tool_call。\n"
                "绝对禁止只用文字说'我来查一下''我帮你画'然后不附带任何 tool_call —— 这等于什么都没做。\n\n"
                "【正确做法示例】\n"
                "用户: 帮我查一下南京天气 → 你的回复必须包含 search(query='南京天气') 的 tool_call\n"
                "用户: 画一张猫娘 → 你的回复必须包含 generate_image(...) 的 tool_call\n"
                "用户: 搜一下最新新闻 → 你的回复必须包含 search(...) 的 tool_call\n\n"
                "【错误做法 — 严禁出现】\n"
                "❌ 回复'好的我这就帮你查'但没有任何 tool_call → 用户什么都收不到\n"
                "❌ 回复'老板娘马上给你画'但没有调用 generate_image → 用户什么都收不到\n\n"
                "- search 工具是唯一搜索入口：scope=web联网搜索，scope=memory搜记忆，scope=auto自动判断（默认）\n"
                "  联网搜索例子：search(query='合肥今天天气', scope='web')\n"
                "  自动判断会根据关键词（天气/新闻/实时等）自动选择联网还是本地搜索\n"
                "- 生成图片流程：先 generate_image → 拿到路径 → 再 send_image 发送给用户\n"
                "  generate_image 参数：prompt(描述,英文更佳), aspect_ratio(auto/1:1/16:9/9:16/4:3/3:4), reference_image(可选,Sandbox图片路径做参考图), number_of_images(1-4)\n"
                "  支持 image-to-image：传入 reference_image 可基于参考图做风格转换/元素替换/编辑\n"
                "- 你可以先回复文字再调用工具，工具完成后继续回复——这是多步工具循环\n"
                "- 可用工具分三种模式：\n"
                "  模式一（简单调用）：直接调用工具，如 generate_image, search, web_fetch 等\n"
                "  模式二（子代理委托）：调用 browser_agent 子代理自主使用工具完成任务并直接返回文本结果 文件产物会以路径指针标记\n"

                "  模式三（Task并行）：调用 task_set 创建后台任务，支持多步骤编排和并行批次执行\n"
                "- 三种模式均可用。简单操作用模式一，复杂多步操作用模式二，需要后台长时间执行的用模式三"
            )

            # 6. Memory 记忆系统使用指南
            inject_parts.append(
                "## Memory 记忆系统\n"
                "你拥有持久化记忆能力，通过 search(scope='memory') 搜索记忆、memory_write 写入记忆。\n"
                "【何时写入】\n"
                "- 用户告诉你重要个人信息（生日、喜好、习惯、身份）时主动存入\n"
                "- 对话中达成的约定、结论、承诺\n"
                "- 用户纠正你的错误认知时更新记忆\n"
                "- 重要事件（用户的成就、经历、情绪变化）\n"
                "【何时读取】\n"
                "- 新对话开始时主动搜索该用户/群聊的历史记忆\n"
                "- 用户说「你还记得吗」「之前说过」等线索时搜索\n"
                "- 需要个性化回复时（如称呼、语气、话题偏好）\n"
                "【⚠️ 用户标识格式】写入 Memory 时涉及用户必须用 昵称(QQ号) 格式\n"
                "  正确: 柚子(<ADMIN_QQ>)说喜欢吃草莓\n"
                "  错误: 柚子说喜欢吃草莓 ← 缺QQ号 无法跨群匹配\n"
                "【卡片档案】为每个常互动的用户/群聊维护一份记忆档案，记录关键信息"
            )

            # 7. Knowledge 全局对话概览说明
            inject_parts.append(
                "## Knowledge 全局对话概览\n"
                "Knowledge 是 Flash Lite 自动维护的全局对话 Cache，你不需要手动更新它。\n"
                "- 内容：每个群聊/私聊窗口的近期摘要、氛围、活跃用户、操作记录\n"
                "- 你收到的 Knowledge 信息反映了各个对话的最新状态\n"
                "- 利用 Knowledge 了解其他对话的上下文（如群友刚聊了什么、你在其他窗口做了什么操作）"
            )

            # 7.5 文件与链接处理规范
            inject_parts.append(
                "## 文件与链接处理规范\n"
                "【文件标记识别】\n"
                "- 当你看到 [文件:xxx] 标记时，说明用户确实发送了该文件，文件存在\n"
                "- 看到文件/链接时直接调用工具处理，不需要先说'我来看看'\n\n"
                "【view_file 文件查看】\n"
                "- 纯文本(.txt/.md/.py/.json/.csv等): 指定行范围读取\n"
                "- 图片(.png/.jpg/.gif/.webp等): 自动缩放优化(≤1024px)后返回图片数据\n"
                "- 批量模式: 传 paths(JSON数组，如[\"a.py\",\"b.txt\"]) 一次读取多个文件(上限10个)\n"
                "- 范围: 仅限 Sandbox 内文件\n\n"
                "【web_fetch 全能网页工具】\n"
                "所有模式通过 mode 参数切换:\n"
                "- mode=text(默认)/full/compact/minimal: 网页正文提取(Markdown)\n"
                "- mode=html: 返回原始HTML(用于自定义解析)\n"
                "- mode=rich: 截图+文本一体返回(效率最高)\n"
                "- mode=screenshot: 单页截图\n"
                "- mode=links: 提取页面所有链接\n"
                "- mode=tables: 提取网页表格为Markdown格式\n"
                "- mode=batch_screenshot + urls(JSON数组): 批量截图(上限10个URL)\n"
                "- mode=download: 下载文件到Sandbox\n"
                "- mode=pipeline + value(JSON步骤数组): 多步操作流水线\n"
                "- url=file:// 本地文件: PDF/Office/图片等直接在浏览器中打开\n"
                "- action参数: click/type/scroll/wait/screenshot/content/visible/find/close 交互操作\n\n"
                "【save_data 文件获取与保存】\n"
                "- 模式1 文本写入: save_data(data=内容, path=路径)\n"
                "- 模式2 URL下载: save_data(url=下载链接, path=保存路径) → 下载到Sandbox\n"
                "  下载完成后会校验Content-Type与文件扩展名，不匹配时会警告\n"
                "- 模式3 本地复制: save_data(local_path=文件路径, path=保存路径) → 仅限QQ/NapCat缓存目录\n\n"
                "【处理流程】\n"
                "1. 网页链接 → web_fetch(url=链接) 直接获取\n"
                "2. 需要表格数据 → web_fetch(url=链接, mode='tables') 提取\n"
                "3. 需要截图查看 → web_fetch(url=链接, mode='rich') 截图+文本\n"
                "4. QQ文件附件 → save_data(local_path=路径, path=sandbox路径) 复制到 Sandbox\n"
                "5. 网络文件下载 → web_fetch(url=链接, mode='download') 或 save_data(url=链接, path=路径)\n"
                "6. PDF/Office 文件(.docx/.xlsx/.pptx/.pdf): 统一用 web_fetch 处理 不要用 view_file\n"
                "   方式A(推荐): web_fetch(url='用户发的原始URL', mode='text') 直接提取文本\n"
                "   方式B: 先 save_data 下载到 workspace 再 web_fetch(url='file://workspace/xxx.docx', mode='text')\n"
                "   方式C: web_fetch(url='file://workspace/xxx.docx', mode='rich') 截图+文本一体\n"
                "   view_file 只能处理纯文本和图片 OFFICE 文档会失败\n"
                "7. 指针系统(source_pointer): 用文件路径引用大块内容 不要全文复制\n"
                "   格式: workspace/xxx.md 或 task_reports/task-0001.md\n"
                "   使用场景: 引用工具模型报告/保存的分析结果/大段代码"
            )

            # 8. Sandbox 工作空间与文件原则
            inject_parts.append(
                "## Sandbox 工作空间\n"
                "你拥有 Sandbox/workspace/ 虚拟空间，可在其中自由操作文件和运行程序。\n"
                "【主动性原则——最重要】\n"
                "- 遇到工具能力不足或库缺失时，优先通过 sandbox_exec 自行解决(如 pip install 安装包、编写 Python 脚本处理)，而不是告诉用户'做不到'\n"
                "- 遇到某格式处理失败时，尝试其他工具降级(如 view_file 失败则 web_fetch，web_fetch 失败则 sandbox_exec 脚本)\n"
                "- 重复性操作可以创建自定义工具(workspace/custom_tools/)来提效\n"
                "- 你的执行环境和用户完全一致，能运行 Python/Node/Shell，能联网 pip install，能力边界非常宽——请充分利用\n"
                "【草稿纸机制】\n"
                "- 复杂任务使用 workspace/drafts/ 做计划、笔记和临时文件\n"
                "- 如同你的思考记录本，可存放任务拆解、中间结果、思路整理\n"
                "- 主模型和工具模型（子代理/Task）都可以使用草稿区写入和读取临时文件\n"
                "- 操作: modify_file(path=workspace/drafts/xx.md, content=内容) 写入\n"
                "- 操作: view_file(path=workspace/drafts/xx.md) 读取\n"
                "- 命名: 任务名_日期.md 如 search_report_0406.md\n"
                "- 用途前缀: plan_(计划) note_(笔记) tmp_(临时) result_(结果)\n"
                "【指针原则】\n"
                "- 文件间引用使用路径地址（指针），分层渐进式组织\n"
                "- 大内容不要全文复制，用路径引用让对方自行查看\n"
                "【限制】\n"
                "- 所有文件操作限于 Sandbox 范围内\n"
                "- 通过基础工具（view_file、modify_file、sandbox_exec 等）交互"
            )

            # 9. 自定义工具编写标准
            inject_parts.append(
                "## 自定义工具系统\n"
                "你可以创建自己的工具扩展能力，工具模型也可以帮你编写工具。\n"
                "【创建方法】\n"
                "1. 在 workspace/custom_tools/ 下创建 <工具名>.tool.json 文件\n"
                "2. JSON 格式必须包含: name(字符串), description(字符串), parameters(object, 含 properties 和 required)\n"
                "3. 可选字段: category(分类), timeout_ms(超时), script(关联脚本路径)\n"
                "4. 如需执行逻辑，在同目录放 <工具名>.py 脚本，tool.json 中 script 字段指向它\n"
                "【.tool.json 格式示例】\n"
                '```json\n'
                '{"name": "my_tool", "description": "做某事", "parameters": {"type": "object", '
                '"properties": {"input": {"type": "string", "description": "输入"}}, "required": ["input"]}, '
                '"script": "my_tool.py", "timeout_ms": 30000}\n'
                '```\n'
                "【调用方法】\n"
                "- 使用 run_custom_tool(name='工具名', args='{\"参数\": \"值\"}') 调用\n"
                "- 基础工具（search、memory_write 等）直接调用，不需要 run_custom_tool\n"
                "【让工具模型代写】\n"
                "- 通过 Task 指令让工具模型编写工具: 描述工具功能和参数，它会生成 .tool.json 和 .py 文件\n"
                "- 写好后通过 run_custom_tool 即可使用"
            )

            # 10. Task 系统完整说明
            inject_parts.append(
                "## Task 后台任务系统\n"
                "通过 task_set 工具管理后台长运行任务，由工具模型子代理执行（每次唤醒默认最多 20 轮工具调用）。\n"
                "【action 一览】\n"
                "- create: 创建任务。参数: task_description(必填), steps(JSON步骤列表), wake_condition, source_pointer, max_steps(工具模型最大轮数 默认20), inject_context(\"true\"则注入当前对话上下文给工具模型 默认不注入)\n"
                "- check: 查看进度。参数: task_id\n"
                "- list: 列出所有活跃任务\n"
                "- kill: 终止任务。参数: task_id\n"
                "【wake_condition 唤醒条件】\n"
                "- notify_main(默认): 任务完成后后台静默唤醒主模型。结果写入 workspace/task_reports/{task_id}.md，"
                "下次收到群消息时自动设置唤醒标志，你会像被 @ 一样被触发，可查看报告\n"
                "- write_report: 仅写报告到 Sandbox，不唤醒\n"
                "- silent: 完全静默\n"
                "【steps 步骤格式】\n"
                "每个 step 是一个 JSON 对象:\n"
                '- {"desc": "描述", "tool": "工具名", "args": {"参数": "值"}}  -- 直接调用工具\n'
                '- {"desc": "描述"}  -- 由工具模型文本执行\n'
                '- {"desc": "描述", "max_steps": 30}  -- 指定该步骤工具模型最大轮数（覆盖默认20）\n'
                '- {"desc": "描述", "batch": 1}  -- 相同 batch 号的步骤并行执行\n'
                '- {"desc": "描述", "wake_at_step": true}  -- 此步骤完成后触发 checkpoint 唤醒主模型\n'
                "【checkpoint 唤醒 (wake_at_step)】\n"
                "在 step 中设置 wake_at_step: true，该步骤完成后系统会:\n"
                "1. 将当前进度写入 workspace/task_reports/{task_id}_checkpoint_{step}.md\n"
                "2. 后台唤醒主模型，你可以查看中间结果并决定是否干预\n"
                "3. 任务不会停止，继续执行后续步骤\n"
                "【使用建议】\n"
                "- 关键步骤设置 wake_at_step 做 checkpoint 检查\n"
                "- 用 check action 主动查询任务进度\n"
                "- 结果文件遵循指针原则，用路径引用"
            )

            # 11. 工具分类导航 + 渐进式披露
            inject_parts.append(
                "## 工具分类速查\n"
                "💡 用 tool_help() 列出全部工具，tool_help(name='工具名') 查看详细参数和用法\n\n"
                "【搜索】search(scope=auto/web/memory/files/all, deep=true 联网深度概括)\n"
                "【记忆】memory_write/read/update/query(跨会话持久化)\n"
                "【文件】view_file(纯文本+图片+批量), modify_file, upload_data, save_data(文本/URL下载/本地复制)\n"
                "【执行】sandbox_exec(Python/Node/Shell), run_custom_tool\n"
                "【媒体】generate_image(prompt, aspect_ratio=auto, reference_image='', n=1), media_summary(转发消息/视频/图文摘要, extract_raw=true提取原文到Sandbox), web_fetch(12种模式)\n"
                "【数据】QQ_data_original(原始聊天, around_msg_id=指针回溯), knowledge_update(Flash Lite 用)\n"
                "【系统】task_set(后台任务), browser_agent(子代理委托, inject_context=\"true\"可传上下文), wait(定时等待), grep(文件搜索)\n\n"
                "## 关键工具调用示例\n"
                "搜索: search(query='天气预报', scope='web')\n"
                "记忆写入: memory_write(title='柚子(<ADMIN_QQ>)的喜好', content='喜欢吃草莓', tags='[\"用户信息\"]')\n"
                "文件查看: view_file(path='workspace/data.txt') 或 view_file(paths='[\"a.py\",\"b.txt\"]')\n"
                "代码执行: sandbox_exec(code='print(1+1)', language='python')\n"
                "网页: web_fetch(url='https://...', mode='text') 或 mode='rich'(截图+文本)\n"
                "草稿: modify_file(path='workspace/drafts/note.md', content='...')\n"
                "定时: wait(seconds=60)\n"
                "文件搜索: grep(pattern='关键词', path='workspace/')\n"
                "详细帮助: tool_help(name='search') ← 查看完整参数说明\n\n"
                "## ⚠️ OFFICE/PDF 文件处理规范\n"
                "- 收到 .docx/.xlsx/.pptx/.pdf 文件时，直接用 web_fetch 处理，不要用 view_file！\n"
                "- 方式1（推荐）：web_fetch(url='用户发的原始URL', mode='text') → 直接提取文本\n"
                "- 方式2：先 save_data 下载到 workspace，再 web_fetch(url='file://workspace/xxx.docx', mode='text')\n"
                "- 方式3：web_fetch(url='file://workspace/xxx.docx', mode='rich') → 截图+文本一体\n"
                "- view_file 只能处理纯文本和图片，OFFICE文档会失败！\n\n"
                "## 引用消息快捷语法\n"
                "- 用户引用消息时，message_str 中已注入 [回复 xxx | 附件=xxx, url=xxx | msg_id=xxx] 信息\n"
                "- 工具参数中可用 @quoted_file / @quoted_image / @quoted_msg / @quoted_forward 快捷引用\n"
                "- 需要查看引用消息上下文时：QQ_data_original(around_msg_id='@quoted_msg', count=10)\n"
                "- around_msg_id 会围绕该消息取前后各 count/2 条记录，📌 标记锚点消息\n\n"
                "## 合并转发消息处理\n"
                "- 收到合并转发消息时，message_str 中有 [Forward Message: id=xxx] 或 [合并转发消息(...)]\n"
                "- 使用 media_summary(content='@quoted_forward', media_type='forward') 自动拉取并AI总结\n"
                "- 使用 media_summary(content='@quoted_forward', extract_raw=true) 提取原文到Sandbox文件（返回路径，用view_file查看）\n"
                "- 支持最深5层嵌套转发递归展开\n"
                "- 转发内的小图片(≤5MB)和小文件(≤10MB)自动下载到Sandbox，图片和PDF会自动用AI分析内容\n"
                "- 视频(media_type='video')支持mp4/webm/mkv/mov等自动MIME检测，时长分级处理（>60min将被拒绝）"
            )

            # 9. Sandbox 环境说明
            try:
                sandbox_env_path = os.path.join(
                    SANDBOX_ROOT, "config", "env.json"
                )
                if os.path.exists(sandbox_env_path):
                    import json as _json
                    with open(sandbox_env_path, 'r', encoding='utf-8') as _f:
                        env_info = _json.load(_f)
                    # 真文件字段：sandbox_version/total_storage_mb/ram_limit_mb/
                    #   exec_timeout_default_ms/exec_timeout_max_ms/available_languages/tool_count/custom_tool_count
                    _langs = env_info.get('available_languages', [])
                    _lang_str = " ".join(_langs) if isinstance(_langs, list) else str(_langs)
                    _to_default_ms = env_info.get('exec_timeout_default_ms', 30000)
                    _to_max_ms = env_info.get('exec_timeout_max_ms', 300000)
                    try:
                        _to_default_s = int(_to_default_ms) // 1000
                    except (TypeError, ValueError):
                        _to_default_s = 30
                    try:
                        _to_max_s = int(_to_max_ms) // 1000
                    except (TypeError, ValueError):
                        _to_max_s = 300
                    dynamic_parts.append(
                        f"## Sandbox 环境\n"
                        f"沙盒版本: {env_info.get('sandbox_version', 'N/A')}\n"
                        f"可用语言: {_lang_str}\n"
                        f"执行超时: sandbox_exec 默认 {_to_default_s}s 上限 {_to_max_s}s(可通过 timeout_ms 参数指定)\n"
                        f"内存: 单次执行上限 {env_info.get('ram_limit_mb', 256)}MB\n"
                        f"存储: workspace/ 可自由读写 总容量 {env_info.get('total_storage_mb', 512)}MB\n"
                        f"工具数: {env_info.get('tool_count', 0)}(自定义 {env_info.get('custom_tool_count', 0)})\n"
                        f"核心已装包: aiohttp PIL pdfplumber openpyxl pandas matplotlib numpy requests bs4\n"
                        f"需要其他包: sandbox_exec 执行 pip install 自行安装即可"
                    )  # 动态
            except Exception:
                pass

            # === KVCache 优化：静态部分注入 system_prompt，动态部分注入 contents ===

            # 静态部分 → system_prompt（稳定不变，提高隐式 KVCache 命中率）
            if inject_parts and req.system_prompt:
                injection = "\n\n".join(inject_parts)
                req.system_prompt = f"{req.system_prompt}\n\n{injection}"
            elif inject_parts:
                req.system_prompt = "\n\n".join(inject_parts)

            # 动态部分 → contents 第一条 user message 前缀
            if dynamic_parts:
                dynamic_block = "\n\n".join(dynamic_parts) + "\n\n---\n\n"
                if hasattr(req, 'contexts') and req.contexts:
                    # 找到第一条 user message 并拼入动态前缀
                    for i, msg in enumerate(req.contexts):
                        msg_role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
                        if msg_role == "user":
                            if isinstance(msg, dict):
                                old_content = msg.get("content", "")
                                msg["content"] = f"{dynamic_block}{old_content}"
                            break
                    else:
                        # 没有 user message，插入一条
                        req.contexts.insert(0, {"role": "user", "content": dynamic_block.rstrip("\n\n---\n\n")})

            if inject_parts or dynamic_parts:
                logger.info(
                    f"[on_llm_request] ✅ 注入 static={len(inject_parts)} dynamic={len(dynamic_parts)} "
                    f"(Knowledge={'Y' if knowledge_text else 'N'}, "
                    f"Summary={'Y' if summary else 'N'}, "
                    f"T-File={'Y' if t_file_active else 'N'}, "
                    f"Tools=Y, system_len={len(req.system_prompt or '')})"
                )
            else:
                logger.warning("[on_llm_request] ⚠️ inject_parts 为空，未注入任何内容")

        except Exception as e:
            import traceback
            logger.error(
                f"[on_llm_request] ❌ 注入失败: {e}\n"
                f"  agent_builder={self._agent_builder is not None}, "
                f"  tool_registry={self._tool_registry is not None}\n"
                f"  {traceback.format_exc()}"
            )

