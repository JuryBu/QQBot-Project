"""
老板娘控制中心 - FastAPI 后端入口

一键启动、统一管理 AstrBot + NapCat + Boss Lady 系统
"""

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ASTRBOT_DIR = PROJECT_ROOT / "AstrBot"
NAPCAT_DIR = None

# 动态发现 NapCat 目录
for d in PROJECT_ROOT.iterdir():
    if d.is_dir() and d.name.startswith("NapCat"):
        NAPCAT_DIR = d
        break


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭生命周期"""
    print(f"[*] BossLady Console Started")
    print(f"   项目根: {PROJECT_ROOT}")
    print(f"   AstrBot: {ASTRBOT_DIR}")
    print(f"   NapCat: {NAPCAT_DIR}")
    yield
    print("[*] BossLady Console Stopped")


app = FastAPI(
    title="老板娘控制中心",
    description="Boss Lady AI Agent 统一管理控制台",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8090", "http://127.0.0.1:8090"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# API 鉴权中间件（可选：配置 BOSSLADY_TOKEN 环境变量或 .token 文件即启用）
# ============================================================

_AUTH_TOKEN = os.environ.get("BOSSLADY_TOKEN", "").strip()
if not _AUTH_TOKEN:
    _token_file = Path(__file__).resolve().parent.parent / ".token"
    if _token_file.is_file():
        _AUTH_TOKEN = _token_file.read_text(encoding="utf-8").strip()

if _AUTH_TOKEN:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse as StarletteJSONResponse

    class AuthMiddleware(BaseHTTPMiddleware):
        """简单 Bearer Token 鉴权：仅保护 /api/ 路由"""
        async def dispatch(self, request, call_next):
            if request.url.path.startswith("/api/"):
                auth = request.headers.get("Authorization", "")
                if auth != f"Bearer {_AUTH_TOKEN}":
                    return StarletteJSONResponse(
                        status_code=401,
                        content={"detail": "未认证：需要有效的 Authorization: Bearer <token> 头"},
                    )
            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    print(f"[*] API 鉴权已启用（token 来源: {'环境变量' if os.environ.get('BOSSLADY_TOKEN') else '.token 文件'}）")

# ============================================================
# 路由注册
# ============================================================

from .routers import dashboard, bot, models, cost, knowledge
from .routes import messages, data, system

app.include_router(dashboard.router, prefix="/api/dashboard", tags=["仪表盘"])
app.include_router(bot.router, prefix="/api/bot", tags=["Bot管理"])
app.include_router(models.router, prefix="/api/models", tags=["模型配置"])
app.include_router(cost.router, prefix="/api/cost", tags=["成本监控"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["Knowledge"])

# Stage 12 路由
app.include_router(messages.router, tags=["对话浏览"])
app.include_router(data.router, tags=["数据管理"])
app.include_router(system.router, tags=["系统设置"])

# ============================================================
# 静态文件 & SPA
# ============================================================

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

if (FRONTEND_DIR / "index.html").exists():
    # 只有在 assets 目录存在时才挂载
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # CSS/JS 静态文件直接服务
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        """SPA 路由回退（排除 /api/ 前缀）"""
        # /api/ 开头的未注册路由应返回 404，不应返回 index.html
        if path.startswith("api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=404,
                content={"detail": f"API endpoint /{path} not found"}
            )
        file_path = FRONTEND_DIR / path
        if file_path.is_file() and file_path.suffix in {'.css', '.js', '.png', '.ico', '.svg', '.jpg', '.webp'}:
            return FileResponse(str(file_path))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
else:
    @app.get("/")
    async def root():
        return {"status": "ok", "message": "老板娘控制中心 API 运行中", "frontend": "未构建"}


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8090, reload=True)
