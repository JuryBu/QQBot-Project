"""
TFileManager 单元测试
验证 T 文件管理器的核心功能：创建、加载、保存、追加、构建 contexts
"""
import asyncio
import json
import os
import sys
import tempfile

# 模拟依赖
class MockLogger:
    def info(self, msg): print(f"  [INFO] {msg}")
    def warning(self, msg): print(f"  [WARN] {msg}")
    def error(self, msg): print(f"  [ERROR] {msg}")
    def debug(self, msg): pass

# 需要 patch 的路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Patch astrbot 依赖
import types
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = MockLogger()
astrbot = types.ModuleType("astrbot")
astrbot.api = astrbot_api
sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = astrbot_api

# 也需要 aiosqlite
try:
    import aiosqlite
except ImportError:
    print("❌ aiosqlite 未安装，跳过 DB 相关测试")
    aiosqlite = None

from checkpoint import (
    TFileManager,
    estimate_tokens,
    estimate_context_msg_tokens,
    _create_empty_t_file,
    CHECKPOINTS_DIR,
    build_compress_prompt,
    serialize_messages_for_compress,
)


def test_estimate_tokens():
    """测试 token 估算"""
    print("🧪 test_estimate_tokens")
    # 纯中文
    assert estimate_tokens("你好世界") == int(4 / 1.5)  # ~2
    # 纯英文
    assert estimate_tokens("hello world") == int(11 / 4.0)  # ~2
    # 空字符串
    assert estimate_tokens("") == 0
    print("  ✅ 通过")


def test_create_empty_t_file():
    """测试空 T 文件创建"""
    print("🧪 test_create_empty_t_file")
    t = _create_empty_t_file("GroupMessage:<GROUP_B>")
    assert t["version"] == 1
    assert t["window_key"] == "GroupMessage:<GROUP_B>"
    assert t["window_type"] == "group"
    assert t["window_id"] == "<GROUP_B>"
    assert t["T1"]["compressed_summary"] == ""
    assert t["T1"]["original_msg_count"] == 0
    assert t["messages"] == []
    assert t["metadata"]["total_messages_ever"] == 0

    t2 = _create_empty_t_file("FriendMessage:1234567")
    assert t2["window_type"] == "private"
    assert t2["window_id"] == "1234567"
    print("  ✅ 通过")


def test_estimate_context_msg_tokens():
    """测试 OpenAI 格式消息 token 估算"""
    print("🧪 test_estimate_context_msg_tokens")
    # 普通文本消息
    msg = {"role": "user", "content": "你好世界，这是一条测试消息"}
    tokens = estimate_context_msg_tokens(msg)
    assert tokens > 0

    # 多模态消息
    msg2 = {
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "..."}},
        ],
    }
    tokens2 = estimate_context_msg_tokens(msg2)
    assert tokens2 > 258  # 至少有图片的 token

    # tool_calls 消息
    msg3 = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"function": {"name": "web_search", "arguments": '{"q": "天气"}'}}
        ],
    }
    tokens3 = estimate_context_msg_tokens(msg3)
    assert tokens3 > 4
    print("  ✅ 通过")


async def test_load_save():
    """测试 T 文件加载和保存"""
    print("🧪 test_load_save")
    # 使用临时目录
    import checkpoint
    old_dir = checkpoint.CHECKPOINTS_DIR
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint.CHECKPOINTS_DIR = tmpdir
        mgr = TFileManager()
        mgr._file_path = lambda wk: os.path.join(tmpdir, wk.replace(":", "_") + ".json")

        # 1. 加载不存在的文件 → 应创建空 T
        t = await mgr.load("GroupMessage:12345")
        assert t["version"] == 1
        assert t["window_key"] == "GroupMessage:12345"
        assert t["messages"] == []

        # 确认文件已创建
        fp = mgr._file_path("GroupMessage:12345")
        assert os.path.exists(fp)

        # 2. 修改并保存
        t["messages"].append({"role": "user", "content": "Hello", "timestamp": "2026-01-01T00:00:00"})
        await mgr.save("GroupMessage:12345", t)

        # 3. 重新加载验证
        t2 = await mgr.load("GroupMessage:12345")
        assert len(t2["messages"]) == 1
        assert t2["messages"][0]["content"] == "Hello"

        # 4. 测试损坏文件恢复
        with open(fp, "w") as f:
            f.write("{invalid json!!")
        t3 = await mgr.load("GroupMessage:12345")
        assert t3["version"] == 1
        assert t3["messages"] == []  # 回退到空 T

    checkpoint.CHECKPOINTS_DIR = old_dir
    print("  ✅ 通过")


async def test_append_messages():
    """测试消息追加"""
    print("🧪 test_append_messages")
    import checkpoint
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint.CHECKPOINTS_DIR = tmpdir
        mgr = TFileManager()
        mgr._file_path = lambda wk: os.path.join(tmpdir, wk.replace(":", "_") + ".json")

        # 创建初始 T 文件
        await mgr.load("GroupMessage:test1")

        # 追加消息
        new_msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀~"},
        ]
        t = await mgr.append_messages("GroupMessage:test1", new_msgs)

        assert len(t["messages"]) == 2
        assert t["messages"][0]["role"] == "user"
        assert t["messages"][1]["content"] == "你好呀~"
        assert t["metadata"]["total_messages_ever"] == 2

        # 再追加
        t2 = await mgr.append_messages("GroupMessage:test1", [
            {"role": "user", "content": "第三条"},
        ])
        assert len(t2["messages"]) == 3
        assert t2["metadata"]["total_messages_ever"] == 3

    print("  ✅ 通过")


async def test_build_llm_contexts():
    """测试构建 LLM contexts"""
    print("🧪 test_build_llm_contexts")
    mgr = TFileManager.__new__(TFileManager)

    # 1. 无 T1，有 messages
    t_file = {
        "T1": {"compressed_summary": ""},
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ],
    }
    contexts = mgr.build_llm_contexts(t_file)
    assert len(contexts) == 2
    assert contexts[0]["role"] == "user"
    assert contexts[1]["content"] == "Hi!"

    # 2. 有 T1
    t_file["T1"]["compressed_summary"] = "之前讨论了天气"
    contexts2 = mgr.build_llm_contexts(t_file)
    assert len(contexts2) == 4  # T1_user + T1_ack + 2 messages
    assert "[对话历史压缩摘要]" in contexts2[0]["content"]
    assert contexts2[1]["content"] == "好的，我已了解之前的对话历史。"

    # 3. 工具调用消息
    t_file["messages"].append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
    })
    t_file["messages"].append({
        "role": "tool",
        "tool_call_id": "c1",
        "content": "result",
    })
    contexts3 = mgr.build_llm_contexts(t_file)
    assert contexts3[-2].get("tool_calls") is not None
    assert contexts3[-1]["tool_call_id"] == "c1"

    print("  ✅ 通过")


async def test_build_flashlite_context():
    """测试 FlashLite 上下文构建"""
    print("🧪 test_build_flashlite_context")
    mgr = TFileManager.__new__(TFileManager)

    t_file = {
        "T1": {"compressed_summary": "历史摘要内容"},
        "messages": [
            {"role": "user", "content": "消息1", "timestamp": "2026-01-01T10:00:00",
             "meta": {"sender_name": "张三", "sender_qq": "12345"}},
            {"role": "assistant", "content": "回复1", "timestamp": "2026-01-01T10:00:05"},
        ],
    }

    text = mgr.build_flashlite_context(t_file, max_tokens=8000)
    assert "历史摘要内容" in text
    assert "张三(12345)" in text
    assert "老板娘 [BOT]" in text

    # 测试截断
    short_text = mgr.build_flashlite_context(t_file, max_tokens=10)
    # max_tokens 很小时应该只有部分内容
    assert len(short_text) < len(text)

    print("  ✅ 通过")


def test_serialize_messages():
    """测试消息序列化"""
    print("🧪 test_serialize_messages")
    messages = [
        {"role": "user", "content": "你好", "timestamp": "2026-01-01T10:00:00",
         "meta": {"sender_name": "Alice", "sender_qq": "111"}},
        {"role": "assistant", "content": "你好~", "timestamp": "2026-01-01T10:00:05"},
        {"role": "assistant", "content": None, "timestamp": "2026-01-01T10:01:00",
         "tool_calls": [{"function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "搜索结果", "timestamp": "2026-01-01T10:01:01"},
    ]
    text = serialize_messages_for_compress(messages)
    assert "Alice(111)" in text
    assert "老板娘 [BOT]" in text
    assert "[工具调用: search]" in text
    assert "[工具结果 c1]" in text
    print("  ✅ 通过")


def test_build_compress_prompt():
    """测试压缩 prompt 构建"""
    print("🧪 test_build_compress_prompt")

    prompt = build_compress_prompt(
        messages_text="测试对话内容" * 100,
        original_tokens=5000,
        target_min_ratio=0.2,
        target_max_ratio=0.4,
        has_previous_summary=False,
    )
    assert "1500" in prompt  # min_chars: 5000 * 0.2 * 1.5 = 1500
    assert "3000" in prompt  # max_chars: 5000 * 0.4 * 1.5 = 3000
    assert "5000" in prompt  # original tokens

    # 有旧摘要
    prompt2 = build_compress_prompt(
        messages_text="test",
        original_tokens=1000,
        target_min_ratio=0.2,
        target_max_ratio=0.4,
        has_previous_summary=True,
    )
    assert "对话历史压缩摘要" in prompt2
    assert "融合" in prompt2

    print("  ✅ 通过")


async def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("TFileManager 单元测试")
    print("=" * 60)

    test_estimate_tokens()
    test_create_empty_t_file()
    test_estimate_context_msg_tokens()
    await test_load_save()
    await test_append_messages()
    await test_build_llm_contexts()
    await test_build_flashlite_context()
    test_serialize_messages()
    test_build_compress_prompt()

    print("=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_all_tests())
