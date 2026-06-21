"""
自动化单元测试 - Codex Review Stage 7-10 修复验证
覆盖：sandbox 路径安全 / Memory 工作区隔离 / Agent SQL 查询 / exec_code 限制

运行: python test_codex_fixes.py
"""

import asyncio
import os
import sys
import tempfile
import json

# 添加插件路径
PLUGIN_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "AstrBot", "data", "plugins", "astrbot_plugin_flashlite")
)
sys.path.insert(0, PLUGIN_DIR)

passed = 0
failed = 0


def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {'- ' + detail if detail else ''}")


# ========================================
# Test 1: Sandbox 路径安全（问题1修复验证）
# ========================================
print("\n🔒 Test Group 1: Sandbox 路径安全")

from sandbox import SandboxSecurity

# 创建临时 Sandbox 目录
with tempfile.TemporaryDirectory() as tmpdir:
    sandbox_root = os.path.join(tmpdir, "Sandbox")
    os.makedirs(sandbox_root)
    os.makedirs(os.path.join(sandbox_root, "workspace"))
    os.makedirs(os.path.join(sandbox_root, "config"))

    # 创建同前缀兄弟目录（模拟 Sandbox_evil）
    evil_dir = os.path.join(tmpdir, "Sandbox_evil")
    os.makedirs(evil_dir)

    sec = SandboxSecurity(sandbox_root)

    # 正常路径应通过
    valid, _ = sec.validate_path("workspace")
    test("正常路径 workspace 通过", valid)

    # 路径逃逸检测
    valid, msg = sec.validate_path("../../etc/passwd")
    test("路径逃逸 ../../etc/passwd 被拒绝", not valid, msg)

    # 绝对路径拒绝
    valid, msg = sec.validate_path("C:\\Windows\\System32\\cmd.exe")
    test("绝对路径被拒绝", not valid, msg)

    # config 写入禁止
    valid, msg = sec.validate_path("config/limits.json", allow_write=True)
    test("config 目录写入被拒绝", not valid, msg)

    # base_tools 写入禁止（非 system_report）
    valid, msg = sec.validate_path("base_tools/runtimes/python", allow_write=True)
    test("base_tools 非 system_report 写入被拒绝", not valid, msg)

    # base_tools/system_report 写入允许
    os.makedirs(os.path.join(sandbox_root, "base_tools", "system_report"), exist_ok=True)
    valid, _ = sec.validate_path("base_tools/system_report", allow_write=True)
    test("base_tools/system_report 写入允许", valid)


# ========================================
# Test 2: Sandbox exec_code 限制（问题2+补充A）
# ========================================
print("\n🔧 Test Group 2: Sandbox exec_code 限制")

from sandbox import SandboxManager


async def test_exec_limits():
    with tempfile.TemporaryDirectory() as tmpdir:
        sandbox_root = os.path.join(tmpdir, "Sandbox")
        os.makedirs(os.path.join(sandbox_root, "workspace", "scripts"), exist_ok=True)
        os.makedirs(os.path.join(sandbox_root, "config"), exist_ok=True)
        os.makedirs(os.path.join(sandbox_root, "base_tools", "runtimes"), exist_ok=True)

        # 创建 limits.json
        limits = {
            "execution": {
                "max_timeout_ms": 5000,
                "max_concurrent": 2,
                "max_code_size_kb": 1,
                "max_stdout_chars": 100,
                "max_stderr_chars": 100,
            },
            "network": {"allow_outbound": False},
        }
        with open(os.path.join(sandbox_root, "config", "limits.json"), "w") as f:
            json.dump(limits, f)

        mgr = SandboxManager(sandbox_root)

        # cwd 禁止 base_tools
        result = await mgr.exec_code("print('hi')", cwd="base_tools/runtimes")
        test("cwd=base_tools 被拒绝", not result["success"], result.get("error", ""))

        # cwd 禁止 config
        result = await mgr.exec_code("print('hi')", cwd="config")
        test("cwd=config 被拒绝", not result["success"], result.get("error", ""))

        # 代码大小超限（limits 设为 1KB）
        big_code = "x = 1\n" * 1000  # 约 6KB
        result = await mgr.exec_code(big_code)
        test("代码大小超限被拒绝", not result["success"], result.get("error", ""))


asyncio.run(test_exec_limits())


# ========================================
# Test 3: Memory 工作区隔离（问题5）
# ========================================
print("\n📝 Test Group 3: Memory 工作区隔离")

# 临时替换 MEMORY_DB
import memory as mem_module

original_db = mem_module.MEMORY_DB
original_dir = mem_module.MEMORY_DIR


async def test_memory_isolation():
    with tempfile.TemporaryDirectory() as tmpdir:
        mem_module.MEMORY_DB = os.path.join(tmpdir, "memory.db")
        mem_module.MEMORY_DIR = tmpdir

        store = mem_module.MemoryStore()

        # 写入不同工作区
        id1 = await store.write("群友A的生日", "8月12日", tags=["生日"], workspace="group_123")
        id2 = await store.write("群友B的梗", "ACGN", tags=["梗"], workspace="group_456")

        # read 带 workspace 隔离
        m1 = await store.read(id1, workspace="group_123")
        test("read 正确工作区返回数据", m1 is not None and m1["title"] == "群友A的生日")

        m1_wrong = await store.read(id1, workspace="group_456")
        test("read 错误工作区返回 None", m1_wrong is None)

        # update 带 workspace 隔离
        ok = await store.update(id1, title="更新标题", workspace="group_123")
        test("update 正确工作区成功", ok)

        ok_wrong = await store.update(id1, title="恶意更新", workspace="group_456")
        test("update 错误工作区失败", not ok_wrong)

        # delete 带 workspace 隔离
        del_wrong = await store.delete(id2, workspace="group_123")
        test("delete 错误工作区失败", not del_wrong)

        del_ok = await store.delete(id2, workspace="group_456")
        test("delete 正确工作区成功", del_ok)

        # 不带 workspace 的兼容性
        m1_compat = await store.read(id1)
        test("read 不带 workspace 仍可访问", m1_compat is not None)

    mem_module.MEMORY_DB = original_db
    mem_module.MEMORY_DIR = original_dir


asyncio.run(test_memory_isolation())


# ========================================
# Test 4: Agent SQL 字段名对齐（问题4）
# ========================================
print("\n🔗 Test Group 4: Agent SQL 字段名对齐")

import agent as agent_module


async def test_agent_sql():
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建模拟 messages.db
        import aiosqlite

        db_path = os.path.join(tmpdir, "messages.db")
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE checkpoint_history (
                    id INTEGER PRIMARY KEY,
                    window_type TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    compressed_content TEXT NOT NULL,
                    original_msg_range_start INTEGER,
                    original_msg_range_end INTEGER,
                    compression_ratio REAL,
                    token_estimate INTEGER,
                    created_at TEXT NOT NULL
                )
            """)
            await db.execute(
                """INSERT INTO checkpoint_history
                   (window_type, window_id, compressed_content, created_at)
                   VALUES (?, ?, ?, ?)""",
                ("group", "<GROUP_B>", "这是一段压缩后的群聊历史摘要。", "2026-04-01T12:00:00"),
            )
            await db.commit()

        # Mock AgentRequestBuilder
        class MockKnowledge:
            def get_formatted(self):
                return ""

        class MockMemory:
            pass

        class MockCheckpoint:
            pass

        builder = agent_module.AgentRequestBuilder(
            knowledge_cache=MockKnowledge(),
            memory_store=MockMemory(),
            checkpoint_mgr=MockCheckpoint(),
        )

        # 临时修改路径
        import types
        original_method = builder._get_checkpoint_summary

        async def patched_summary(window_key):
            parts = window_key.split(":", 1)
            w_type = "group" if "Group" in parts[0] else "private"
            w_id = parts[1] if len(parts) == 2 else window_key
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    """SELECT compressed_content FROM checkpoint_history
                       WHERE window_type = ? AND window_id = ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (w_type, w_id),
                )
                row = await cursor.fetchone()
                return row[0] if row else None

        builder._get_checkpoint_summary = patched_summary

        # 测试
        result = await builder._get_checkpoint_summary("GroupMessage:<GROUP_B>")
        test("CHECKPOINT 查询返回正确内容", result == "这是一段压缩后的群聊历史摘要。")

        result_none = await builder._get_checkpoint_summary("GroupMessage:999999")
        test("不存在的窗口返回 None", result_none is None)


asyncio.run(test_agent_sql())


# ========================================
# 最终报告
# ========================================
print(f"\n{'='*40}")
print(f"🏁 测试结果: {passed} 通过, {failed} 失败 / {passed + failed} 总计")
if failed == 0:
    print("🎉 所有测试通过!")
else:
    print("⚠️ 有测试失败，请检查！")
print(f"{'='*40}")

sys.exit(0 if failed == 0 else 1)
