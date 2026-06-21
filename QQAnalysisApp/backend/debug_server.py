"""
Diagnostic Script - File Logging Version
"""
import logging
import sys
import threading
import time
import os

# Ensure backend dir is in sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Configure logging to file
logging.basicConfig(
    filename='debug.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filemode='w'
)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
logging.getLogger('').addHandler(console)

logger = logging.getLogger("DebugServer")

from onebot_server import onebot_server
from napcat_manager import napcat_manager

def start_napcat_thread():
    logger.info("Starting NapCat thread...")
    res = napcat_manager.start()
    logger.info(f"NapCat Start Result: {res}")
    
    # Keep reading output
    while True:
        if napcat_manager.output_queue:
            line = napcat_manager.output_queue.pop(0)
            logger.info(f"[NapCat] {line.strip()}")
        
        # Periodic Status Check
        if int(time.time()) % 2 == 0:
            logger.info(f"OneBot Connected: {onebot_server.is_connected}, Bot Online: {onebot_server.is_bot_online}, Bot Info: {onebot_server.login_info}")
        
        time.sleep(0.1)

if __name__ == "__main__":
    logger.info("--- Starting Diagnostic (File Mode) ---")
    
    # 1. Kill anything on 6199
    logger.info("Checking port 6199...")
    try:
        from napcat_manager import kill_process_on_port
        kill_process_on_port(6199)
        time.sleep(2) # Wait for cleanup
    except Exception as e:
        logger.warning(f"Failed to clean port 6199: {e}")

    # 2. Start NapCat
    t = threading.Thread(target=start_napcat_thread, daemon=True)
    t.start()
    
    # 3. Run Server
    logger.info("Starting OneBotServer on 6199...")
    logger.info(f"Registered Routes: {onebot_server.bot.url_map}")
    try:
        # Check if port is in use first?
        # Use run_task with asyncio loop if we want more control, but .run() is easiest
        # Note: .run() blocks.
        onebot_server.bot.run(host="0.0.0.0", port=6199)
    except Exception as e:
        logger.error(f"Server crashed: {e}", exc_info=True)
