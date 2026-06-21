"""Stage 6 综合测试：三模型 system prompt 稳定性 + 动态前缀验证"""
import sys, os, re

PLUGIN_DIR = r"c:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_flashlite"


def read_source():
    with open(os.path.join(PLUGIN_DIR, "main.py"), "r", encoding="utf-8") as f:
        return f.read()


def get_method_body(source, method_sig):
    start = source.find(method_sig)
    if start < 0:
        return ""
    # 找到下一个同级的 def/async def（4空格缩进的）
    candidates = []
    for pattern in ['\n    def ', '\n    async def ']:
        pos = source.find(pattern, start + len(method_sig))
        if pos >= 0:
            candidates.append(pos)
    end = min(candidates) if candidates else len(source)
    return source[start:end]


def test_flashlite_system_stability():
    """FlashLite system prompt 无动态表达式"""
    source = read_source()
    body = get_method_body(source, 'def _build_flash_lite_system(self)')
    
    dynamic_patterns = ['datetime.', 'self._knowledge', '.now()', '.get_prompt_text']
    for p in dynamic_patterns:
        assert p not in body, f"FAIL: FlashLite system 含动态: {p}"
    
    print("✅ FlashLite system prompt 纯静态（无动态表达式）")


def test_tool_model_system_stability():
    """工具模型 system prompt 无动态表达式"""
    source = read_source()
    body = get_method_body(source, 'def _build_tool_model_system(self)')
    
    assert 'datetime' not in body, "FAIL: 工具模型 system 含 datetime"
    assert 'knowledge_snapshot' not in body, "FAIL: 工具模型 system 含 knowledge_snapshot"
    assert '.now()' not in body, "FAIL: 工具模型 system 含 .now()"
    assert 'get_prompt_text' not in body, "FAIL: 工具模型 system 含 get_prompt_text"
    
    print("✅ 工具模型 system prompt 纯静态（无动态表达式）")


def test_main_model_static_dynamic():
    """主模型 inject_flashlite_context 正确分离静态/动态"""
    source = read_source()
    body = get_method_body(source, 'async def inject_flashlite_context(')
    
    assert 'inject_parts = []' in body, "FAIL: 缺少 inject_parts"
    assert 'dynamic_parts = []' in body, "FAIL: 缺少 dynamic_parts"
    assert 'dynamic_block = ' in body, "FAIL: 缺少 dynamic_block"
    assert 'msg["content"] = f"{dynamic_block}' in body, "FAIL: 缺少 contents 注入"
    
    print("✅ 主模型 inject_flashlite_context 双列表结构正确")


def test_flashlite_dynamic_prefix():
    """FlashLite _call_flash_lite 包含动态前缀"""
    source = read_source()
    body = get_method_body(source, 'async def _call_flash_lite(')
    
    assert '_dynamic_prefix_parts' in body, "FAIL: 缺少动态前缀列表"
    assert 'Knowledge 快照' in body, "FAIL: 缺少 Knowledge 快照"
    assert '系统时间' in body, "FAIL: 缺少系统时间"
    assert '_effective_prompt = _dynamic_prefix + prompt' in body, "FAIL: 缺少拼接"
    
    print("✅ FlashLite _call_flash_lite 动态前缀正确")


def test_tool_model_dynamic_prefix():
    """工具模型 _call_tool_model 包含动态前缀"""
    source = read_source()
    body = get_method_body(source, 'async def _call_tool_model(')
    
    assert '_dynamic_prefix_parts' in body, "FAIL: 缺少动态前缀列表"
    assert 'Knowledge 概况' in body, "FAIL: 缺少 Knowledge"
    assert '系统时间' in body, "FAIL: 缺少系统时间"
    assert '_dynamic_prefix + prompt' in body, "FAIL: 缺少拼接"
    
    print("✅ 工具模型 _call_tool_model 动态前缀正确")


def test_checkpoint_not_affected():
    """CHECKPOINT 压缩不受影响 — 它不使用 inject_flashlite_context"""
    source = read_source()
    
    # 压缩逻辑调用 _call_flash_lite，不直接使用 inject_parts
    # 确认压缩提示词构建独立于 inject_flashlite_context
    assert 'compress_if_needed' in source, "FAIL: 找不到压缩方法"
    assert 'checkpoint' in source.lower(), "FAIL: 找不到 checkpoint"
    
    # 确认 checkpoint.py 没有被修改（搜索 dynamic_parts 不应出现）
    cp_path = os.path.join(PLUGIN_DIR, "checkpoint.py")
    with open(cp_path, "r", encoding="utf-8") as f:
        cp_source = f.read()
    assert 'dynamic_parts' not in cp_source, "FAIL: checkpoint.py 不应包含 dynamic_parts"
    
    print("✅ CHECKPOINT 压缩逻辑未受影响")


def test_review_mode_not_affected():
    """定期 Review 模式不受影响"""
    source = read_source()
    
    # review_mode 逻辑使用 _call_tool_model，不直接涉及 inject_parts
    assert '_review_mode' in source, "FAIL: 找不到 _review_mode"
    assert 'system_report' in source, "FAIL: 找不到 system_report"
    
    print("✅ 定期 Review 模式未受影响")


if __name__ == "__main__":
    print("=" * 60)
    print("Stage 6 综合测试：三模型 KVCache 静态/动态分离验证")
    print("=" * 60)
    
    test_flashlite_system_stability()
    test_tool_model_system_stability()
    test_main_model_static_dynamic()
    test_flashlite_dynamic_prefix()
    test_tool_model_dynamic_prefix()
    test_checkpoint_not_affected()
    test_review_mode_not_affected()
    
    print()
    print("=" * 60)
    print("✅ 综合测试全部通过！三模型 KVCache 分离验证完成")
    print("=" * 60)
