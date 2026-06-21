"""
Sandbox 安全执行管理器
提供路径白名单验证、安全文件操作、沙盒化代码执行

安全原则：
- Sandbox 外部绝对禁止 AI 触碰
- 所有文件操作必须在 Sandbox/ 目录树内
- 路径逃逸检测（..、绝对路径、符号链接）
- 执行环境 PATH 隔离

文档: Plan_1_sandbox.md
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# Sandbox 根目录（相对于项目根）
SANDBOX_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "Sandbox")
)


class SandboxSecurity:
    """路径安全检查器

    确保所有操作都在 Sandbox 内部，防止逃逸
    """

    def __init__(self, sandbox_root: str = SANDBOX_ROOT):
        self._root = os.path.realpath(sandbox_root)
        self._limits = self._load_limits()
        self._review_mode = False  # 问题4修复: 初始化 review_mode 避免 AttributeError

    def _load_limits(self) -> Dict[str, Any]:
        """加载限制配置"""
        limits_path = os.path.join(self._root, "config", "limits.json")
        try:
            with open(limits_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError):
            return {}

    def validate_path(self, path: str, allow_write: bool = False) -> Tuple[bool, str]:
        """验证路径是否在 Sandbox 内且符合权限

        Returns:
            (is_valid, error_message_or_resolved_path)
        """
        # 拒绝绝对路径
        if os.path.isabs(path):
            return False, "绝对路径不允许，请使用 Sandbox 内的相对路径"

        # 构建完整路径
        full_path = os.path.normpath(os.path.join(self._root, path))
        real_path = os.path.realpath(full_path)

        # 路径逃逸检测（修复 Codex 问题1: startswith 可被同前缀兄弟目录绕过）
        # 使用 os.sep 后缀确保精确匹配，而非前缀匹配
        sandbox_prefix = self._root + os.sep
        if real_path != self._root and not real_path.startswith(sandbox_prefix):
            return False, f"路径逃逸检测: '{path}' 解析到 Sandbox 外部"

        # 符号链接检测
        if os.path.islink(full_path):
            link_target = os.path.realpath(full_path)
            if link_target != self._root and not link_target.startswith(sandbox_prefix):
                return False, f"符号链接指向 Sandbox 外部: {link_target}"

        # 路径深度检查
        max_depth = self._limits.get("security", {}).get("max_path_depth", 10)
        rel = os.path.relpath(real_path, self._root)
        depth = len(Path(rel).parts)
        if depth > max_depth:
            return False, f"路径深度超限: {depth} > {max_depth}"

        # 写入权限检查
        if allow_write:
            rel_parts = Path(rel).parts
            if rel_parts and rel_parts[0] == "base_tools":
                if len(rel_parts) >= 2 and rel_parts[1] == "system_report":
                    # base_tools/system_report/ 仅 Review 模式下可写
                    if not self._review_mode:
                        return False, "base_tools/system_report/ 仅在 Review 模式下可写入"
                else:
                    return False, "base_tools 目录为只读"
            if rel_parts and rel_parts[0] == "config":
                return False, "config 目录为只读"

        return True, real_path

    def resolve_path(self, relative_path: str) -> str:
        """解析相对路径到绝对路径（带安全检查）"""
        valid, result = self.validate_path(relative_path)
        if not valid:
            raise PermissionError(result)
        return result


class SandboxManager:
    """Sandbox 管理器

    提供安全的文件操作和代码执行
    """

    def __init__(self, sandbox_root: str = SANDBOX_ROOT):
        self._root = os.path.realpath(sandbox_root)
        self._security = SandboxSecurity(sandbox_root)
        self._env = self._load_env()
        self._review_mode = False  # Review 模式标记——system_report 临时开放写权限
        self._command_whitelist_enabled = True  # C-1: 命令白名单开关
        # 问题4修复: 同步 review_mode 到 SandboxSecurity
        self._security._review_mode = self._review_mode

    # === C-1: 命令白名单 ===

    # 允许的命令前缀（小写匹配）
    _COMMAND_WHITELIST = {
        # 包管理
        "pip", "pip3", "pip.exe",
        # Python 执行
        "python", "python3", "python.exe", "py",
        # 文件查看/搜索
        "grep", "find", "findstr", "dir", "ls",
        "type", "cat", "head", "tail", "more", "less",
        "where", "which", "wc",
        # 文件信息
        "file", "stat", "du",
        # 输出/测试
        "echo", "printf", "test",
        # Node.js
        "node", "node.exe", "npm", "npx",
    }

    # 显式拒绝的危险命令
    _COMMAND_BLACKLIST = {
        "rm", "rmdir", "del", "rd",  # 删除
        "curl", "wget", "invoke-webrequest", "iwr",  # 网络请求
        "net", "netsh", "nslookup",  # 网络配置
        "reg", "regedit",  # 注册表
        "sc", "taskkill", "tasklist",  # 进程/服务
        "shutdown", "restart",  # 系统控制
        "format", "diskpart",  # 磁盘
        "powershell", "pwsh", "cmd",  # Shell 逃逸
        "ssh", "scp", "ftp",  # 远程
        "chmod", "chown", "sudo", "su",  # 权限提升
    }

    def _check_command_whitelist(self, command: str) -> tuple:
        """检查命令是否在白名单内
        
        Returns:
            (is_blocked: bool, reason: str)
        """
        # 提取第一个 token（命令名）
        cmd_stripped = command.strip()
        if not cmd_stripped:
            return True, "空命令"
        
        # 处理管道和链式命令：检查所有子命令
        # 分割 &&, ||, ;, | 并检查每个子命令
        import re
        sub_commands = re.split(r'\s*(?:&&|\|\||;|\|)\s*', cmd_stripped)
        
        for sub_cmd in sub_commands:
            sub_cmd = sub_cmd.strip()
            if not sub_cmd:
                continue
            
            # 提取命令名（第一个 token）
            first_token = sub_cmd.split()[0].lower()
            # 去除路径前缀，只看文件名
            cmd_name = os.path.basename(first_token)
            # 去除 .exe 后缀用于匹配
            cmd_base = cmd_name.replace(".exe", "")
            
            # 先检查黑名单（优先级最高）
            if cmd_base in self._COMMAND_BLACKLIST:
                return True, f"禁止执行危险命令: {cmd_name}"
            
            # 再检查白名单
            if cmd_base not in self._COMMAND_WHITELIST and cmd_name not in self._COMMAND_WHITELIST:
                return True, f"未知命令: {cmd_name}，不在安全命令列表中"
        
        return False, ""

    def _load_env(self) -> Dict[str, Any]:
        """加载环境配置"""
        env_path = os.path.join(self._root, "config", "env.json")
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError):
            return {}

    # ========================
    # 文件操作
    # ========================

    # 图片扩展名集合
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"}
    # 二进制文档扩展名
    BINARY_DOC_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx"}

    async def view_file(
        self, path: str, start_line: int = 1, end_line: Optional[int] = None
    ) -> str:
        """查看文件内容。支持纯文本、图片、PDF和Office文档。

        - 纯文本：返回指定行范围
        - 图片：自动缩放优化后返回 base64 描述信息
        - PDF：提取文本内容
        - Office文档：提取纯文本
        """
        real_path = self._security.resolve_path(path)

        if not os.path.isfile(real_path):
            raise FileNotFoundError(f"文件不存在: {path}")

        ext = os.path.splitext(real_path)[1].lower()

        # 图片文件：优化处理后返回 base64
        if ext in self.IMAGE_EXTENSIONS:
            return await self._view_image(real_path, path)

        # PDF/Office 文档：提取文本（按扩展名）
        if ext in self.BINARY_DOC_EXTENSIONS:
            return await self._view_document(real_path, path, ext)

        # 魔数检测：QQ 文件名不可靠，.md/.txt 可能实际是 PDF/DOCX
        try:
            with open(real_path, "rb") as bf:
                magic = bf.read(8)
            if magic[:5] == b"%PDF-":
                logger.info(f"[view_file] 魔数检测: {path} 扩展名={ext} 但实际是 PDF")
                return await self._view_document(real_path, path, ".pdf")
            if magic[:4] == b"PK\x03\x04":
                # PK 头 = ZIP 容器 (DOCX/XLSX/PPTX)
                name_lower = os.path.basename(real_path).lower()
                if "xlsx" in name_lower or "xls" in name_lower:
                    detected_ext = ".xlsx"
                elif "pptx" in name_lower or "ppt" in name_lower:
                    detected_ext = ".pptx"
                else:
                    detected_ext = ".docx"  # 默认当 DOCX 处理
                logger.info(f"[view_file] 魔数检测: {path} 扩展名={ext} 但实际是 {detected_ext}")
                return await self._view_document(real_path, path, detected_ext)
        except Exception as e:
            logger.debug(f"[view_file] 魔数检测失败: {e}")

        # 纯文本文件
        MAX_LINES_PER_PAGE = 200

        with open(real_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        user_specified_range = end_line is not None

        if end_line is None:
            # 自动分页：首次读取大文件时只返回前 MAX_LINES_PER_PAGE 行
            if total_lines > MAX_LINES_PER_PAGE:
                end_line = start_line + MAX_LINES_PER_PAGE - 1
            else:
                end_line = total_lines

        start_line = max(1, start_line)
        end_line = min(end_line, total_lines)

        selected = lines[start_line - 1 : end_line]
        content = "".join(selected)

        # 附加分页元信息
        if not user_specified_range and total_lines > MAX_LINES_PER_PAGE:
            remaining = total_lines - end_line
            content += (
                f"\n\n--- 📄 分页提示 ---\n"
                f"文件共 {total_lines} 行，当前显示第 {start_line}-{end_line} 行。"
                f"剩余 {remaining} 行未显示。\n"
                f"如需继续阅读，请调用 view_file(path=\"{path}\", start_line={end_line + 1})"
            )
        elif user_specified_range:
            content += f"\n\n[第 {start_line}-{end_line} 行 / 共 {total_lines} 行]"

        return content

    async def view_files_batch(self, paths: list) -> str:
        """批量读取多个文件，返回合并结果"""
        results = []
        for p in paths[:10]:  # 最多10个文件
            try:
                content = await self.view_file(p)
                # 每个文件截断到 2000 字符
                if len(content) > 2000:
                    content = content[:2000] + f"\n... (截断，共 {len(content)} 字符)"
                results.append(f"=== {p} ===\n{content}")
            except Exception as e:
                results.append(f"=== {p} ===\n错误: {e}")
        return "\n\n".join(results)

    async def _view_image(self, real_path: str, display_path: str) -> str:
        """处理图片文件：优化大小后返回 base64 信息"""
        import base64
        file_size = os.path.getsize(real_path)

        try:
            from PIL import Image
            img = Image.open(real_path)
            width, height = img.size
            fmt = img.format or "PNG"

            # 自动缩放到 1024px 以内（保持纵横比）
            max_dim = 1024
            if width > max_dim or height > max_dim:
                ratio = min(max_dim / width, max_dim / height)
                new_w, new_h = int(width * ratio), int(height * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                width, height = new_w, new_h

            # 转 JPEG 压缩（除 PNG 透明图外）
            import io
            buf = io.BytesIO()
            if img.mode == "RGBA":
                img.save(buf, format="PNG", optimize=True)
                mime = "image/png"
            else:
                img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=80)
                mime = "image/jpeg"
            b64_data = base64.b64encode(buf.getvalue()).decode()
            optimized_size = len(buf.getvalue())

            return (
                f"[图片] {display_path}\n"
                f"尺寸: {width}x{height}, 原始大小: {file_size}B, 优化后: {optimized_size}B\n"
                f"data:{mime};base64,{b64_data}"
            )
        except ImportError:
            # PIL 不可用，回退到原始 base64
            with open(real_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode()
            ext = os.path.splitext(real_path)[1].lower()
            mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
            mime = mime_map.get(ext, "image/png")
            return (
                f"[图片] {display_path}\n"
                f"大小: {file_size}B (未优化，PIL不可用)\n"
                f"data:{mime};base64,{b64_data}"
            )

    async def _view_document(self, real_path: str, display_path: str, ext: str) -> str:
        """处理 PDF/Office 文档：提取文本内容"""
        file_size = os.path.getsize(real_path)
        MAX_TEXT = 10000  # 截断上限

        if ext == ".pdf":
            return await self._extract_pdf_text(real_path, display_path, file_size, MAX_TEXT)
        elif ext == ".docx":
            return self._extract_docx_text(real_path, display_path, file_size, MAX_TEXT)
        else:
            # xlsx/pptx/doc 等暂不支持提取，返回文件信息
            return (
                f"[文档] {display_path}\n"
                f"类型: {ext}, 大小: {file_size}B\n"
                f"暂不支持 {ext} 文本提取。建议使用 save_data 将文件保存后用其他工具处理。"
            )

    async def _extract_pdf_text(self, real_path: str, display_path: str, file_size: int, max_text: int) -> str:
        """从 PDF 提取纯文本（含假 PDF 检测）"""
        # === 文件头校验：检测假 PDF ===
        try:
            with open(real_path, "rb") as f:
                header = f.read(20)
            if not header.startswith(b"%PDF-"):
                # 不是真正的 PDF！尝试作为文本读取
                logger.warning(f"[_extract_pdf_text] {display_path} 文件头不是 %PDF-，尝试文本读取")
                try:
                    with open(real_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(max_text)
                    if content.strip():
                        return (
                            f"⚠️ 注意: {display_path} 扩展名为 .pdf 但实际不是 PDF 文件！\n"
                            f"文件可能是下载链接失效后返回的文本/HTML内容。\n"
                            f"建议: 请让用户重新发送文件。\n\n"
                            f"--- 文件实际内容（前 {len(content)} 字符）---\n"
                            f"{content}"
                        )
                except Exception:
                    pass
                return (
                    f"[PDF] {display_path}\n"
                    f"大小: {file_size}B\n"
                    f"⚠️ 此文件不是有效的 PDF（文件头不匹配）。可能是下载链接已失效。"
                )
        except Exception:
            pass

        text = ""
        page_count = 0
        method = "unknown"

        # 优先使用 pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(real_path) as pdf:
                page_count = len(pdf.pages)
                pages_text = []
                for i, page in enumerate(pdf.pages):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        pages_text.append(f"--- 第 {i+1} 页 ---\n{page_text}")
                    if sum(len(t) for t in pages_text) > max_text:
                        break
                text = "\n\n".join(pages_text)
                method = "pdfplumber"
        except Exception:
            # 备选 PyPDF2
            try:
                import PyPDF2
                with open(real_path, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    page_count = len(reader.pages)
                    pages_text = []
                    for i, page in enumerate(reader.pages):
                        page_text = page.extract_text() or ""
                        if page_text.strip():
                            pages_text.append(f"--- 第 {i+1} 页 ---\n{page_text}")
                        if sum(len(t) for t in pages_text) > max_text:
                            break
                    text = "\n\n".join(pages_text)
                    method = "PyPDF2"
            except Exception as e2:
                return (
                    f"[PDF] {display_path}\n"
                    f"大小: {file_size}B\n"
                    f"文本提取失败: {e2}\n"
                    f"建议: 可通过 web_fetch(url='file://workspace/路径', mode='rich') 截图查看，"
                    f"或者 sandbox_exec 安装所需库后用 Python 脚本处理。"
                )

        if not text.strip():
            return (
                f"[PDF] {display_path}\n"
                f"大小: {file_size}B, 页数: {page_count}\n"
                f"提取到空文本。此 PDF 可能是扫描件（图片型），需要 OCR 才能提取文字。"
            )

        if len(text) > max_text:
            text = text[:max_text] + f"\n\n... (截断，共 {len(text)} 字符)"

        return (
            f"[PDF] {display_path}\n"
            f"大小: {file_size}B, 页数: {page_count}, 提取方法: {method}\n\n"
            f"{text}"
        )

    def _extract_docx_text(self, real_path: str, display_path: str, file_size: int, max_text: int) -> str:
        """从 DOCX 提取纯文本"""
        try:
            import docx
            doc = docx.Document(real_path)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n".join(paragraphs)
            if len(text) > max_text:
                text = text[:max_text] + f"\n\n... (截断，共 {len(text)} 字符)"
            return (
                f"[DOCX] {display_path}\n"
                f"大小: {file_size}B, 段落数: {len(paragraphs)}\n\n"
                f"{text}"
            )
        except Exception as e:
            return (
                f"[DOCX] {display_path}\n"
                f"大小: {file_size}B\n"
                f"DOCX 提取失败: {e}\n"
                f"⚠️ 请改用 web_fetch(url='file://{display_path}', mode='text') 提取内容，"
                f"或 web_fetch(url='file://{display_path}', mode='rich') 截图+文本。"
            )

    async def modify_file(
        self, path: str, content: str, mode: str = "write"
    ) -> bool:
        """修改文件

        Args:
            path: 文件路径
            content: 文件内容
            mode: "write"(覆盖) 或 "append"(追加)
        """
        real_path = self._security.resolve_path(path)

        # 检查写入权限
        valid, msg = self._security.validate_path(path, allow_write=True)
        if not valid:
            # Review 模式下放行（system_report 临时开放写权限）
            if self._review_mode:
                logger.info(f"ReviewMode: 放行写入 {path}")
            else:
                raise PermissionError(msg)

        # 确保目录存在
        os.makedirs(os.path.dirname(real_path), exist_ok=True)

        # 文件大小限制
        max_size = (
            self._security._limits.get("storage", {}).get("max_single_file_mb", 50)
            * 1024
            * 1024
        )
        if len(content.encode("utf-8")) > max_size:
            raise ValueError(f"文件内容超过限制 ({max_size // 1024 // 1024}MB)")

        write_mode = "a" if mode == "append" else "w"
        with open(real_path, write_mode, encoding="utf-8") as f:
            f.write(content)

        return True

    async def list_files(self, path: str = "workspace") -> List[Dict[str, Any]]:
        """列出目录内容"""
        real_path = self._security.resolve_path(path)

        if not os.path.isdir(real_path):
            raise FileNotFoundError(f"目录不存在: {path}")

        result = []
        for entry in os.scandir(real_path):
            info = {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "path": os.path.relpath(entry.path, self._root).replace("\\", "/"),
            }
            if entry.is_file():
                info["size_bytes"] = entry.stat().st_size
            result.append(info)

        return result

    # ========================
    # 代码执行
    # ========================

    # 并发执行计数器
    _active_exec_count = 0
    _active_exec_lock = asyncio.Lock() if asyncio else None

    async def exec_code(
        self,
        code: str = "",
        language: str = "python",
        timeout_ms: int = 30000,
        cwd: str = "workspace/scripts",
        command: str = "",
    ) -> Dict[str, Any]:
        """在 Sandbox 内安全执行代码或系统命令

        Args:
            code: 代码内容（和 command 二选一）
            language: python / node / bash / shell / cmd
            timeout_ms: 超时毫秒数
            cwd: 工作目录（相对于 Sandbox）
            command: 系统命令（和 code 二选一，如 'pip install xxx'）
        """
        # code/command 互斥校验
        if not code and not command:
            return {"success": False, "error": "必须提供 code 或 command 参数"}

        # === 修复补充问题A: cwd 安全校验 ===
        cwd_parts = Path(cwd).parts
        if cwd_parts and cwd_parts[0] in ("base_tools", "config"):
            return {"success": False, "error": f"禁止在 {cwd_parts[0]}/ 目录下执行代码"}

        # 验证工作目录
        work_dir = self._security.resolve_path(cwd)
        os.makedirs(work_dir, exist_ok=True)

        # === 修复 Codex 问题2: 并发限制 ===
        limits = self._security._limits
        max_concurrent = limits.get("execution", {}).get("max_concurrent", 3)

        if SandboxManager._active_exec_lock is None:
            SandboxManager._active_exec_lock = asyncio.Lock()

        async with SandboxManager._active_exec_lock:
            if SandboxManager._active_exec_count >= max_concurrent:
                return {"success": False, "error": f"并发执行数已达上限 ({max_concurrent})"}
            SandboxManager._active_exec_count += 1

        try:
            return await self._exec_code_inner(code, command, language, timeout_ms, work_dir, limits)
        finally:
            async with SandboxManager._active_exec_lock:
                SandboxManager._active_exec_count -= 1

    async def _exec_code_inner(
        self,
        code: str,
        command: str,
        language: str,
        timeout_ms: int,
        work_dir: str,
        limits: Dict[str, Any],
    ) -> Dict[str, Any]:
        """实际执行代码或命令（内部方法）"""
        # === 超时限制 ===
        max_timeout = limits.get("execution", {}).get("max_timeout_ms", 60000)
        timeout_ms = min(timeout_ms, max_timeout)

        # 输出截断配置
        max_stdout = limits.get("execution", {}).get("max_stdout_chars", 10000)
        max_stderr = limits.get("execution", {}).get("max_stderr_chars", 5000)

        # 环境变量
        env = os.environ.copy()
        runtimes_dir = os.path.join(self._root, "base_tools", "runtimes")
        env["PATH"] = runtimes_dir + os.pathsep + env.get("PATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        for key in ["PYTHONPATH", "NODE_PATH"]:
            env.pop(key, None)

        # 网络限制
        if not limits.get("network", {}).get("allow_outbound", True):
            env["no_proxy"] = "*"
            env["http_proxy"] = "http://0.0.0.0:0"
            env["https_proxy"] = "http://0.0.0.0:0"

        script_file = None

        try:
            timeout_s = timeout_ms / 1000

            if command:
                # === C-1 修复: command 模式白名单校验 ===
                if self._command_whitelist_enabled:
                    blocked, reason = self._check_command_whitelist(command)
                    if blocked:
                        logger.warning(f"[Sandbox] 命令被白名单拦截: {command[:80]} | 原因: {reason}")
                        return {
                            "success": False,
                            "error": f"安全限制: {reason}。仅允许 pip/python/grep/find/dir/type/echo 等安全命令。",
                            "blocked": True,
                            "blocked_reason": reason,
                        }
                # === command 模式：系统命令执行 ===
                import sys
                if sys.platform == "win32":
                    if language == "bash":
                        bash_path = self._find_runtime("bash")
                        if bash_path:
                            proc = await asyncio.create_subprocess_exec(
                                bash_path, "-c", command,
                                cwd=work_dir, stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE, env=env,
                            )
                        else:
                            return {"success": False, "error": "bash 不可用，请使用 language='python' 或 'shell'"}
                    elif language == "cmd":
                        proc = await asyncio.create_subprocess_exec(
                            "cmd", "/c", f"chcp 65001 >nul & {command}",
                            cwd=work_dir, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, env=env,
                        )
                    else:
                        # 默认用 PowerShell（UTF-8 兼容好）
                        ps_cmd = f"$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; {command}"
                        proc = await asyncio.create_subprocess_exec(
                            "powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd,
                            cwd=work_dir, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, env=env,
                        )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        "sh", "-c", command,
                        cwd=work_dir, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, env=env,
                    )
            else:
                # === code 模式：写临时脚本执行 ===
                # 代码大小限制
                max_code_size = limits.get("execution", {}).get("max_code_size_kb", 100) * 1024
                if len(code.encode("utf-8")) > max_code_size:
                    return {"success": False, "error": f"代码大小超过限制 ({max_code_size // 1024}KB)"}

                runtime = self._find_runtime(language)
                if not runtime:
                    return {"success": False, "error": f"不支持的语言: {language}。支持: python, node, bash, shell, cmd"}

                ext_map = {"python": ".py", "node": ".js", "bash": ".sh", "shell": ".sh", "cmd": ".bat"}
                script_file = os.path.join(
                    work_dir, f"_sandbox_exec{ext_map.get(language, '.txt')}"
                )
                with open(script_file, "w", encoding="utf-8") as f:
                    f.write(code)

                proc = await asyncio.create_subprocess_exec(
                    runtime, script_file,
                    cwd=work_dir, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=env,
                )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                return {
                    "success": False,
                    "error": f"执行超时 ({timeout_ms}ms)",
                    "killed": True,
                    "kill_reason": "timeout",
                }

            stdout_str = stdout.decode("utf-8", errors="replace")[:max_stdout]
            stderr_str = stderr.decode("utf-8", errors="replace")[:max_stderr]

            return {
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "error": stderr_str.strip() if proc.returncode != 0 and stderr_str.strip() else None,
            }

        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            if script_file:
                try:
                    os.remove(script_file)
                except OSError:
                    pass

    def _find_runtime(self, language: str) -> Optional[str]:
        """查找运行时解释器

        支持: python, node, bash, shell, cmd
        """
        runtimes = os.path.join(self._root, "base_tools", "runtimes")

        if language == "python":
            # 1. Sandbox runtimes
            local = os.path.join(runtimes, "python", "python.exe")
            if os.path.exists(local):
                return local
            # 2. 项目 venv
            venv_python = os.path.normpath(
                os.path.join(self._root, "..", "AstrBot", ".venv", "Scripts", "python.exe")
            )
            if os.path.exists(venv_python):
                logger.warning(f"Sandbox runtimes 无 Python，回退到项目 venv: {venv_python}")
                return venv_python
            # 3. 系统 Python
            system_py = shutil.which("python") or shutil.which("python3")
            if system_py:
                logger.warning(f"回退到系统 Python: {system_py}")
            return system_py

        elif language == "node":
            local = os.path.join(runtimes, "node", "node.exe")
            if os.path.exists(local):
                return local
            system_node = shutil.which("node")
            if system_node:
                logger.warning(f"回退到系统 Node: {system_node}")
            return system_node

        elif language in ("bash", "shell"):
            # Git Bash / WSL bash
            git_bash = r"C:\Program Files\Git\bin\bash.exe"
            if os.path.exists(git_bash):
                return git_bash
            system_bash = shutil.which("bash")
            if system_bash:
                return system_bash
            logger.warning("bash 不可用（需要安装 Git for Windows）")
            return None

        elif language == "cmd":
            return shutil.which("cmd") or "cmd"

        logger.warning(f"不支持的语言: {language}")
        return None

    # ========================
    # 统计
    # ========================

    def get_stats(self) -> Dict[str, Any]:
        """获取 Sandbox 统计"""
        workspace = os.path.join(self._root, "workspace")
        total_size = 0
        file_count = 0

        if os.path.exists(workspace):
            for root, dirs, files in os.walk(workspace):
                for f in files:
                    fp = os.path.join(root, f)
                    total_size += os.path.getsize(fp)
                    file_count += 1

        # 自定义工具数
        custom_tools = os.path.join(workspace, "custom_tools")
        custom_count = 0
        if os.path.exists(custom_tools):
            custom_count = len(os.listdir(custom_tools))

        return {
            "root": self._root,
            "workspace_size_mb": round(total_size / 1024 / 1024, 2),
            "workspace_files": file_count,
            "custom_tools": custom_count,
            "available_languages": self._env.get("available_languages", []),
        }
