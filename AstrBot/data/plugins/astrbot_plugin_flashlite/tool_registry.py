"""
ToolRegistry — 动态工具注册表

核心模块：统一管理 Sandbox 内工具的发现、声明、校验和调度。
支持 base_tools/（只读内建）和 workspace/（可写自定义）两个目录的工具自动发现。

设计原则：
- 所有 .tool.json 文件自动被发现注册
- builtin=true 的工具走 main.py @filter.llm_tool 原生路由
- builtin=false 的自定义工具通过 sandbox_exec 执行对应 .py 脚本
- AI 可在 workspace/ 中写 .tool.json + .py 来创建新工具

文档: QQBotPlan/初始讨论记录副本.md
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# Sandbox 根目录
SANDBOX_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Sandbox")
)

# 工具扫描目录
SCAN_DIRS = ["base_tools", "workspace"]

# 工具 JSON 必须字段
REQUIRED_FIELDS = {"name", "description", "parameters"}


class ToolRegistry:
    """动态工具注册表

    scan() → 注册所有工具
    dispatch() → 路由执行
    get_brief/get_full() → 渐进式披露
    """

    def __init__(self, sandbox_root: str = SANDBOX_ROOT, sandbox_mgr=None):
        self._root = os.path.realpath(sandbox_root)
        self._sandbox = sandbox_mgr
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._last_scan_ts: float = 0
        self._scan_interval = 30  # 秒，自动重扫间隔

        # 首次扫描
        self.scan()

    # ========================
    # 工具发现
    # ========================

    def scan(self) -> Dict[str, int]:
        """扫描 base_tools/ 和 workspace/ 下所有 *.tool.json

        Returns:
            {"base_tools": N, "workspace": M, "total": N+M, "errors": E}
        """
        stats = {"base_tools": 0, "workspace": 0, "total": 0, "errors": 0}
        new_tools: Dict[str, Dict[str, Any]] = {}

        for scan_dir in SCAN_DIRS:
            dir_path = os.path.join(self._root, scan_dir)
            if not os.path.isdir(dir_path):
                continue

            # 递归扫描 .tool.json
            for root, dirs, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".tool.json"):
                        continue

                    json_path = os.path.join(root, fname)
                    try:
                        tool_def = self._load_tool_json(json_path, scan_dir)
                        if tool_def:
                            name = tool_def["name"]
                            # 同名工具：base_tools 优先
                            if name in new_tools and new_tools[name]["source"] == "base_tools":
                                logger.debug(f"跳过重名自定义工具: {name}")
                                continue
                            new_tools[name] = tool_def
                            stats[scan_dir] = stats.get(scan_dir, 0) + 1
                    except Exception as e:
                        logger.warning(f"加载工具 {json_path} 失败: {e}")
                        stats["errors"] += 1

        self._tools = new_tools
        stats["total"] = len(new_tools)
        self._last_scan_ts = time.time()

        logger.info(
            f"ToolRegistry scan: {stats['base_tools']} builtin + "
            f"{stats['workspace']} custom = {stats['total']} tools "
            f"({stats['errors']} errors)"
        )
        return stats

    def _load_tool_json(self, path: str, source_dir: str) -> Optional[Dict]:
        """加载并校验单个 .tool.json"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 校验必须字段
        missing = REQUIRED_FIELDS - set(data.keys())
        if missing:
            logger.warning(f"工具 {path} 缺少字段: {missing}")
            return None

        name = data["name"]
        is_builtin = data.get("builtin", source_dir == "base_tools")

        # 查找处理器脚本
        handler_file = data.get("handler")
        handler_path = None
        if not is_builtin:
            if handler_file:
                handler_path = os.path.join(os.path.dirname(path), handler_file)
            else:
                # 默认同名 .py
                py_name = name + ".py"
                handler_path = os.path.join(os.path.dirname(path), py_name)

            if handler_path and not os.path.isfile(handler_path):
                logger.warning(f"自定义工具 {name} 缺少处理器: {handler_path}")
                handler_path = None

        return {
            "name": name,
            "description": data["description"],
            "category": data.get("category", "custom"),
            "builtin": is_builtin,
            "handler_path": handler_path,
            "json_path": path,
            "source": source_dir,
            "timeout_ms": data.get("timeout_ms", 30000),
            "parameters": data.get("parameters", {}),
            "raw": data,
        }

    def _maybe_rescan(self):
        """需要时自动重扫"""
        if time.time() - self._last_scan_ts > self._scan_interval:
            self.scan()

    # ========================
    # 工具查询
    # ========================

    def get_tool(self, name: str) -> Optional[Dict]:
        """获取单个工具定义"""
        self._maybe_rescan()
        return self._tools.get(name)

    def get_all_tools(self) -> Dict[str, Dict]:
        """获取所有工具"""
        self._maybe_rescan()
        return dict(self._tools)

    def get_custom_tools(self) -> Dict[str, Dict]:
        """只获取自定义工具"""
        self._maybe_rescan()
        return {k: v for k, v in self._tools.items() if not v["builtin"]}

    def get_builtin_tools(self) -> Dict[str, Dict]:
        """只获取内建工具"""
        return {k: v for k, v in self._tools.items() if v["builtin"]}

    # ========================
    # 渐进式披露
    # ========================

    def get_brief(self) -> str:
        """生成工具简要列表（用于 system_prompt brief 模式）"""
        self._maybe_rescan()
        lines = ["## 可用工具 (简要)"]

        # 分类输出
        categories: Dict[str, List] = {}
        for name, tool in self._tools.items():
            cat = tool.get("category", "其他")
            categories.setdefault(cat, []).append(tool)

        for cat, tools in categories.items():
            lines.append(f"\n### {cat}")
            for t in tools:
                prefix = "🔧" if t["builtin"] else "🔌"
                lines.append(f"- {prefix} `{t['name']}`: {t['description'][:60]}")

        custom_count = len(self.get_custom_tools())
        if custom_count > 0:
            lines.append(f"\n> 🔌 自定义工具({custom_count}个)通过 `run_custom_tool` 调用。")
        lines.append("> 使用工具时系统会自动展开完整参数说明。")
        return "\n".join(lines)

    def get_full(self) -> str:
        """生成工具完整说明（用于 system_prompt full 模式）"""
        self._maybe_rescan()
        lines = ["## 可用工具 (完整)"]

        for name, tool in self._tools.items():
            prefix = "[内建]" if tool["builtin"] else "[自定义]"
            lines.append(f"\n### {prefix} {name}")
            lines.append(f"**{tool['description']}**")

            params = tool["parameters"]
            props = params.get("properties", {})
            required = params.get("required", [])

            for pname, pinfo in props.items():
                req_mark = " *必须*" if pname in required else ""
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                lines.append(f"- `{pname}` ({ptype}){req_mark}: {pdesc}")

            if not tool["builtin"]:
                lines.append(f"- _调用方式_: `run_custom_tool(name=\"{name}\", args={{...}})`")

        return "\n".join(lines)

    def get_all_schemas(self) -> List[Dict]:
        """获取 Gemini API 格式的工具定义（function_declarations）"""
        self._maybe_rescan()
        declarations = []
        for name, tool in self._tools.items():
            if tool["builtin"]:
                declarations.append({
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                })
        # 自定义工具通过 run_custom_tool 暴露，不单独声明
        return [{"function_declarations": declarations}] if declarations else []

    # ========================
    # 调度执行
    # ========================

    async def dispatch(
        self, name: str, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """调度执行自定义工具

        Args:
            name: 工具名称
            args: 参数字典

        Returns:
            {"success": bool, "result": str, "error": str}
        """
        tool = self.get_tool(name)
        if not tool:
            return {"success": False, "result": "", "error": f"工具不存在: {name}"}

        if tool["builtin"]:
            return {
                "success": False,
                "result": "",
                "error": f"内建工具 {name} 请直接调用，无需通过 run_custom_tool",
            }

        if not tool["handler_path"]:
            return {
                "success": False,
                "result": "",
                "error": f"自定义工具 {name} 缺少处理器脚本 (.py)",
            }

        # 参数校验
        validation_error = self._validate_args(tool, args)
        if validation_error:
            return {"success": False, "result": "", "error": validation_error}

        # 执行
        if not self._sandbox:
            return {"success": False, "result": "", "error": "SandboxManager 不可用"}

        try:
            # 构造执行代码：导入脚本并传入参数
            args_json = json.dumps(args, ensure_ascii=False)
            handler_rel = os.path.relpath(tool["handler_path"], self._root)

            # 在 Sandbox 内执行处理器脚本
            exec_code = (
                f"import sys, json, os\n"
                f"sys.argv = ['tool', {json.dumps(args_json)}]\n"
                f"os.chdir({json.dumps(os.path.dirname(tool['handler_path']))})\n"
                f"exec(open({json.dumps(tool['handler_path'])}, encoding='utf-8').read())\n"
            )

            timeout = tool.get("timeout_ms", 30000)
            result = await self._sandbox.exec_code(
                exec_code, "python", timeout
            )

            return {
                "success": result.get("success", False),
                "result": result.get("stdout", "").strip(),
                "error": result.get("stderr", "").strip() if not result.get("success") else "",
                "exit_code": result.get("exit_code", -1),
            }

        except Exception as e:
            return {"success": False, "result": "", "error": str(e)}

    @staticmethod
    def _validate_args(tool: Dict, args: Dict) -> Optional[str]:
        """按 JSON schema 校验参数"""
        params = tool.get("parameters", {})
        required = params.get("required", [])
        properties = params.get("properties", {})

        # 检查必须参数
        for req in required:
            if req not in args:
                return f"缺少必须参数: {req}"

        # 类型检查（简易版）
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
        for key, value in args.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type in type_map:
                    if not isinstance(value, type_map[expected_type]):
                        return f"参数 {key} 类型错误: 期望 {expected_type}，得到 {type(value).__name__}"

        return None

    # ========================
    # 状态
    # ========================

    def get_stats(self) -> Dict[str, Any]:
        """获取注册表统计"""
        return {
            "total": len(self._tools),
            "builtin": len(self.get_builtin_tools()),
            "custom": len(self.get_custom_tools()),
            "last_scan": self._last_scan_ts,
            "scan_interval": self._scan_interval,
            "sandbox_root": self._root,
        }
