"""
NapCat Process Manager - Handles starting/stopping NapCat.
Port conflict detection is less critical now as aiocqhttp/Quart handles port binding robustly.
"""
import subprocess
import os
import threading
import json
import glob
from pathlib import Path
import logging

logger = logging.getLogger("NapCatManager")

# Path to NapCat launcher
NAPCAT_DIR = Path(r"c:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\NapCat.Shell.Windows.OneKey\NapCat.39038.Shell")
NAPCAT_BAT = NAPCAT_DIR / "napcat.bat"
NAPCAT_EXE = NAPCAT_DIR / "NapCatWinBootMain.exe"  # Direct exe for quick login
NAPCAT_CONFIG_DIR = NAPCAT_DIR / "versions" / "9.9.21-39038" / "resources" / "app" / "napcat" / "config"

# Required settings for our app to work properly
REQUIRED_ONEBOT_SETTINGS = {
    "enableLocalFile2Url": True,  # Convert local files to URLs for images
    "parseMultMsg": True,  # Parse forward messages
}

def auto_configure_napcat():
    """Automatically configure all NapCat account configs with required settings."""
    if not NAPCAT_CONFIG_DIR.exists():
        logger.warning(f"NapCat config directory not found: {NAPCAT_CONFIG_DIR}")
        return
    
    # Find all onebot11*.json files
    config_files = list(NAPCAT_CONFIG_DIR.glob("onebot11*.json"))
    
    for config_path in config_files:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            modified = False
            for key, value in REQUIRED_ONEBOT_SETTINGS.items():
                if config.get(key) != value:
                    config[key] = value
                    modified = True
                    logger.info(f"Set {key}={value} in {config_path.name}")
            
            if modified:
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                logger.info(f"Updated config: {config_path.name}")
        except Exception as e:
            logger.error(f"Failed to update {config_path.name}: {e}")


import socket
def is_port_in_use(port: int) -> bool:
    """Check if a port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def kill_process_on_port(port: int) -> bool:
    """Kill process using the specified port (Windows only)."""
    try:
        # Find PID using netstat
        result = subprocess.run(
            f'netstat -ano | findstr ":{port}"',
            shell=True,
            capture_output=True,
            text=True
        )
        
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            pids = set()
            for line in lines:
                parts = line.split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    if pid.isdigit() and pid != '0':
                        pids.add(pid)
            
            for pid in pids:
                if str(pid) == str(os.getpid()):
                    continue
                logger.info(f"Killing process {pid} on port {port}")
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            return True
    except Exception as e:
        logger.error(f"Error killing process on port {port}: {e}")
    return False

class NapCatManager:
    def __init__(self):
        self.process = None
        self.running = False
        self.output_queue = []

    def get_saved_accounts(self):
        """Get list of QQ accounts that can use quick login."""
        accounts = []
        try:
            # Check config directory for saved accounts
            if NAPCAT_CONFIG_DIR.exists():
                for config_file in NAPCAT_CONFIG_DIR.glob("onebot11_*.json"):
                    # Extract QQ number from filename like onebot11_123456.json
                    name = config_file.stem
                    if name.startswith("onebot11_"):
                        qq = name.replace("onebot11_", "")
                        if qq.isdigit():
                            accounts.append({"qq": qq, "nickname": ""})
            
            # Also check for any cached login info
            cache_dir = NAPCAT_DIR / "versions"
            if cache_dir.exists():
                for version_dir in cache_dir.iterdir():
                    if version_dir.is_dir():
                        # Look for login cache files
                        data_dir = version_dir / "resources" / "app" / "napcat" / "config"
                        if data_dir.exists():
                            for f in data_dir.glob("onebot11_*.json"):
                                qq = f.stem.replace("onebot11_", "")
                                if qq.isdigit() and qq not in [a["qq"] for a in accounts]:
                                    accounts.append({"qq": qq, "nickname": ""})
        except Exception as e:
            logger.error(f"Error getting saved accounts: {e}")
        
        return accounts

    def start(self, login_type: str = "qrcode", qq: str = None, password: str = None):
        """
        Start NapCat with specified login method.
        login_type: "qrcode" (default), "quick", or "password"
        qq: QQ number for quick/password login
        password: Password for password login (not recommended, may trigger captcha)
        """
        if self.running:
            return {"status": "already_running", "pid": self.process.pid}

        try:
            # Auto-configure all account configs with required settings
            auto_configure_napcat()
            
            # Clean port 6299 before starting to avoid zombie conflicts（改 6299 避免误杀主 bot 的 6199）
            kill_process_on_port(6299)
            
            # Build command based on login type
            use_shell = True
            
            if login_type == "quick" and qq:
                # Quick login: use exe directly with -q parameter
                # NapCatWinBootMain.exe -q QQ号
                cmd = [str(NAPCAT_EXE), "-q", qq]
                use_shell = False  # Don't use shell for direct exe
                logger.info(f"Starting NapCat with quick login for QQ: {qq}")
            elif login_type == "password" and qq and password:
                # Password login (note: may trigger captcha)
                # NapCat doesn't directly support password via CLI
                cmd = [str(NAPCAT_BAT)]
                logger.warning("Password login requires WebUI interaction")
            else:
                # Default: QR code login
                cmd = [str(NAPCAT_BAT)]
                logger.info("Starting NapCat with QR code login")
            
            logger.info(f"Running command: {cmd}")
            
            # Start NapCat process
            self.process = subprocess.Popen(
                cmd,
                cwd=str(NAPCAT_DIR),
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1
            )
            self.running = True
            
            # Start a thread to read output
            threading.Thread(target=self._read_output, daemon=True).start()
            
            return {
                "status": "started", 
                "pid": self.process.pid,
                "login_type": login_type,
                "qq": qq if login_type == "quick" else None
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _read_output(self):
        """Reads stdout from the process and keeps the last 100 lines."""
        if not self.process:
            return

        for line in self.process.stdout:
            self.output_queue.append(line)
            if len(self.output_queue) > 100:
                self.output_queue.pop(0)
            print(f"[NapCat] {line.strip()}")
            
        self.running = False

    def get_qrcode_path(self):
        """Finds the QR code image in NapCat cache directory."""
        try:
            versions_dir = NAPCAT_DIR / "versions"
            if not versions_dir.exists():
                return None
            
            version_folders = [f for f in versions_dir.iterdir() if f.is_dir()]
            if not version_folders:
                return None
            
            version_folders.sort(key=lambda x: x.name, reverse=True)
            latest_version = version_folders[0]
            
            qrcode_path = latest_version / "resources" / "app" / "napcat" / "cache" / "qrcode.png"
            
            if qrcode_path.exists():
                return str(qrcode_path)
        except Exception:
            return None
        return None

    def stop(self):
        if self.process:
            try:
                logger.info(f"Killing process {self.process.pid}")
                # Use taskkill to force kill process tree
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self.running = False
                self.process = None
                return {"status": "stopped"}
            except Exception as e:
                logger.error(f"Error killing process: {e}")
                self.running = False
                self.process = None
                return {"status": "error", "message": str(e)}
        return {"status": "not_running"}

    def get_status(self):
        return {
            "running": self.running,
            "pid": self.process.pid if self.process else None,
            "qrcode_available": bool(self.get_qrcode_path()),
            "logs": list(self.output_queue)
        }


napcat_manager = NapCatManager()
