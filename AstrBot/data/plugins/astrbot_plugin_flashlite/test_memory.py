"""Memory 引擎测试 — 验证 FTS5 + CJK 混合搜索 + CRUD + 概览"""
import asyncio
import os
import sys
import json

# 临时数据库路径
TEST_DB = os.path.join(os.path.dirname(__file__), "_test_memory.db")

# 修改 MEMORY_DB 指向测试数据库
import memory as mem_mod
mem_mod.MEMORY_DB = TEST_DB

from memory import MemoryStore, MemorySearchEngine, _tokenize, _has_cjk

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        print(f"  ❌ {name}: {detail}")
        failed += 1

async def test_all():
    # 清理旧测试库
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    print("=" * 50)
    print("1. CJK 检测与分词")
    print("=" * 50)
    check("CJK检测-中文", _has_cjk("你好世界"))
    check("CJK检测-日文", _has_cjk("こんにちは"))
    check("CJK检测-英文", not _has_cjk("hello world"))
    
    tokens_cn = _tokenize("你好世界测试")
    check("CJK 2-gram分词", len(tokens_cn) >= 3, f"tokens={tokens_cn}")
    
    tokens_en = _tokenize("hello world test")
    check("英文空格分词", "hello" in tokens_en and "world" in tokens_en)
    
    tokens_mix = _tokenize("你好 hello 世界")
    check("混合分词有CJK", any(len(t) == 2 and ord(t[0]) > 0x4e00 for t in tokens_mix))

    print("\n" + "=" * 50)
    print("2. 搜索引擎单元测试")
    print("=" * 50)
    engine = MemorySearchEngine()
    entries = [
        {"id": "1", "title": "Python学习笔记", "search_summary": "asyncio协程基础", "tags": ["python", "async"]},
        {"id": "2", "title": "数据库设计", "search_summary": "SQLite FTS5全文索引", "tags": ["database", "sqlite"]},
        {"id": "3", "title": "前端开发", "search_summary": "React组件化", "tags": ["frontend", "react"]},
    ]
    
    r1 = engine.search(entries, "python", limit=5)
    check("搜索python命中", len(r1) >= 1 and r1[0]["id"] == "1")
    
    r2 = engine.search(entries, "SQLite 数据库", limit=5)
    check("多词搜索命中", len(r2) >= 1 and r2[0]["id"] == "2")
    
    r3 = engine.search(entries, "完全无关的内容xyz", limit=5)
    check("无关查询无结果", len(r3) == 0)
    
    dups = engine.check_duplicates(entries, "Python学习", "asyncio")
    check("去重检测命中", len(dups) >= 1)

    print("\n" + "=" * 50)
    print("3. MemoryStore CRUD")
    print("=" * 50)
    store = MemoryStore()
    
    # 写入
    w1 = await store.write("测试记忆1", "这是第一条测试内容", tags=["test", "unit"], workspace="test_ws")
    check("写入成功", "id" in w1, str(w1))
    
    w2 = await store.write("Python异步编程", "asyncio await 协程", tags=["python"], workspace="test_ws")
    check("写入2成功", "id" in w2)
    
    w3 = await store.write("全局笔记", "跨工作区的内容", tags=["global"], workspace="general")
    check("写入general成功", "id" in w3)
    
    # 去重检测
    w4 = await store.write("测试记忆1", "类似内容", workspace="test_ws")
    check("去重检测触发", len(w4.get("duplicates", [])) >= 1)
    
    # 读取
    rd = await store.read(w1["id"])
    check("读取成功", rd is not None and rd["title"] == "测试记忆1")
    check("读取有content", "content" in rd and "测试内容" in rd["content"])
    
    # 查询-概览模式（无参）
    overview = await store.query(workspace="test_ws")
    check("概览模式", overview.get("mode") == "overview")
    check("概览有total", overview.get("total", 0) >= 2)
    
    # 查询-混合搜索
    q1 = await store.query(query="python", workspace="test_ws")
    check("混合搜索命中", len(q1.get("results", [])) >= 1)
    
    # 查询-grep全文检索
    q2 = await store.query(grep="asyncio", workspace="test_ws")
    check("grep FTS5命中", len(q2.get("results", [])) >= 1, str(q2))
    
    # 查询-三级depth
    q_idx = await store.query(query="测试", workspace="test_ws", depth="index")
    if q_idx.get("results"):
        check("depth=index无content", "content" not in q_idx["results"][0])
    
    q_full = await store.query(query="测试", workspace="test_ws", depth="full")
    if q_full.get("results"):
        check("depth=full有content", "content" in q_full["results"][0])
    
    # 查询-scope=global
    q_global = await store.query(query="全局", scope="global")
    check("global搜索命中", len(q_global.get("results", [])) >= 1)
    
    # 更新
    ok = await store.update(w1["id"], title="已更新标题", append="追加内容")
    check("更新成功", ok)
    rd2 = await store.read(w1["id"])
    check("更新后标题变了", rd2 and rd2["title"] == "已更新标题")
    check("追加内容存在", rd2 and "追加内容" in rd2.get("content", ""))
    
    # pinned
    await store.update(w1["id"], pinned=True)
    overview2 = await store.query(workspace="test_ws")
    check("pinned出现在概览", len(overview2.get("pinned", [])) >= 1)
    
    # 删除
    ok2 = await store.delete(w4["id"])
    check("删除成功", ok2)
    rd3 = await store.read(w4["id"])
    check("删除后读不到", rd3 is None)

    # 清理
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.exists(TEST_DB + "-wal"):
        os.remove(TEST_DB + "-wal")
    if os.path.exists(TEST_DB + "-shm"):
        os.remove(TEST_DB + "-shm")

    print(f"\n{'=' * 50}")
    print(f"Memory 测试结果: {passed} 通过, {failed} 失败")
    print(f"{'=' * 50}")

if __name__ == "__main__":
    asyncio.run(test_all())
