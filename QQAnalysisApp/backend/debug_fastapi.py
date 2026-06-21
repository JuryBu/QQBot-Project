"""
Diagnostic Script - FastAPI Version
Tests if FastAPI/Uvicorn can handle the connection.
"""
import logging
import sys
import threading
import time
import os
import uvicorn
from fastapi import FastAPI, WebSocket, Request

# Ensure backend dir is in sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Configure logging to file
logging.basicConfig(
    filename='debug_fastapi.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filemode='w'
)
logger = logging.getLogger("DebugFastAPI")

from napcat_manager import napcat_manager

app = FastAPI()

@app.websocket("/{path:path}")
async def websocket_endpoint(websocket: WebSocket, path: str):
    print(f"DEBUG: Incoming WebSocket connection at {path}", flush=True)
    logger.info(f"Incoming WebSocket connection from {websocket.client} at {path}")
    try:
        await websocket.accept()
        print("DEBUG: WebSocket Accepted!", flush=True)
        logger.info("WebSocket Accepted!")
        await websocket.send_json({"op": "hello"}) 
        while True:
            data = await websocket.receive_text()
            # print(f"DEBUG: Received msg: {data[:50]}...", flush=True)
            logger.info(f"Received msg: {data[:100]}...")
    except Exception as e:
        print(f"DEBUG: WebSocket Error: {e}", flush=True)
        logger.error(f"WebSocket Error: {e}")

def start_napcat_thread():
    logger.info("Starting NapCat thread...")
    # Kill port first?
    try:
        from napcat_manager import kill_process_on_port
        kill_process_on_port(6199)
        time.sleep(2)
    except:
        pass
        
    res = napcat_manager.start()
    logger.info(f"NapCat Start Result: {res}")
    
    while True:
        if napcat_manager.output_queue:
            line = napcat_manager.output_queue.pop(0)
            logger.info(f"[NapCat] {line.strip()}")
        time.sleep(0.1)

if __name__ == "__main__":
    print("DEBUG: Starting FastAPI Diagnostic...", flush=True)
    logger.info("--- Starting FastAPI Diagnostic ---")
    
    t = threading.Thread(target=start_napcat_thread, daemon=True)
    t.start()
    
    print("DEBUG: Starting Uvicorn on 6199...", flush=True)
    logger.info("Starting Uvicorn on 6199...")
    uvicorn.run(app, host="0.0.0.0", port=6199)
