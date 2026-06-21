import asyncio
import json
import logging
import uvicorn
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from typing import Callable, Coroutine, Dict, Any, Optional, List

logger = logging.getLogger("OneBotServer")

class OneBotServer:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(OneBotServer, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
            
        self._initialized = True
        self.app = FastAPI()
        self._connected = False
        self._bot_id: Optional[str] = None
        self._bot_info: Dict[str, Any] = {}
        self.logs = []  # Internal log buffer for UI
        
        # Event handlers
        self._handlers = {
            "meta_event": [],
            "message": [],
            "notice": [],
            "request": []
        }
        
        # WebSocket connection (from NapCat)
        self._ws: Optional[WebSocket] = None
        
        # Frontend WebSocket clients for real-time push
        self._frontend_clients: List[WebSocket] = []
        
        # API Response Futures: echo -> Future
        self._pending_requests: Dict[str, asyncio.Future] = {}
        
        # Setup Routes
        self.app.websocket("/{path:path}")(self._websocket_endpoint)
        
        # Register default message handler for forwarding
        self._handlers["message"].append(self._forward_message_to_frontend)

    async def _forward_message_to_frontend(self, payload: Dict[str, Any]):
        """Forward incoming messages to all connected frontend clients."""
        if not self._frontend_clients:
            return
        message_data = json.dumps({
            "type": "new_message",
            "data": payload
        })
        for client in self._frontend_clients[:]:  # Copy list to avoid mutation issues
            try:
                await client.send_text(message_data)
            except Exception:
                # Client disconnected
                if client in self._frontend_clients:
                    self._frontend_clients.remove(client)

    async def add_frontend_client(self, ws: WebSocket):
        """Add a frontend WebSocket client."""
        await ws.accept()
        self._frontend_clients.append(ws)
        self.log(f"Frontend client connected. Total: {len(self._frontend_clients)}")

    async def remove_frontend_client(self, ws: WebSocket):
        """Remove a frontend WebSocket client."""
        if ws in self._frontend_clients:
            self._frontend_clients.remove(ws)
            self.log(f"Frontend client disconnected. Total: {len(self._frontend_clients)}")

    def log(self, message: str):
        """Log to internal buffer and console."""
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        log_msg = f"[OneBot] {timestamp} {message}"
        print(log_msg, flush=True)
        self.logs.append(log_msg)
        if len(self.logs) > 50:
            self.logs.pop(0)

    async def start(self, host="0.0.0.0", port=6299):
        """Start the Uvicorn server."""
        try:
            self.log(f"Starting server on {host}:{port}")
            config = uvicorn.Config(self.app, host=host, port=port, log_level="warning")
            self.server = uvicorn.Server(config)
            await self.server.serve()
        except Exception as e:
            self.log(f"CRITICAL: Failed to start server: {e}")
            logger.error(f"Failed to start OneBotServer: {e}")

    async def stop(self):
        """Stop the Uvicorn server."""
        if hasattr(self, 'server') and self.server:
            self.server.should_exit = True
            logger.info("Stopping OneBotServer...")
            # Allow some time for cleanup if needed, but should_exit usually triggers shutdown loop
            await asyncio.sleep(0.5)

    async def _websocket_endpoint(self, websocket: WebSocket, path: str):
        """Handle incoming WebSocket connections."""
        self.log(f"New connection from {websocket.client} at {path}")
        await websocket.accept()
        self._ws = websocket
        self._connected = True
        self.log("Connection accepted. Fetching info...")
        # Proactively fetch info
        asyncio.create_task(self._fetch_bot_info())
        
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    payload = json.loads(data)
                    await self._handle_payload(payload)
                except json.JSONDecodeError:
                    self.log("Received invalid JSON")
        except Exception as e:
            self.log(f"WS Error: {e}")
        finally:
            self._connected = False
            self._ws = None
            self.log("Disconnected.")

    async def _handle_payload(self, payload: Dict[str, Any]):
        """Dispatch OneBot events."""
        # Log reduced payload
        post_type = payload.get("post_type")
        meta_type = payload.get("meta_event_type")
        
        if meta_type == "heartbeat":
             if "self_id" in payload:
                 # Too noisy to log every heartbeat?
                 pass
             else:
                 self.log("Heartbeat missing self_id!")
        else:
             self.log(f"RX: {str(payload)[:100]}...")

        # 1. Handle API Responses (echo)
        if "echo" in payload:
            echo = payload["echo"]
            if echo in self._pending_requests:
                if not self._pending_requests[echo].done():
                    self._pending_requests[echo].set_result(payload)
                del self._pending_requests[echo]
            return

        # 2. Handle Events (post_type)
        post_type = payload.get("post_type")
        
        # Special handling for lifecycle/heartbeat to update state
        if post_type == "meta_event":
            meta_type = payload.get("meta_event_type")
            if meta_type == "heartbeat":
                # Update self_id and status
                if "self_id" in payload:
                    self._bot_id = str(payload["self_id"])
                    self._connected = True
                    # Auto-fetch info if missing
                    if not self._bot_info:
                        asyncio.create_task(self._fetch_bot_info())
            elif meta_type == "lifecycle":
                sub_type = payload.get("sub_type")
                if sub_type == "connect":
                    self._connected = True
                    asyncio.create_task(self._fetch_bot_info())

        # Dispatch to registered handlers
        if post_type in self._handlers:
            for handler in self._handlers[post_type]:
                try:
                    await handler(payload)
                except Exception as e:
                    logger.error(f"Error in handler for {post_type}: {e}")

    async def call_api(self, action: str, params: dict = None, timeout: float = 10.0) -> dict:
        """Call OneBot API."""
        if not self._ws or not self._connected:
            logger.warning("Cannot call API: WebSocket not connected.")
            return {}

        import uuid
        echo = str(uuid.uuid4())
        payload = {
            "action": action,
            "params": params or {},
            "echo": echo
        }
        
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[echo] = future
        
        try:
            await self._ws.send_text(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout)
            return response # Return full response
        except asyncio.TimeoutError:
            self.log(f"API call {action} timed out.")
            if echo in self._pending_requests:
                del self._pending_requests[echo]
            return {}
        except Exception as e:
            self.log(f"API call failed: {e}")
            return {}

    async def _fetch_bot_info(self):
        """Fetch bot info after connection."""
        if not self._connected:
            return
        try:
            response = await self.call_api("get_login_info")
            info = response.get("data", {})
            if info:
                self._bot_info = info
                self._bot_id = str(info.get("user_id", ""))
                self.log(f"Updated Bot Info: {self._bot_info}")
        except Exception as e:
            self.log(f"Failed to fetch bot info: {e}")

    # --- compatibility methods for decorators ---
    def on_meta_event(self, event_type: str = None):
        def decorator(func):
            self._handlers["meta_event"].append(func)
            return func
        return decorator

    def on_message(self, func):
        self._handlers["message"].append(func)
        return func
        
    def on_notice(self, func):
        self._handlers["notice"].append(func)
        return func

    @property
    def is_connected(self):
        return self._connected

    @property
    def is_bot_online(self):
        return self._connected and self._bot_id is not None

    @property
    def login_info(self):
        return self._bot_info

    def get_status(self):
        """Get current status of the OneBot Server."""
        return {
            "ws_connected": self._connected,
            "bot_online": self.is_bot_online,
            "login_info": self._bot_info
        }

# Global Instance
onebot_server = OneBotServer()
