"""
Web Fetch 引擎 — 参照 mcp-web-fetcher 架构改造
无头浏览器 + 多模式输出 + 会话管理 + 交互

改造要点（对齐 mcp-web-fetcher）：
- Playwright chromium 无头浏览器替代 aiohttp
- 多级输出模式（text/full/compact/minimal/links/screenshot）
- SessionManager 会话复用
- 交互操作（click/type/scroll/wait/screenshot/content/visible/find/close）
- Pipeline 多步顺序执行
- HTML → Markdown 内容提取

文档: Plan_1_sandbox.md (web_fetch.tool)
"""
from __future__ import annotations  # 所有类型注解延迟求值，防止 Playwright 未安装时 NameError

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from astrbot.api import logger

# 尝试导入 playwright（可能未安装）
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    Page = Browser = BrowserContext = None  # type: ignore  # dummy 占位
    logger.warning("Playwright 未安装，web_fetch 将使用 aiohttp 降级模式")

# 尝试导入 html2text（HTML → Markdown）
try:
    import html2text
    HAS_HTML2TEXT = True
except ImportError:
    HAS_HTML2TEXT = False


# ============================================================
# HTML → 文本/Markdown 提取器
# 对齐 extractor.ts
# ============================================================
class ContentExtractor:
    """内容提取器：HTML → 纯文本/Markdown"""

    @staticmethod
    def html_to_markdown(html: str) -> str:
        """HTML → Markdown（如果 html2text 可用）"""
        if HAS_HTML2TEXT:
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = False
            h.ignore_emphasis = False
            h.body_width = 0  # 不换行
            h.skip_internal_links = True
            return h.handle(html).strip()
        else:
            return ContentExtractor.html_to_text(html)

    @staticmethod
    def html_to_text(html: str) -> str:
        """HTML → 纯文本（正则去标签）"""
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @staticmethod
    def extract_links(html: str) -> List[Dict[str, str]]:
        """提取页面链接"""
        links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE)
        result = []
        seen = set()
        for href, text in links:
            text = re.sub(r'<[^>]+>', '', text).strip()
            if href not in seen and href.startswith(('http://', 'https://')):
                seen.add(href)
                result.append({"url": href, "text": text[:100]})
        return result[:50]

    @staticmethod
    def compress_text(text: str, max_chars: int = 8000) -> str:
        """压缩文本到指定长度"""
        if len(text) <= max_chars:
            return text
        # 保留开头和结尾
        head = max_chars * 2 // 3
        tail = max_chars // 3
        return text[:head] + f"\n\n... (省略 {len(text) - max_chars} 字符) ...\n\n" + text[-tail:]


# ============================================================
# SessionManager — 会话管理
# 对齐 interact.ts SessionManager
# ============================================================
class SessionManager:
    """管理 Playwright 页面会话，支持复用"""

    def __init__(self):
        self._sessions: Dict[str, Page] = {}
        self._timeouts: Dict[str, float] = {}
        self._max_idle = 300  # 5分钟空闲超时

    async def create(self, browser_ctx: BrowserContext, url: str, timeout: int = 30000) -> str:
        """创建新会话，返回 session_id"""
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        page = await browser_ctx.new_page()
        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        self._sessions[session_id] = page
        self._timeouts[session_id] = time.time()
        logger.info(f"WebSession 创建: {session_id} → {url}")
        return session_id

    def get(self, session_id: str) -> Optional[Page]:
        """获取会话页面"""
        page = self._sessions.get(session_id)
        if page:
            self._timeouts[session_id] = time.time()
        return page

    async def close(self, session_id: str) -> bool:
        """关闭指定会话"""
        page = self._sessions.pop(session_id, None)
        self._timeouts.pop(session_id, None)
        if page:
            try:
                await page.close()
            except Exception:
                pass
            logger.info(f"WebSession 关闭: {session_id}")
            return True
        return False

    async def cleanup_idle(self):
        """清理空闲超时的会话"""
        now = time.time()
        expired = [
            sid for sid, ts in self._timeouts.items()
            if now - ts > self._max_idle
        ]
        for sid in expired:
            await self.close(sid)
        if expired:
            logger.info(f"清理 {len(expired)} 个空闲 WebSession")

    async def close_all(self):
        """关闭所有会话"""
        for sid in list(self._sessions.keys()):
            await self.close(sid)


# ============================================================
# WebFetchEngine — 核心引擎
# 对齐 mcp-web-fetcher fetch-page.ts + interact.ts + pipeline.ts
# ============================================================
class WebFetchEngine:
    """无头浏览器引擎

    优先使用 Playwright，不可用时降级为 aiohttp。
    """

    def __init__(self, sandbox_path: str = ""):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._session_mgr = SessionManager()
        self._sandbox_path = sandbox_path
        self._initialized = False
        self._use_playwright = HAS_PLAYWRIGHT

    async def init(self):
        """初始化浏览器引擎"""
        if self._initialized:
            return
        if self._use_playwright:
            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                self._context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                )
                self._initialized = True
                logger.info("WebFetchEngine 初始化完成 (Playwright)")
            except Exception as e:
                logger.warning(f"Playwright 初始化失败，降级 aiohttp: {e}")
                self._use_playwright = False
                self._initialized = True
        else:
            self._initialized = True
            logger.info("WebFetchEngine 初始化完成 (aiohttp 降级模式)")

    async def shutdown(self):
        """关闭引擎"""
        await self._session_mgr.close_all()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("WebFetchEngine 已关闭")

    # 文件类型分类
    TEXT_EXTENSIONS = {
        ".txt", ".md", ".py", ".js", ".ts", ".json", ".xml", ".csv", ".yaml", ".yml",
        ".html", ".htm", ".css", ".sh", ".bat", ".ps1", ".r", ".java", ".c", ".cpp",
        ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".sql", ".ini", ".cfg", ".conf",
        ".log", ".toml", ".env", ".gitignore", ".dockerfile",
    }
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"}
    OFFICE_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".ods", ".odp"}
    PDF_EXTENSIONS = {".pdf"}

    async def fetch_page(
        self,
        url: str,
        mode: str = "text",  # text/full/compact/minimal/links/screenshot
        timeout: int = 15000,
        scroll_count: int = 0,
    ) -> Dict[str, Any]:
        """获取网页/本地文件内容

        mode:
        - text: 默认，Markdown 正文（截断 8000 字）
        - full: 完整 Markdown（最多 50000 字）
        - compact: 压缩到 8000 字
        - minimal: 压缩到 3000 字
        - links: 提取页面链接
        - screenshot: 截图保存到 Sandbox
        """
        await self.init()

        # file:// 本地文件快捷通道（对齐 MCP fetch-page.ts L116-201）
        if url.startswith("file://"):
            return await self._fetch_local_file(url, mode, timeout)

        if not url.startswith(("http://", "https://")):
            return {"error": "URL 必须以 http://、https:// 或 file:// 开头"}

        if self._use_playwright and self._context:
            return await self._fetch_playwright(url, mode, timeout, scroll_count)
        else:
            return await self._fetch_aiohttp(url, mode, timeout)

    async def _fetch_local_file(
        self, url: str, mode: str, timeout: int
    ) -> Dict[str, Any]:
        """处理 file:// 本地文件（对齐 MCP fetch-page.ts）

        - 纯文本：直接读取
        - 图片：base64 返回
        - PDF/Office：走 Playwright 浏览器渲染
        """
        import urllib.parse
        # 解析 file:// URL 为本地路径
        file_path = urllib.parse.unquote(url.replace("file:///", "").replace("file://", ""))
        # 相对路径自动拼接 Sandbox 根目录
        if not os.path.isabs(file_path):
            if self._sandbox_path:
                file_path = os.path.join(self._sandbox_path, file_path)
                logger.info(f"[web_fetch] file:// 相对路径解析: {url} → {file_path}")
            elif os.name == "nt":
                file_path = "/" + file_path  # fallback: Windows 根路径
        file_path = os.path.normpath(file_path)

        if not os.path.isfile(file_path):
            return {"error": f"文件不存在: {file_path}"}

        ext = os.path.splitext(file_path)[1].lower()
        name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        # 1. 纯文本文件：直接读取
        if ext in self.TEXT_EXTENSIONS:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                header = f"# {name}\n\n```{ext[1:]}\n{content}\n```"
                return self._format_output(header, mode, url)
            except Exception as e:
                return {"error": f"读取文件失败: {e}"}

        # 2. 图片文件：base64 返回
        if ext in self.IMAGE_EXTENSIONS:
            import base64
            try:
                with open(file_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode()
                mime_map = {
                    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                    ".svg": "image/svg+xml", ".ico": "image/x-icon",
                }
                mime = mime_map.get(ext, "image/png")
                if mode == "screenshot":
                    return {
                        "text": f"[图片] {name} ({file_size}B)\ndata:{mime};base64,{b64_data}",
                        "url": url,
                    }
                return {
                    "text": f"# {name}\n\n[图片文件] 大小: {file_size}B, 格式: {ext}\ndata:{mime};base64,{b64_data}",
                    "url": url,
                }
            except Exception as e:
                return {"error": f"读取图片失败: {e}"}

        # 3. PDF/Office 文件
        if ext in self.PDF_EXTENSIONS or ext in self.OFFICE_EXTENSIONS:
            # 优先 Playwright 浏览器渲染
            if self._use_playwright and self._context:
                try:
                    page = await self._context.new_page()
                    try:
                        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                        await asyncio.sleep(2)
                        if mode == "screenshot":
                            return await self._take_screenshot(page, url)
                        html = await page.content()
                        text = ContentExtractor.html_to_markdown(html)
                        if len(text.strip()) < 50:
                            text = f"# {name}\n\n(文档内容较少或为图形化排版，建议使用 screenshot 模式查看)"
                        return self._format_output(text, mode, url)
                    finally:
                        await page.close()
                except Exception as e:
                    logger.warning(f"Playwright 渲染 {ext} 失败: {e}，尝试 pdfplumber 降级")

            # 降级: 用 pdfplumber 直接提取 PDF 文本
            if ext == ".pdf":
                try:
                    import pdfplumber
                    with pdfplumber.open(file_path) as pdf:
                        pages_text = []
                        for i, page in enumerate(pdf.pages[:20]):
                            pt = page.extract_text() or ""
                            if pt.strip():
                                pages_text.append(f"--- 第 {i+1} 页 ---\n{pt}")
                        if pages_text:
                            text = f"# {name} (pdfplumber 提取)\n\n" + "\n\n".join(pages_text)
                            return self._format_output(text, mode, url)
                        else:
                            logger.info(f"pdfplumber 提取空文本: {name}")
                except Exception as e:
                    logger.warning(f"pdfplumber 提取失败: {e}")

            # 最终降级: 尝试直接作为文本读取（文件可能不是真正的 PDF）
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(50000)
                if content.strip() and not content.startswith("%PDF"):
                    text = (
                        f"# {name}\n\n"
                        f"> ⚠️ 此文件扩展名为 {ext}，但实际内容为纯文本，可能是下载链接已失效或文件格式不正确\n\n"
                        f"{content}"
                    )
                    return self._format_output(text, mode, url)
            except Exception:
                pass

            return {"error": f"{ext} 文件处理失败: 无法提取文本内容。可能是下载链接已失效，建议重新发送文件。"}

        # 4. 其它文件：尝试文本读取
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(50000)
            return self._format_output(f"# {name}\n\n{content}", mode, url)
        except Exception as e:
            return {"error": f"无法读取文件 ({ext}): {e}"}

    async def _fetch_playwright(
        self, url: str, mode: str, timeout: int, scroll_count: int
    ) -> Dict[str, Any]:
        """Playwright 抓取"""
        page = await self._context.new_page()
        try:
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")

            # 滚动加载
            for _ in range(scroll_count):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(0.3)

            if mode == "screenshot":
                return await self._take_screenshot(page, url)
            elif mode == "links":
                html = await page.content()
                links = ContentExtractor.extract_links(html)
                return {"links": links, "total": len(links)}
            else:
                html = await page.content()
                text = ContentExtractor.html_to_markdown(html)
                return self._format_output(text, mode, url)
        except asyncio.TimeoutError:
            return {"error": f"页面加载超时: {url}"}
        except Exception as e:
            return {"error": f"抓取错误: {e}"}
        finally:
            await page.close()

    async def _fetch_aiohttp(self, url: str, mode: str, timeout: int) -> Dict[str, Any]:
        """aiohttp 降级抓取"""
        import aiohttp
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout / 1000),
                ) as resp:
                    if resp.status != 200:
                        return {"error": f"HTTP {resp.status}"}
                    ct = resp.headers.get("Content-Type", "")
                    if "text/html" not in ct and "application/json" not in ct:
                        return {"error": f"非文本内容: {ct}"}
                    raw = await resp.text(errors="replace")

            if mode == "links":
                links = ContentExtractor.extract_links(raw)
                return {"links": links, "total": len(links)}
            else:
                text = ContentExtractor.html_to_markdown(raw)
                return self._format_output(text, mode, url)
        except asyncio.TimeoutError:
            return {"error": f"请求超时: {url}"}
        except Exception as e:
            return {"error": f"获取错误: {e}"}

    def _format_output(self, text: str, mode: str, url: str) -> Dict[str, Any]:
        """根据 mode 格式化输出 — 对齐 fetch-page.ts formatOutput"""
        if mode == "full":
            content = text[:50000]
        elif mode == "compact":
            content = ContentExtractor.compress_text(text, 8000)
        elif mode == "minimal":
            content = ContentExtractor.compress_text(text, 3000)
        else:  # text (默认)
            content = ContentExtractor.compress_text(text, 8000)

        return {
            "content": content,
            "url": url,
            "length": len(text),
            "truncated": len(content) < len(text),
        }

    async def _take_screenshot(self, page: Page, url: str) -> Dict[str, Any]:
        """截图保存到 Sandbox"""
        ts = int(time.time() * 1000)
        filename = f"screenshot_{ts}.png"
        if self._sandbox_path:
            save_dir = os.path.join(self._sandbox_path, "workspace", "screenshots")
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)
        else:
            save_path = os.path.join(os.path.dirname(__file__), filename)

        await page.screenshot(path=save_path, full_page=False)
        return {
            "screenshot": save_path,
            "url": url,
            "size": os.path.getsize(save_path),
        }

    # ========================
    # interact — 对齐 interact.ts
    # ========================
    async def interact(
        self,
        action: str,  # click/type/scroll/wait/screenshot/content/visible/find/close
        url: str = "",
        session_id: str = "",
        selector: str = "",
        value: str = "",
        scroll_count: int = 3,
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        """页面交互操作 — 对齐 interact.ts 9 种 action"""
        await self.init()

        if not self._use_playwright or not self._context:
            return {"error": "交互模式需要 Playwright，当前为降级模式"}

        # 获取或创建会话
        page = None
        if session_id:
            page = self._session_mgr.get(session_id)
        if not page and url:
            session_id = await self._session_mgr.create(self._context, url, timeout)
            page = self._session_mgr.get(session_id)
        if not page:
            return {"error": "需要提供 url 或有效的 session_id"}

        result: Dict[str, Any] = {"session_id": session_id}

        try:
            if action == "click" and selector:
                await page.click(selector, timeout=timeout)
                result["action"] = "clicked"
            elif action == "type" and selector and value:
                await page.fill(selector, value)
                result["action"] = "typed"
            elif action == "scroll":
                if selector:
                    await page.evaluate(f'document.querySelector("{selector}")?.scrollIntoView()')
                else:
                    for _ in range(scroll_count):
                        await page.evaluate("window.scrollBy(0, window.innerHeight)")
                        await asyncio.sleep(0.2)
                result["action"] = "scrolled"
            elif action == "wait" and selector:
                await page.wait_for_selector(selector, timeout=timeout)
                result["action"] = "found"
            elif action == "screenshot":
                ss_result = await self._take_screenshot(page, page.url)
                result.update(ss_result)
            elif action == "content":
                html = await page.content()
                if selector:
                    el = await page.query_selector(selector)
                    if el:
                        html = await el.inner_html()
                text = ContentExtractor.html_to_markdown(html)
                result["content"] = ContentExtractor.compress_text(text, 8000)
            elif action == "visible":
                text = await page.evaluate("""() => {
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    return document.body.innerText.substring(0, 5000);
                }""")
                result["content"] = text
            elif action == "find" and value:
                text = await page.evaluate("() => document.body.innerText")
                count = text.lower().count(value.lower())
                # 找到匹配的上下文
                idx = text.lower().find(value.lower())
                context = ""
                if idx >= 0:
                    start = max(0, idx - 100)
                    end = min(len(text), idx + len(value) + 100)
                    context = text[start:end]
                result["matches"] = count
                result["context"] = context
            elif action == "close":
                await self._session_mgr.close(session_id)
                result["action"] = "closed"
            else:
                result["error"] = f"不支持的操作: {action}"
        except Exception as e:
            result["error"] = f"交互错误: {e}"

        return result

    # ========================
    # pipeline — 对齐 pipeline.ts
    # ========================
    async def pipeline(
        self,
        url: str,
        steps: List[Dict[str, Any]],
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        """多步顺序执行 — 对齐 pipeline.ts"""
        await self.init()

        if not self._use_playwright or not self._context:
            return {"error": "Pipeline 需要 Playwright"}

        if len(steps) > 20:
            return {"error": "最多支持 20 步"}

        session_id = await self._session_mgr.create(self._context, url, timeout)
        results = []

        try:
            for i, step in enumerate(steps):
                action = step.get("action", "")
                step_result = await self.interact(
                    action=action,
                    session_id=session_id,
                    selector=step.get("selector", ""),
                    value=step.get("value", ""),
                    scroll_count=step.get("scroll_count", 3),
                    timeout=timeout,
                )
                results.append({"step": i + 1, "action": action, **step_result})

                # 步间等待
                wait_ms = step.get("wait_ms", 0)
                if wait_ms > 0:
                    await asyncio.sleep(wait_ms / 1000)

                # 如果某步失败，中止
                if "error" in step_result:
                    break
        finally:
            await self._session_mgr.close(session_id)

        return {"results": results, "total_steps": len(results)}

    # ========================
    # fetch_html — 对齐 fetch-html.ts
    # ========================
    async def fetch_html(
        self,
        url: str,
        selector: str = "",
        timeout: int = 30000,
        scroll_count: int = 0,
    ) -> Dict[str, Any]:
        """获取原始 HTML — 对齐 fetch-html.ts"""
        await self.init()

        if url.startswith("file://"):
            # 本地文件直接读取 HTML
            import urllib.parse
            file_path = urllib.parse.unquote(url.replace("file:///", "").replace("file://", ""))
            # 相对路径自动拼接 Sandbox 根目录
            if not os.path.isabs(file_path) and self._sandbox_path:
                file_path = os.path.join(self._sandbox_path, file_path)
            file_path = os.path.normpath(file_path)
            if os.path.isfile(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        return {"html": f.read()[:100000], "url": url}
                except Exception as e:
                    return {"error": f"读取失败: {e}"}
            return {"error": f"文件不存在: {file_path}"}

        if self._use_playwright and self._context:
            try:
                page = await self._context.new_page()
                try:
                    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                    for _ in range(scroll_count):
                        await page.evaluate("window.scrollBy(0, window.innerHeight)")
                        await asyncio.sleep(0.3)
                    if selector:
                        el = await page.query_selector(selector)
                        html = await el.inner_html() if el else ""
                    else:
                        html = await page.content()
                    return {"html": html[:100000], "url": url}
                finally:
                    await page.close()
            except Exception as e:
                return {"error": f"获取 HTML 失败: {e}"}
        else:
            # 降级 aiohttp
            import aiohttp
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout / 1000)) as resp:
                        html = await resp.text()
                        return {"html": html[:100000], "url": url}
            except Exception as e:
                return {"error": f"获取 HTML 失败: {e}"}

    # ========================
    # fetch_rich — 对齐 fetch-rich.ts（截图+文本一体）
    # ========================
    async def fetch_rich(
        self,
        url: str,
        timeout: int = 30000,
        scroll_count: int = 0,
    ) -> Dict[str, Any]:
        """截图 + Markdown 文本一次返回 — 对齐 fetch-rich.ts"""
        await self.init()

        if not self._use_playwright or not self._context:
            # 降级：只返回文本
            text_result = await self.fetch_page(url, mode="compact", timeout=timeout)
            text_result["screenshot"] = None
            text_result["note"] = "Playwright 不可用，仅返回文本"
            return text_result

        try:
            page = await self._context.new_page()
            try:
                await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                for _ in range(scroll_count):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(0.3)

                # 截图
                ss_result = await self._take_screenshot(page, url)
                # 文本
                html = await page.content()
                text = ContentExtractor.html_to_markdown(html)
                text = ContentExtractor.compress_text(text, 8000)

                return {
                    "content": text,
                    "screenshot": ss_result.get("screenshot", ""),
                    "screenshot_size": ss_result.get("size", 0),
                    "url": url,
                    "length": len(text),
                }
            finally:
                await page.close()
        except Exception as e:
            return {"error": f"fetch_rich 失败: {e}"}

    # ========================
    # extract_tables — 对齐 extract-tables.ts
    # ========================
    async def extract_tables(
        self,
        url: str,
        selector: str = "",
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        """提取网页中的 HTML 表格 — 对齐 extract-tables.ts"""
        await self.init()

        html_result = await self.fetch_html(url, selector=selector, timeout=timeout)
        if "error" in html_result:
            return html_result

        html = html_result.get("html", "")
        if not html:
            return {"tables": [], "count": 0, "url": url}

        import re
        tables = []
        # 正则提取所有 <table>...</table>
        table_pattern = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
        row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
        cell_pattern = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)

        for i, table_match in enumerate(table_pattern.finditer(html)):
            table_html = table_match.group(1)
            rows = []
            for row_match in row_pattern.finditer(table_html):
                cells = []
                for cell_match in cell_pattern.finditer(row_match.group(1)):
                    # 清理 HTML 标签
                    cell_text = re.sub(r'<[^>]+>', '', cell_match.group(1)).strip()
                    cells.append(cell_text)
                if cells:
                    rows.append(cells)

            if rows:
                # 转为 Markdown 表格
                md_lines = []
                if rows:
                    md_lines.append("| " + " | ".join(rows[0]) + " |")
                    md_lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
                    for row in rows[1:]:
                        # 补齐列数
                        while len(row) < len(rows[0]):
                            row.append("")
                        md_lines.append("| " + " | ".join(row[:len(rows[0])]) + " |")

                tables.append({
                    "index": i,
                    "rows": len(rows),
                    "cols": len(rows[0]) if rows else 0,
                    "markdown": "\n".join(md_lines),
                })

        return {"tables": tables, "count": len(tables), "url": url}

    # ========================
    # batch_screenshot — 对齐 batch-screenshot.ts
    # ========================
    async def batch_screenshot(
        self,
        urls: List[str],
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        """批量截图多个 URL — 对齐 batch-screenshot.ts"""
        await self.init()

        if not self._use_playwright or not self._context:
            return {"error": "批量截图需要 Playwright"}

        results = []
        for url in urls[:10]:  # 最多10个
            try:
                page = await self._context.new_page()
                try:
                    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                    await asyncio.sleep(1)
                    ss = await self._take_screenshot(page, url)
                    results.append({"url": url, **ss})
                finally:
                    await page.close()
            except Exception as e:
                results.append({"url": url, "error": str(e)})

        return {"screenshots": results, "count": len(results)}

    # ========================
    # download — 对齐 fetch-download.ts
    # ========================
    async def download(
        self,
        url: str,
        save_path: str = "",
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        """下载文件到本地 — 对齐 fetch-download.ts"""
        import aiohttp
        HEADERS = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        MAX_SIZE = 50 * 1024 * 1024

        if not save_path:
            # 自动生成保存路径
            from urllib.parse import urlparse
            parsed = urlparse(url)
            filename = os.path.basename(parsed.path) or f"download_{int(time.time())}"
            if self._sandbox_path:
                save_dir = os.path.join(self._sandbox_path, "workspace", "downloads")
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, filename)
            else:
                save_path = os.path.join(os.path.dirname(__file__), filename)

        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout / 1000)) as resp:
                    if resp.status != 200:
                        return {"error": f"HTTP {resp.status}"}
                    total = 0
                    with open(save_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            total += len(chunk)
                            if total > MAX_SIZE:
                                os.remove(save_path)
                                return {"error": "文件超过 50MB 限制"}
                            f.write(chunk)
            return {
                "path": save_path,
                "size": os.path.getsize(save_path),
                "url": url,
            }
        except Exception as e:
            if os.path.exists(save_path):
                try: os.remove(save_path)
                except: pass
            return {"error": f"下载失败: {e}"}
