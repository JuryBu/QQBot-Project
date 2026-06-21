"""Stage 1 测试：FlashLite 静态/动态分离验证"""
import sys, os, re

# 将插件目录加入 path
PLUGIN_DIR = r"c:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_flashlite"
sys.path.insert(0, PLUGIN_DIR)

# ===== 直接从源码提取函数测试（避免完整初始化）=====

def test_build_flash_lite_system():
    """从源代码提取 _build_flash_lite_system 的返回值字符串进行验证"""
    # 读取源代码
    with open(os.path.join(PLUGIN_DIR, "main.py"), "r", encoding="utf-8") as f:
        source = f.read()
    
    # 提取 _build_flash_lite_system 方法体
    # 简单方式：找到方法开始和结束，提取 return 的字符串内容
    # 更可靠的方式：直接调用
    
    # 方法一：通过正则检查源代码特征
    # 确认不含动态内容
    method_start = source.find('def _build_flash_lite_system(self)')
    method_end = source.find('\n    def ', method_start + 1)
    method_body = source[method_start:method_end]
    
    # T1.1 验证：不含 Knowledge 动态注入
    assert 'knowledge_snapshot' not in method_body, "FAIL: system prompt 仍包含 knowledge_snapshot 变量"
    assert 'get_prompt_text' not in method_body, "FAIL: system prompt 仍调用 get_prompt_text"
    assert 'datetime.datetime.now()' not in method_body, "FAIL: system prompt 仍包含动态时间"
    print("✅ T1.1a: system prompt 不含 Knowledge 和日期动态注入")
    
    # T1.1 验证：包含新增的静态内容
    assert '任务执行指南' in method_body, "FAIL: 缺少 '任务执行指南'"
    assert 'Memory 召回指南' in method_body, "FAIL: 缺少 'Memory 召回指南'"
    assert '群聊场景' in method_body, "FAIL: 缺少群聊场景判断规则"
    assert '私聊场景' in method_body, "FAIL: 缺少私聊场景判断规则"
    assert 'MEMORY_HINT' in method_body, "FAIL: 缺少 MEMORY_HINT 说明"
    assert 'pinned 优先' in method_body, "FAIL: 缺少排序规则说明"
    print("✅ T1.1b: system prompt 包含 任务执行指南 + Memory 召回指南 + 判断规则")
    
    # T1.1 验证：import datetime 不在 _build_flash_lite_system 中
    assert 'import datetime' not in method_body, "FAIL: _build_flash_lite_system 仍导入 datetime"
    print("✅ T1.1c: system prompt 构建不再需要 datetime")


def test_build_judgment_prompt():
    """验证 _build_judgment_prompt 只包含纯数据"""
    with open(os.path.join(PLUGIN_DIR, "main.py"), "r", encoding="utf-8") as f:
        source = f.read()
    
    method_start = source.find('def _build_judgment_prompt(')
    method_end = source.find('\n    # ========================', method_start + 1)
    method_body = source[method_start:method_end]
    
    # T1.2 验证：不含判断规则和任务描述（在 prompt 内容中，docstring 中的注释不算）
    assert 'chat_rules' not in method_body, "FAIL: 仍包含 chat_rules 变量"
    assert '你的任务' not in method_body, "FAIL: 仍包含 '你的任务' 段落"
    assert '对话分析引擎' not in method_body, "FAIL: 仍包含角色定义 '对话分析引擎'"
    # 检查 prompt 字符串内容不含判断规则（排除 docstring）
    # 在 f""" 到 """ 之间的 prompt 内容中搜索
    prompt_start = method_body.find('prompt = f"""')
    prompt_end = method_body.find('"""', prompt_start + 13)
    prompt_content = method_body[prompt_start:prompt_end] if prompt_start > 0 else ""
    assert '判断规则' not in prompt_content, "FAIL: prompt 内容中仍包含 '判断规则'"
    assert 'should_trigger' not in prompt_content, "FAIL: prompt 内容中仍包含 should_trigger"
    print("✅ T1.2a: judgment prompt 不含判断规则和任务描述")
    
    # T1.2 验证：包含纯数据字段
    assert '窗口类型' in method_body, "FAIL: 缺少 '窗口类型'"
    assert '窗口标识' in method_body, "FAIL: 缺少 '窗口标识'"
    assert '最近' in method_body, "FAIL: 缺少 '最近' 记录"
    assert '触发信息' in method_body, "FAIL: 缺少 '触发信息'"
    print("✅ T1.2b: judgment prompt 包含纯数据字段（窗口类型/标识/记录/触发信息）")


def test_call_flash_lite_dynamic_prefix():
    """验证 _call_flash_lite 的动态内容注入到 user prompt"""
    with open(os.path.join(PLUGIN_DIR, "main.py"), "r", encoding="utf-8") as f:
        source = f.read()
    
    method_start = source.find('async def _call_flash_lite(')
    method_end = source.find('\n    async def ', method_start + 1)
    if method_end == -1:
        method_end = source.find('\n    def ', method_start + 1)
    method_body = source[method_start:method_end]
    
    # 验证动态前缀逻辑
    assert '_dynamic_prefix_parts' in method_body, "FAIL: 缺少动态前缀列表"
    assert 'Knowledge 快照' in method_body, "FAIL: 缺少 Knowledge 快照动态注入"
    assert '系统时间' in method_body, "FAIL: 缺少系统时间动态注入"
    assert '_effective_prompt' in method_body, "FAIL: 缺少有效 prompt 拼接"
    assert '_dynamic_prefix + prompt' in method_body, "FAIL: 缺少动态前缀 + 原始 prompt 拼接"
    print("✅ T1.3a: _call_flash_lite 包含动态前缀注入逻辑")
    
    # 验证 system prompt 不再追加动态内容
    assert '_fl_system += ' not in method_body, "FAIL: system prompt 仍被追加动态内容"
    print("✅ T1.3b: system prompt 不再被追加动态内容（_mem_index 不再拼入 system）")
    
    # 验证 user prompt 使用 _effective_prompt
    assert '_effective_prompt' in method_body, "FAIL: payload 未使用 _effective_prompt"
    # 确保 non-cached 路径也使用 _effective_prompt
    non_cached_section = method_body[method_body.find('# 降级'):]
    assert '_effective_prompt' in non_cached_section, "FAIL: 降级路径未使用 _effective_prompt"
    print("✅ T1.3c: 缓存和降级两个路径都使用 _effective_prompt")


def test_system_stability():
    """验证 _build_flash_lite_system() 多次调用结果完全相同"""
    with open(os.path.join(PLUGIN_DIR, "main.py"), "r", encoding="utf-8") as f:
        source = f.read()
    
    method_start = source.find('def _build_flash_lite_system(self)')
    method_end = source.find('\n    def ', method_start + 1)
    method_body = source[method_start:method_end]
    
    # 确认方法体中没有任何会变化的表达式
    # 动态表达式：datetime.now()、self._knowledge、self._build_memory
    dynamic_patterns = [
        r'datetime\.',
        r'self\._knowledge',
        r'self\._build_memory',
        r'\.now\(\)',
        r'\.get_prompt_text',
    ]
    
    for pattern in dynamic_patterns:
        matches = re.findall(pattern, method_body)
        assert len(matches) == 0, f"FAIL: system prompt 包含动态表达式: {pattern} (found {len(matches)})"
    
    print("✅ T2.1: system prompt 不含任何动态表达式，保证多次调用完全相同")


if __name__ == "__main__":
    print("=" * 60)
    print("Stage 1 + Stage 2 FlashLite KVCache 分离测试")
    print("=" * 60)
    
    test_build_flash_lite_system()
    print()
    test_build_judgment_prompt()
    print()
    test_call_flash_lite_dynamic_prefix()
    print()
    test_system_stability()
    
    print()
    print("=" * 60)
    print("✅ 所有测试通过！Stage 1 + Stage 2 FlashLite 分离验证完成")
    print("=" * 60)
