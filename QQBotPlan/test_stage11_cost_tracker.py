"""Stage 11 测试：CostTracker 单元测试"""
import asyncio
import json
import os
import sys
import tempfile
import shutil

# 添加插件目录
PLUGIN_DIR = r"c:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_flashlite"
sys.path.insert(0, PLUGIN_DIR)

from cost_tracker import CostTracker, PRICING


async def test_record_and_query():
    """记录 → 查询 → 数据一致"""
    tmpdir = tempfile.mkdtemp()
    try:
        ct = CostTracker(data_dir=tmpdir)
        
        await ct.record(
            model="gemini-3.1-flash-lite-preview",
            call_type="flashlite_judge",
            window_key="GroupMessage:123",
            prompt_tokens=5000,
            cached_tokens=4000,
            output_tokens=100,
        )
        
        summary = ct.get_summary("today")
        assert summary["total_calls"] == 1, f"FAIL: calls={summary['total_calls']}"
        assert summary["total_prompt_tokens"] == 5000
        assert summary["total_cached_tokens"] == 4000
        assert summary["total_output_tokens"] == 100
        assert summary["cache_hit_rate"] == 80.0  # 4000/5000
        assert summary["total_cost_usd"] > 0
        
        print(f"✅ T11.1a: record/query 一致 (cost=${summary['total_cost_usd']:.6f})")
    finally:
        shutil.rmtree(tmpdir)


async def test_cost_calculation():
    """成本计算正确性"""
    tmpdir = tempfile.mkdtemp()
    try:
        ct = CostTracker(data_dir=tmpdir)
        
        # FlashLite: input=$0.25/M, cached=$0.025/M, output=$1.50/M
        # 5000 prompt, 4000 cached → 1000 uncached
        # cost = 1000*0.25/1M + 4000*0.025/1M + 100*1.50/1M = 0.00025 + 0.0001 + 0.00015 = 0.0005
        await ct.record(
            model="gemini-3.1-flash-lite-preview",
            call_type="flashlite_judge",
            window_key="test",
            prompt_tokens=5000,
            cached_tokens=4000,
            output_tokens=100,
        )
        
        summary = ct.get_summary("today")
        expected = 0.0005
        actual = summary["total_cost_usd"]
        assert abs(actual - expected) < 0.0001, f"FAIL: expected={expected}, got={actual}"
        
        print(f"✅ T11.1b: 成本计算正确 (${actual:.6f} ≈ ${expected})")
    finally:
        shutil.rmtree(tmpdir)


async def test_by_model_grouping():
    """按模型聚合"""
    tmpdir = tempfile.mkdtemp()
    try:
        ct = CostTracker(data_dir=tmpdir)
        
        for _ in range(3):
            await ct.record("gemini-3.1-flash-lite-preview", "flashlite", "g1", 1000, 800, 50)
        for _ in range(2):
            await ct.record("gemini-3-flash-preview", "tool_model", "g1", 2000, 1500, 100)
        
        by_model = ct.get_by_model("today")
        assert len(by_model) == 2, f"FAIL: groups={len(by_model)}"
        
        fl = [m for m in by_model if "lite" in m["model"]][0]
        assert fl["calls"] == 3
        
        tool = [m for m in by_model if "lite" not in m["model"]][0]
        assert tool["calls"] == 2
        
        print("✅ T11.1c: 按模型聚合正确")
    finally:
        shutil.rmtree(tmpdir)


async def test_by_window_grouping():
    """按窗口聚合"""
    tmpdir = tempfile.mkdtemp()
    try:
        ct = CostTracker(data_dir=tmpdir)
        
        await ct.record("gemini-3.1-flash-lite-preview", "flashlite", "GroupMessage:111", 1000, 800, 50)
        await ct.record("gemini-3.1-flash-lite-preview", "flashlite", "GroupMessage:222", 1000, 800, 50)
        await ct.record("gemini-3-flash-preview", "tool_model", "GroupMessage:111", 2000, 1500, 100)
        
        by_window = ct.get_by_window("today")
        assert len(by_window) == 2, f"FAIL: windows={len(by_window)}"
        
        w111 = [w for w in by_window if "111" in w["window_key"]][0]
        assert w111["calls"] == 2
        assert w111["flashlite_calls"] == 1  # "flashlite" counts as flashlite
        assert w111["tool_calls"] == 1
        
        w222 = [w for w in by_window if "222" in w["window_key"]][0]
        assert w222["calls"] == 1
        assert w222["flashlite_calls"] == 1
        
        print("✅ T11.1d: 按窗口聚合正确")
    finally:
        shutil.rmtree(tmpdir)


async def test_cache_hit_rate():
    """缓存命中率计算"""
    tmpdir = tempfile.mkdtemp()
    try:
        ct = CostTracker(data_dir=tmpdir)
        
        await ct.record("gemini-3.1-flash-lite-preview", "flashlite", "g1", 10000, 8000, 100)
        await ct.record("gemini-3-flash-preview", "main_model", "g1", 5000, 3000, 200)
        
        rates = ct.get_cache_hit_rate("today")
        assert rates["_total"] == 73.3  # (8000+3000)/(10000+5000)*100 = 73.3%
        assert rates["gemini-3.1-flash-lite-preview"] == 80.0
        assert rates["gemini-3-flash-preview"] == 60.0
        
        print("✅ T11.1e: 缓存命中率计算正确")
    finally:
        shutil.rmtree(tmpdir)


async def test_timeline():
    """时间轴数据"""
    tmpdir = tempfile.mkdtemp()
    try:
        ct = CostTracker(data_dir=tmpdir)
        
        await ct.record("gemini-3.1-flash-lite-preview", "flashlite", "g1", 1000, 800, 50)
        
        timeline = ct.get_timeline("today", "hour")
        assert len(timeline) == 1
        assert timeline[0]["calls"] == 1
        
        print("✅ T11.1f: 时间轴数据正确")
    finally:
        shutil.rmtree(tmpdir)


async def test_persistence():
    """持久化：写入后重新加载"""
    tmpdir = tempfile.mkdtemp()
    try:
        ct1 = CostTracker(data_dir=tmpdir)
        await ct1.record("gemini-3.1-flash-lite-preview", "flashlite", "g1", 1000, 800, 50)
        await ct1._flush()
        
        # 创建新实例重新加载
        ct2 = CostTracker(data_dir=tmpdir)
        summary = ct2.get_summary("today")
        assert summary["total_calls"] == 1, "FAIL: 持久化后数据丢失"
        
        print("✅ T11.1g: 持久化和重新加载正确")
    finally:
        shutil.rmtree(tmpdir)


async def test_main_integration():
    """验证 main.py 集成"""
    source_path = os.path.join(PLUGIN_DIR, "main.py")
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    
    assert "from .cost_tracker import CostTracker" in source or "from cost_tracker import CostTracker" in source, \
        "FAIL: main.py 缺少 CostTracker 导入"
    assert "self._cost_tracker = CostTracker" in source, "FAIL: main.py 缺少 CostTracker 初始化"
    assert "await self._cost_tracker.record(" in source, "FAIL: main.py 缺少 CostTracker.record 调用"
    
    print("✅ T11.2: main.py CostTracker 集成验证通过")


async def main():
    print("=" * 60)
    print("Stage 11 测试：CostTracker 单元测试")
    print("=" * 60)
    
    await test_record_and_query()
    await test_cost_calculation()
    await test_by_model_grouping()
    await test_by_window_grouping()
    await test_cache_hit_rate()
    await test_timeline()
    await test_persistence()
    await test_main_integration()
    
    print()
    print("=" * 60)
    print("✅ Stage 11 所有测试通过！CostTracker 数据采集层验证完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
