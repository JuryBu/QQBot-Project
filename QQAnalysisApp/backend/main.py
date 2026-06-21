"""
QQ Analysis App - FastAPI Backend
"""
from fastapi import FastAPI, HTTPException, File, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import uvicorn
import os
import asyncio
import uuid
import logging

from napcat_manager import napcat_manager
from onebot_server import onebot_server
from crawler import crawler
from analyzer import analyzer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()

# Mount frontend
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if not os.path.exists(FRONTEND_DIR):
    os.makedirs(FRONTEND_DIR)

app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LLMConfig(BaseModel):
    base_url: str
    api_key: str
    model: str


class AnalysisSettings(BaseModel):
    history_days: int = 7
    context_msg_count: int = 50
    qzone_count: int = 10


class CrawlRequest(BaseModel):
    target_id: int
    is_group: bool = False
    group_id: Optional[int] = None
    known_info: Optional[str] = ""
    settings: Optional[AnalysisSettings] = None


class SendMsgRequest(BaseModel):
    target_id: int
    is_group: bool
    type: str
    content: str


# --- Lifecycle Events ---
@app.on_event("startup")
async def startup_event():
    """Start OneBotServer when FastAPI starts."""
    try:
        # Run in background to avoid blocking FastAPI startup
        asyncio.create_task(onebot_server.start())
        logger.info("OneBotServer background task scheduled.")
    except Exception as e:
        logger.error(f"Failed to schedule OneBotServer: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    await onebot_server.stop()
    napcat_manager.stop()


# --- API Routes ---

# WebSocket for Frontend Real-time Messages
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for frontend to receive real-time messages."""
    await onebot_server.add_frontend_client(websocket)
    try:
        while True:
            await websocket.receive_text()  # Keep alive
    except Exception:
        pass
    finally:
        await onebot_server.remove_frontend_client(websocket)


@app.get("/")
async def read_root():
    return {"status": "ok", "message": "QQ Analysis API running"}


# --- NapCat Management ---

class LoginRequest(BaseModel):
    login_type: str = "qrcode"  # "qrcode", "quick", or "password"
    qq: Optional[str] = None
    password: Optional[str] = None


@app.get("/api/napcat/accounts")
async def get_saved_accounts():
    """Get list of QQ accounts that can use quick login."""
    return {"accounts": napcat_manager.get_saved_accounts()}


@app.post("/api/napcat/start")
async def start_napcat(request: LoginRequest = None):
    """Start NapCat with specified login method."""
    if request is None:
        request = LoginRequest()
    return napcat_manager.start(
        login_type=request.login_type,
        qq=request.qq,
        password=request.password
    )


@app.post("/api/napcat/stop")
async def stop_napcat():
    return napcat_manager.stop()


@app.get("/api/napcat/status")
async def get_napcat_status():
    """Get comprehensive status including WebSocket and bot state."""
    napcat_status = napcat_manager.get_status()
    onebot_status = onebot_server.get_status()
    
    # Merge statuses and logs
    all_logs = napcat_status.get("logs", []) + onebot_server.logs
    
    return {
        "running": napcat_status.get("running", False),
        "pid": napcat_status.get("pid"),
        "qrcode_available": napcat_status.get("qrcode_available", False),
        "ws_connected": onebot_status.get("ws_connected", False),
        "bot_online": onebot_status.get("bot_online", False),
        "login_info": onebot_status.get("login_info", {}),
        "logs": all_logs
    }


@app.get("/api/napcat/qrcode")
async def get_qrcode():
    path = napcat_manager.get_qrcode_path()
    if path and os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="QR Code not found")


# --- User Info ---
@app.get("/api/user/info")
async def get_user_info():
    """Get login info - only returns data if connected and logged in."""
    if not onebot_server.is_connected:
        return {}  # Not connected yet
    if not onebot_server.is_bot_online:
        return {}  # Connected but not logged in
    return await crawler.get_login_info()


@app.get("/api/contacts")
async def get_contacts():
    """Get contacts - only works when connected and logged in."""
    if not onebot_server.is_bot_online:
        return {"friends": [], "groups": []}
    return await crawler.get_contacts()


# --- User Data Management ---
# In-memory storage for user notes (in production, use a database)
user_notes_store = {}
user_data_cache = {}


class UserNote(BaseModel):
    user_id: int
    note: str
    images: Optional[list] = []


@app.get("/api/user/profile/{user_id}")
async def get_user_profile(user_id: int):
    """获取用户详细资料"""
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    profile = await crawler.get_profile_detail(user_id)
    # Cache for later export
    user_data_cache[user_id] = user_data_cache.get(user_id, {})
    user_data_cache[user_id]["profile"] = profile
    return profile


@app.get("/api/user/data/{user_id}")
async def get_user_data(user_id: int):
    """获取已爬取的用户所有数据"""
    cached = user_data_cache.get(user_id, {})
    notes = user_notes_store.get(user_id, {"note": "", "images": []})
    return {
        "profile": cached.get("profile", {}),
        "chat_history": cached.get("chat_history", []),
        "analysis": cached.get("analysis", {}),
        "notes": notes,
    }


@app.get("/api/user/data/{user_id}/export")
async def export_user_data(user_id: int):
    """导出用户数据为JSON"""
    from fastapi.responses import JSONResponse
    
    data = await get_user_data(user_id)
    data["exported_at"] = __import__("datetime").datetime.now().isoformat()
    data["user_id"] = user_id
    
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": f"attachment; filename=user_{user_id}_data.json"
        }
    )


@app.post("/api/user/notes")
async def save_user_note(note: UserNote):
    """保存用户备注"""
    user_notes_store[note.user_id] = {
        "note": note.note,
        "images": note.images or []
    }
    return {"status": "ok", "message": "Note saved"}


@app.get("/api/user/notes/{user_id}")
async def get_user_note(user_id: int):
    """获取用户备注"""
    return user_notes_store.get(user_id, {"note": "", "images": []})


# --- LLM Configuration ---
@app.post("/api/llm/models")
async def get_models(config: LLMConfig):
    models = await analyzer.list_models(config.base_url, config.api_key)
    return {"models": models}


@app.post("/api/llm/save")
async def save_llm_config(config: LLMConfig):
    analyzer.configure(config.base_url, config.api_key, config.model)
    return {"status": "configured"}


# --- Message Actions ---
class RecallRequest(BaseModel):
    message_id: str

@app.post("/api/message/recall")
async def recall_message(req: RecallRequest):
    """Recall (delete) a message."""
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    try:
        response = await onebot_server.call_api("delete_msg", {"message_id": int(req.message_id)})
        if response.get("status") == "ok":
            return {"status": "ok"}
        else:
            raise HTTPException(status_code=400, detail=response.get("message", "Failed to recall"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/stranger")
async def get_stranger_info(user_id: str):
    """Get stranger/user info by QQ ID."""
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    try:
        response = await onebot_server.call_api("get_stranger_info", {"user_id": int(user_id)})
        if response.get("status") == "ok":
            return response.get("data", {})
        else:
            return {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/file/download/{file_id}")
async def download_file(file_id: str):
    """Download a file by file_id."""
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    try:
        response = await onebot_server.call_api("get_file", {"file_id": file_id})
        if response.get("status") == "ok":
            file_data = response.get("data", {})
            file_path = file_data.get("file", file_data.get("path", ""))
            if file_path and os.path.exists(file_path):
                return FileResponse(file_path)
            elif file_data.get("url"):
                # Redirect to URL
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url=file_data["url"])
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/image/get")
async def get_image(file: str):
    """Get image URL from file identifier."""
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    try:
        # Try get_image API first
        response = await onebot_server.call_api("get_image", {"file": file})
        if response.get("status") == "ok":
            data = response.get("data", {})
            return {"url": data.get("url", data.get("file", ""))}
        # Fallback to get_file
        response = await onebot_server.call_api("get_file", {"file_id": file})
        if response.get("status") == "ok":
            data = response.get("data", {})
            return {"url": data.get("url", data.get("file", ""))}
        return {"url": ""}
    except Exception as e:
        logger.error(f"Failed to get image: {e}")
        return {"url": ""}


@app.get("/api/image/proxy")
async def proxy_image(url: str):
    """Proxy image from QQ CDN to bypass CORS restrictions. Auto-refreshes expired rkey."""
    import httpx
    import re
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://qq.com/",
    }
    
    async def fetch_image(image_url: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            return await client.get(image_url, headers=headers)
    
    async def get_all_rkeys() -> list:
        """Get all fresh rkeys from NapCat using nc_get_rkey API.
        Returns list of (rkey, type) tuples where type is 'private' or 'group'."""
        rkeys = []
        try:
            response = await onebot_server.call_api("nc_get_rkey", {})
            logger.info(f"nc_get_rkey raw response: {str(response)[:300]}")
            if response.get("status") == "ok":
                data = response.get("data", [])
                # nc_get_rkey returns list of rkey objects with type field
                if isinstance(data, list):
                    for item in data:
                        rkey = item.get("rkey", "")
                        rkey_type = item.get("type", "unknown")
                        # Strip any prefix like "&rkey=" or "rkey="
                        if rkey.startswith("&rkey="):
                            rkey = rkey[6:]
                        elif rkey.startswith("rkey="):
                            rkey = rkey[5:]
                        if rkey:
                            # Map type numbers to names (based on common pattern)
                            if rkey_type == 10 or rkey_type == "private":
                                rkeys.append((rkey, "private"))
                            elif rkey_type == 20 or rkey_type == "group":
                                rkeys.append((rkey, "group"))
                            else:
                                rkeys.append((rkey, str(rkey_type)))
                logger.info(f"Got {len(rkeys)} fresh rkeys: {[(r[:15]+'...', t) for r, t in rkeys]}")
        except Exception as e:
            logger.error(f"Failed to get fresh rkeys: {e}")
        return rkeys
    
    try:
        logger.info(f"Proxying image: {url[:100]}...")
        resp = await fetch_image(url)
        logger.info(f"Proxy response status: {resp.status_code}")
        
        # Check if expired - try to refresh rkey with multiple options
        if resp.status_code == 404:
            try:
                error_data = resp.json()
                if error_data.get("retmsg") == "file has expired" or "expired" in str(error_data):
                    logger.info("Image URL expired, trying to refresh rkey...")
                    fresh_rkeys = await get_all_rkeys()
                    
                    # Determine preferred rkey type based on appid in URL
                    # appid=1406 = private images, appid=1407 = group images
                    preferred_type = "private"
                    if "appid=1407" in url:
                        preferred_type = "group"
                    logger.info(f"URL has appid for {preferred_type} images")
                    
                    # Sort rkeys to try preferred type first
                    sorted_rkeys = sorted(fresh_rkeys, key=lambda x: (0 if x[1] == preferred_type else 1))
                    
                    for rkey, rkey_type in sorted_rkeys:
                        # Replace rkey parameter in URL
                        new_url = re.sub(r'rkey=[^&]+', f'rkey={rkey}', url)
                        logger.info(f"Retrying with {rkey_type} rkey: {rkey[:20]}...")
                        resp = await fetch_image(new_url)
                        if resp.status_code == 200:
                            logger.info(f"Success with {rkey_type} rkey!")
                            break
                        # If we get 400 (appid not match), skip remaining rkeys of wrong type
                        if resp.status_code == 400 and "appid" in resp.text:
                            logger.info(f"Appid mismatch with {rkey_type} rkey, skipping...")
                            continue
                        logger.info(f"Retry response status: {resp.status_code}")
            except Exception as e:
                logger.error(f"Rkey refresh failed: {e}")
        
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            from fastapi.responses import Response
            return Response(content=resp.content, media_type=content_type)
        else:
            logger.error(f"Image proxy failed with status {resp.status_code}: {resp.text[:200]}")
            raise HTTPException(status_code=resp.status_code, detail="Failed to fetch image")
    except httpx.HTTPError as e:
        logger.error(f"Image proxy HTTP error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Image proxy error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forward/get")
async def get_forward_msg(id: str):
    """Get forward message content."""
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    try:
        response = await onebot_server.call_api("get_forward_msg", {"id": id})
        if response.get("status") == "ok":
            data = response.get("data", {})
            messages = data.get("messages", data.get("message", []))
            return {"messages": messages}
        return {"messages": []}
    except Exception as e:
        logger.error(f"Failed to get forward message: {e}")
        return {"messages": []}


# --- File Upload ---
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file (image/document) for sending via chat."""
    try:
        ext = os.path.splitext(file.filename)[1] if file.filename else ".bin"
        unique_name = f"{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(UPLOAD_DIR, unique_name)
        
        contents = await file.read()
        with open(file_path, "wb") as f:
            f.write(contents)
        
        abs_path = os.path.abspath(file_path)
        return {"status": "ok", "path": abs_path, "filename": unique_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/send")
async def send_message(req: SendMsgRequest):
    """Send message via OneBot."""
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    return await crawler.send_msg(req.target_id, req.type, req.content, req.is_group)


# --- Analysis & Crawling ---
@app.post("/api/dashboard/update")
async def update_dashboard(req: CrawlRequest):
    if not onebot_server.is_bot_online:
        raise HTTPException(status_code=503, detail="Bot is not online")
    
    settings = req.settings or AnalysisSettings()
    
    profile = {}
    qzone_feeds = []
    
    # Skip profile and qzone for group chats (no individual target)
    if not req.is_group and req.target_id:
        try:
            # Use enhanced profile detail method
            profile = await crawler.get_profile_detail(req.target_id)
        except Exception as e:
            logger.error(f"Failed to get profile: {e}")
        
        try:
            cookies_data = await crawler.get_cookies("qzone.qq.com")
            cookies_str = cookies_data.get("cookies", "")
            qzone_feeds = await crawler.get_qzone_feeds(req.target_id, cookies_str, limit=settings.qzone_count)
        except Exception as e:
            logger.error(f"Failed to get qzone feeds: {e}")
    
    chat_history = []
    try:
        if req.is_group and req.group_id:
            # For groups, fetch all messages (don't filter by target_id)
            chat_history = await crawler.get_chat_history(str(req.group_id), count=settings.context_msg_count, is_group=True)
        elif req.target_id:
            # For private chats, fetch message history with target user
            chat_history = await crawler.get_chat_history(str(req.target_id), count=settings.context_msg_count, is_group=False)
    except Exception as e:
        logger.error(f"Failed to get chat history: {e}")
        
    analysis_result = {}
    suggested_topics = []
    try:
        analysis_result = await analyzer.analyze_personality(profile, qzone_feeds, chat_history)
        suggested_topics = await analyzer.suggest_topics(chat_history, analysis_result)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
    
    # Cache data for later retrieval/export
    if req.target_id:
        user_data_cache[req.target_id] = {
            "profile": profile,
            "chat_history": chat_history[-50:],  # Keep last 50 messages
            "analysis": analysis_result,
            "qzone_feeds": qzone_feeds,
            "topics": suggested_topics,
        }
    
    return {
        "profile": profile,
        "analysis": analysis_result,
        "topics": suggested_topics,
        "recent_chats": chat_history[-20:]
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
