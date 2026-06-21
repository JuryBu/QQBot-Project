"""
Agent 集成模块——主模型请求体构建 + 工具链定义 + 渐进式披露

核心公式:
C' = Knowledge(全局) + SystemEnv + Persona(角色设定) + Tools(渐进式)
   + CHECKPOINT压缩摘要 + 最近~10条消息 + 工具调用/结果

文档: Plan_1_architecture.md + Plan_1_models.md
"""

import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

# 确保插件目录在 sys.path 中
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import logger

# 角色设定路径
PERSONA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Sandbox", "config")
)


# ============================================================
# 工具定义 — 由 ToolRegistry 从 Sandbox/base_tools/*.tool.json 动态加载
# 旧版硬编码 TOOL_DEFINITIONS 已删除，改为运行时扫描
# ============================================================

from tool_registry import ToolRegistry


# ============================================================
# 请求体构建器
# ============================================================

class AgentRequestBuilder:
    """主模型请求体构建器

    按照架构文档的公式构建 C':
    C' = Knowledge + SystemEnv + Persona + Tools(渐进式) + 最近消息
    注意：CHECKPOINT 摘要已迁移到 T 文件系统（checkpoint.py TFileManager），
    不再通过 agent.py 注入。
    """

    def __init__(
        self,
        knowledge_cache,   # KnowledgeCache 实例
        memory_store,      # MemoryStore 实例
        checkpoint_mgr,    # CheckpointManager 实例
        sandbox_mgr=None,  # SandboxManager 实例
        tool_registry=None, # ToolRegistry 实例
    ):
        self._knowledge = knowledge_cache
        self._memory = memory_store
        self._checkpoint = checkpoint_mgr
        self._sandbox = sandbox_mgr
        self._tool_registry = tool_registry

    def build_system_instruction(
        self,
        window_key: str,
        persona_prompt: str = "",
        tool_mode: str = "brief",
    ) -> str:
        """构建系统指令（systemInstruction）

        Args:
            window_key: 当前窗口标识
            persona_prompt: 角色设定内容
            tool_mode: "brief" 只给工具列表, "full" 展开完整参数
        """
        parts = []

        # 1. 角色设定
        if persona_prompt:
            parts.append(f"## 角色设定\n{persona_prompt}")

        # 2. 系统环境说明
        parts.append(self._build_system_env())

        # 3. Knowledge 全局缓存
        knowledge_text = self._knowledge.get_formatted()
        if knowledge_text:
            parts.append(knowledge_text)

        # 4. 工具系统说明（渐进式披露）
        parts.append(self._build_tool_section(tool_mode))

        return "\n\n".join(parts)

    async def build_contents(
        self,
        window_key: str,
        recent_messages: Optional[List[Dict]] = None,
        keep_recent: int = 10,
    ) -> List[Dict[str, Any]]:
        """构建请求内容（contents 数组）

        Returns:
            Gemini API 格式的 contents 数组
        """
        contents = []

        # 1. CHECKPOINT 摘要已迁移到 T 文件系统（checkpoint.py TFileManager）
        # T 文件通过 on_llm_request 钩子直接替换 req.contexts，
        # 不再需要在此处手动注入 CHECKPOINT 摘要。

        # 2. 最近 ~10 条未压缩消息
        if recent_messages:
            for msg in recent_messages[-keep_recent:]:
                role = "model" if msg.get("is_bot") else "user"
                text = msg.get("content", "")
                sender = msg.get("sender_name", "")
                if sender and role == "user":
                    text = f"[{sender}] {text}"

                parts = [{"text": text}]

                # 多模态支持
                if msg.get("image_url"):
                    parts.append({
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": msg["image_url"],  # base64 或 URL
                        }
                    })

                contents.append({"role": role, "parts": parts})

        return contents

    # ========================
    # 内部方法
    # ========================

    def _build_system_env(self) -> str:
        """构建系统环境说明——从 Sandbox/config 动态读取"""
        now = datetime.now()

        # 动态读取 Sandbox 配置
        env_info = {}
        limits_info = {}
        try:
            # PERSONA_DIR = Sandbox/config/，env.json 和 limits.json 就在这个目录下
            env_path = os.path.join(PERSONA_DIR, "env.json")
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    env_info = json.load(f)
            limits_path = os.path.join(PERSONA_DIR, "limits.json")
            if os.path.exists(limits_path):
                with open(limits_path, "r", encoding="utf-8") as f:
                    limits_info = json.load(f)
        except Exception:
            pass

        storage_mb = env_info.get("total_storage_mb", 512)
        ram_mb = env_info.get("ram_limit_mb", 256)
        timeout_s = env_info.get("exec_timeout_default_ms", 30000) // 1000
        max_timeout_s = env_info.get("exec_timeout_max_ms", 300000) // 1000
        languages = env_info.get("available_languages", ["python", "node"])
        tool_count = env_info.get("tool_count", 20)

        exec_limits = limits_info.get("execution", {})
        max_concurrent = exec_limits.get("max_concurrent", 3)
        net_info = limits_info.get("network", {})

        return (
            "## 系统环境\n"
            f"- 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S %A')}\n"
            f"- 平台: QQ (via NapCat + AstrBot)\n"
            "\n"
            "### Sandbox 虚拟工作空间\n"
            "你拥有一个隔离的 Sandbox 虚拟空间，可以自由读写文件、执行代码、管理数据：\n"
            f"- 存储: {storage_mb}MB (workspace/ 可读写创建)\n"
            f"- 运行内存: {ram_mb}MB\n"
            f"- 执行超时: 默认 {timeout_s}s，最大 {max_timeout_s}s\n"
            f"- 可用语言: {', '.join(languages)}\n"
            f"- 并发限制: 最多 {max_concurrent} 个任务同时执行\n"
            f"- 网络: {'允许出站' if net_info.get('allow_outbound', True) else '禁止出站'}\n"
            f"- 安全: Sandbox 外无文件操作权限，路径自动校验\n"
            "\n"
            "### 可用系统\n"
            f"- 🔧 工具系统: {tool_count} 个内建工具 (使用工具时自动展开参数)\n"
            "- 📚 Memory: SQLite 持久化记忆系统 (memory_write/query/read/update)\n"
            "- 📖 Knowledge: 实时话题摘要缓存\n"
            "- 📦 workspace/: 你可以在这里自由创建文件、脚本，甚至编写新的自定义工具\n"
            "- ⏰ Task: 支持后台任务管理 (task_set)\n"
        )

    def _build_tool_section(self, mode: str = "brief") -> str:
        """构建工具说明（渐进式披露）— 从 ToolRegistry 动态读取"""
        if not self._tool_registry:
            return "## 工具系统未初始化"

        if mode == "brief":
            return self._tool_registry.get_brief()
        else:
            return self._tool_registry.get_full()

    async def _get_checkpoint_summary(self, window_key: str) -> Optional[str]:
        """[已废弃] 获取最新的 CHECKPOINT 压缩摘要

        已迁移到 T 文件系统（checkpoint.py TFileManager）。
        保留此方法避免其他可能的外部调用报错，但始终返回 None。
        """
        return None

    def get_tool_schemas_for_api(self) -> List[Dict[str, Any]]:
        """获取 Gemini API 格式的工具定义 — 从 ToolRegistry 动态读取"""
        if not self._tool_registry:
            return []
        return self._tool_registry.get_all_schemas()
