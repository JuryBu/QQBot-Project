"""
QQ Crawler - Uses OneBotServer for API calls.
"""
import logging
from typing import Dict, List

from onebot_server import onebot_server

logger = logging.getLogger("QQCrawler")


class QQCrawler:
    """QQ Data Crawler using OneBotServer for OneBot API calls."""

    async def get_stranger_info(self, user_id: int) -> Dict:
        """Fetch profile info for a user."""
        try:
            response = await onebot_server.call_api(
                "get_stranger_info", 
                {"user_id": user_id, "no_cache": True}
            )
            if response.get("status") == "ok":
                return response.get("data", {})
        except Exception as e:
            logger.error(f"Error getting stranger info: {e}")
        return {}

    async def get_profile_detail(self, user_id: int) -> Dict:
        """获取用户详细资料，包括头像URL、签名等"""
        result = {
            "user_id": user_id,
            "nickname": "",
            "avatar_url": f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
            "avatar_url_small": f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100",
            "signature": "",
            "sex": "",
            "age": 0,
            "level": 0,
            "qzone_url": f"https://user.qzone.qq.com/{user_id}",
        }
        
        # 获取基本信息
        try:
            stranger_info = await self.get_stranger_info(user_id)
            if stranger_info:
                result["nickname"] = stranger_info.get("nickname", "")
                result["sex"] = stranger_info.get("sex", "")
                result["age"] = stranger_info.get("age", 0)
                result["level"] = stranger_info.get("level", 0)
                # 有些API返回签名
                if "signature" in stranger_info:
                    result["signature"] = stranger_info["signature"]
                if "qid" in stranger_info:
                    result["qid"] = stranger_info["qid"]
        except Exception as e:
            logger.error(f"Error getting profile detail: {e}")
        
        return result

    async def get_cookies(self, domain: str = "qzone.qq.com") -> Dict:
        """Fetch cookies for a specific domain via OneBot."""
        try:
            response = await onebot_server.call_api("get_cookies", {"domain": domain})
            if response.get("status") == "ok":
                return response.get("data", {})
        except Exception as e:
            logger.error(f"Error getting cookies: {e}")
        return {}

    async def get_qzone_feeds(self, user_id: int, cookies: str, limit: int = 10) -> List[Dict]:
        """Fetch Qzone feeds (placeholder implementation)."""
        return [{"content": "Qzone scraping requires complex reverse engineering. Mocking data for now.", "time": 0}]

    async def get_chat_history(self, session_id: str, count: int = 50, is_group: bool = True) -> List[Dict]:
        """Fetch chat history for group or private chat."""
        try:
            if is_group:
                # Group history
                payload = {"group_id": int(session_id), "count": count}
                response = await onebot_server.call_api("get_group_msg_history", payload)
            else:
                # Private chat history
                payload = {"user_id": int(session_id), "count": count}
                response = await onebot_server.call_api("get_friend_msg_history", payload)
            
            if response.get("status") == "ok":
                return response.get("data", {}).get("messages", [])
        except Exception as e:
            logger.error(f"Error getting chat history: {e}")
        return []

    async def get_login_info(self) -> Dict:
        """Fetch logged-in user info."""
        # First check if we have cached info
        cached = onebot_server.login_info
        if cached:
            return cached
        
        # Otherwise try to fetch
        try:
            response = await onebot_server.call_api("get_login_info", {})
            if response.get("status") == "ok":
                return response.get("data", {})
        except Exception as e:
            logger.error(f"Error getting login info: {e}")
        return {}

    async def get_contacts(self) -> Dict:
        """Fetch friends and groups."""
        friends = []
        groups = []
        
        try:
            # Friends
            response = await onebot_server.call_api("get_friend_list", {})
            # print(f"DEBUG: Friends Resp: {str(response)[:100]}")
            if response.get("status") == "ok":
                friends = response.get("data", [])
            
            # Groups
            response = await onebot_server.call_api("get_group_list", {})
            # print(f"DEBUG: Groups Resp: {str(response)[:100]}")
            if response.get("status") == "ok":
                groups = response.get("data", [])
        except Exception as e:
            logger.error(f"Error getting contacts: {e}")
        
        return {"friends": friends, "groups": groups}

    async def send_msg(self, target_id: int, message_type: str, content: str, is_group: bool = False) -> Dict:
        """Send a message using OneBot API."""
        payload = {
            "message_type": "group" if is_group else "private",
            "message": content if message_type == "text" else f"[CQ:image,file={content}]",
            "auto_escape": False
        }
        
        if is_group:
            payload["group_id"] = target_id
        else:
            payload["user_id"] = target_id
        
        try:
            return await onebot_server.call_api("send_msg", payload)
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return {"status": "error", "message": str(e)}


crawler = QQCrawler()
