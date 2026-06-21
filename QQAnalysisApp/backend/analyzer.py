from openai import AsyncOpenAI
from typing import List, Dict, Optional
import json
import logging

logger = logging.getLogger("Analyzer")

class Analyzer:
    def __init__(self):
        self.client: Optional[AsyncOpenAI] = None
        self.model: str = "gpt-3.5-turbo"

    def configure(self, base_url: str, api_key: str, model: str):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    async def list_models(self, base_url: str, api_key: str) -> List[str]:
        """Fetch available models from the provider."""
        try:
            temp_client = AsyncOpenAI(base_url=base_url, api_key=api_key)
            models = await temp_client.models.list()
            return [m.id for m in models.data]
        except Exception as e:
            print(f"Error fetching models: {e}")
            return []

    async def analyze_personality(self, profile: Dict, qzone_feeds: List[Dict], chat_history: List[Dict]) -> Dict:
        if not self.client:
            return {"error": "LLM not configured"}

        # Construct prompt
        prompt = f"""
请根据以下信息分析该QQ用户的性格、兴趣爱好、和最近情绪变化。

[基本资料]
昵称: {profile.get('nickname')}
性别: {profile.get('sex')}
年龄: {profile.get('age')}

[近期动态 (Qzone)]
{json.dumps(qzone_feeds, indent=2, ensure_ascii=False)}

[最近聊天记录]
{json.dumps([msg.get('message') or msg.get('raw_message') for msg in chat_history[-20:]], indent=2, ensure_ascii=False)}

请以JSON格式输出分析结果，包含 key: "personality" (性格), "interests" (兴趣), "emotion" (情绪), "summary" (总结).
"""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            return {"error": str(e)}

    async def analyze_with_images(self, profile: Dict, image_urls: List[str], chat_history: List[Dict]) -> Dict:
        """使用图片进行多模态分析（如果模型支持 vision 能力）"""
        if not self.client:
            return {"error": "LLM not configured"}
        
        # 构建多模态消息内容
        content = [
            {
                "type": "text",
                "text": f"""请根据以下信息和图片分析该QQ用户的性格、兴趣爱好、和最近情绪变化。

[基本资料]
昵称: {profile.get('nickname', '未知')}
性别: {profile.get('sex', '未知')}
年龄: {profile.get('age', '未知')}

[最近聊天记录摘要]
{json.dumps([msg.get('raw_message', '') for msg in chat_history[-10:]], ensure_ascii=False)}

请结合图片内容（如头像、发送的图片等）进行综合分析。
以JSON格式输出，包含: "personality", "interests", "emotion", "image_analysis", "summary"。"""
            }
        ]
        
        # 添加图片URL（最多分析5张）
        for url in image_urls[:5]:
            if url:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": url}
                })
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                response_format={"type": "json_object"}
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            # 如果模型不支持vision，回退到纯文本分析
            logger.warning(f"Multimodal analysis failed, falling back to text: {e}")
            return await self.analyze_personality(profile, [], chat_history)

    async def suggest_topics(self, chat_history: List[Dict], analysis: Dict) -> List[str]:
        if not self.client:
            return []

        prompt = f"""
基于以下用户画像和聊天背景，推荐3个适合当前聊天的切入话题。

[用户画像]
{json.dumps(analysis, ensure_ascii=False)}

[最近聊天上下文]
{json.dumps([msg.get('message') for msg in chat_history[-5:]], ensure_ascii=False)}

请直接返回一个JSON数组，例如: ["话题1", "话题2", "话题3"]
"""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            result = json.loads(response.choices[0].message.content)
            # Handle potential different keys if LLM doesn't return list directly
            if isinstance(result, list):
                return result
            return list(result.values())[0] if result else []
        except Exception as e:
            return [f"Error generating topics: {e}"]

analyzer = Analyzer()
