"""Stage 3 测试：主模型 inject_flashlite_context 静态/动态分离验证"""
import sys, os, re

PLUGIN_DIR = r"c:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_flashlite"

def read_inject_method():
    """读取 inject_flashlite_context 方法体"""
    with open(os.path.join(PLUGIN_DIR, "main.py"), "r", encoding="utf-8") as f:
        source = f.read()
    method_start = source.find('async def inject_flashlite_context(')
    # 找下一个同级方法定义
    method_end = source.find('\n    async def ', method_start + 1)
    if method_end == -1:
        method_end = source.find('\n    def ', method_start + 1)
    return source[method_start:method_end]


def test_dual_list_init():
    """验证 inject_parts 和 dynamic_parts 双列表初始化"""
    body = read_inject_method()
    assert 'inject_parts = []' in body, "FAIL: 缺少 inject_parts 初始化"
    assert 'dynamic_parts = []' in body, "FAIL: 缺少 dynamic_parts 初始化"
    print("✅ T3.0: inject_parts 和 dynamic_parts 双列表初始化正确")


def test_static_sections():
    """验证静态 Section 仍然使用 inject_parts.append"""
    body = read_inject_method()
    
    # 静态 Section 列表（应留在 inject_parts 中）
    static_markers = [
        ("系统架构认知", "S0 体系认知"),
        ("输出风格硬性约束", "S1 输出风格"),
        ("回复格式要求", "S5 回复格式"),
        ("工具调用规范", "S5 工具规范"),
        ("Memory 记忆系统", "S6 Memory 指南"),
        ("Knowledge 全局对话概览", "S7 Knowledge 说明"),
        ("文件与链接处理规范", "S7.5 文件处理"),
        ("Sandbox 工作空间", "S8 Sandbox 空间"),
        ("自定义工具系统", "S9 自定义工具"),
        ("Task 后台任务系统", "S10 Task 系统"),
        ("工具分类速查", "S11 工具速查"),
    ]
    
    for marker, label in static_markers:
        # 更精确的方式：找到真正包含 marker 的 inject_parts.append 或 dynamic_parts.append 块
        search_start = 0
        found_in_inject = False
        while True:
            pos = body.find("inject_parts.append(", search_start)
            if pos == -1:
                break
            # 取这个 append 后的 500 字符看是否包含 marker
            snippet = body[pos:pos+500]
            if marker in snippet:
                found_in_inject = True
                break
            search_start = pos + 1
        
        assert found_in_inject, f"FAIL: {label} ({marker}) 未在 inject_parts 中找到"
    
    print(f"✅ T3.1: {len(static_markers)} 个静态 Section 正确保留在 inject_parts 中")


def test_dynamic_sections():
    """验证动态 Section 使用 dynamic_parts.append"""
    body = read_inject_method()
    
    # 动态 Section 列表（应在 dynamic_parts 中）
    dynamic_markers = [
        ("当前时间", "时间"),
        ("knowledge_text", "Knowledge 快照"),
        ("ctx_block", "上下文摘要"),
        ("Memory 召回", "Memory 召回"),
        ("用户卡片", "用户卡片"),
        ("Sandbox 环境", "Sandbox 环境"),
    ]
    
    for marker, label in dynamic_markers:
        idx = body.find(f"dynamic_parts.append")
        # 确需找到 dynamic_parts.append 中含有 marker 的位置
        search_start = 0
        found = False
        while True:
            pos = body.find("dynamic_parts.append", search_start)
            if pos == -1:
                break
            # 取这个 append 后的 200 字符
            snippet = body[pos:pos+300]
            if marker in snippet:
                found = True
                break
            search_start = pos + 1
        
        assert found, f"FAIL: {label} ({marker}) 未在 dynamic_parts 中找到"
    
    print(f"✅ T3.2: {len(dynamic_markers)} 个动态 Section 正确在 dynamic_parts 中")


def test_time_not_in_static():
    """验证时间在 dynamic_parts 中，不在 inject_parts 的 S0 中"""
    body = read_inject_method()
    
    # 确认 dynamic_parts 中有时间
    assert "dynamic_parts.append(" in body, "FAIL: 没有 dynamic_parts.append"
    
    # 找包含 strftime 的 dynamic_parts.append
    search_start = 0
    found_time_in_dynamic = False
    while True:
        pos = body.find("dynamic_parts.append(", search_start)
        if pos == -1:
            break
        snippet = body[pos:pos+300]
        if "strftime" in snippet or "当前时间" in snippet:
            found_time_in_dynamic = True
            break
        search_start = pos + 1
    
    assert found_time_in_dynamic, "FAIL: dynamic_parts 中没有时间注入"
    
    # 确认 inject_parts 中没有包含 strftime 的 append
    search_start = 0
    time_in_inject = False
    while True:
        pos = body.find("inject_parts.append(", search_start)
        if pos == -1:
            break
        snippet = body[pos:pos+800]
        # 截到下一个 append 或 # 注释行
        end = snippet.find("\n\n            #", 20)
        if end > 0:
            snippet = snippet[:end]
        if "strftime" in snippet:
            time_in_inject = True
            break
        search_start = pos + 1
    
    assert not time_in_inject, "FAIL: inject_parts 中仍有 strftime 时间格式化"
    
    print("✅ T3.3: 时间正确在 dynamic_parts，inject_parts 中无 strftime")


def test_final_injection_logic():
    """验证末尾注入逻辑：static→system_prompt, dynamic→contents"""
    body = read_inject_method()
    
    # 检查双路径注入
    assert 'KVCache 优化' in body, "FAIL: 缺少 KVCache 优化注释"
    assert '静态部分 → system_prompt' in body or '静态部分' in body, "FAIL: 缺少静态注入注释"
    assert '动态部分 → contents' in body or 'dynamic_block' in body, "FAIL: 缺少动态注入逻辑"
    
    # 检查 dynamic_block 拼接
    assert 'dynamic_block = ' in body, "FAIL: 缺少 dynamic_block 构建"
    assert '---' in body[body.find('dynamic_block = '):], "FAIL: 缺少动态前缀分隔符 ---"
    
    # 检查 contents 注入
    assert 'msg["content"] = f"{dynamic_block}' in body, "FAIL: 缺少 contents 注入"
    
    # 检查日志包含 static/dynamic 计数
    assert 'static=' in body, "FAIL: 日志缺少 static 计数"
    assert 'dynamic=' in body, "FAIL: 日志缺少 dynamic 计数"
    
    print("✅ T3.4: 末尾注入逻辑正确（static→system_prompt, dynamic→contents）")


def test_no_knowledge_in_inject_parts():
    """验证 inject_parts（将注入 system_prompt）中不含 Knowledge 动态内容"""
    body = read_inject_method()
    
    # Knowledge 全局缓存应在 dynamic_parts，不在 inject_parts
    # 找 knowledge_text 的 append
    kg_section = body[body.find('# 1. Knowledge 全局缓存'):body.find('# 2. Flash Lite')]
    assert 'dynamic_parts.append(knowledge_text)' in kg_section, "FAIL: knowledge_text 未在 dynamic_parts"
    assert 'inject_parts.append(knowledge_text)' not in kg_section, "FAIL: knowledge_text 仍在 inject_parts"
    
    print("✅ T3.5: Knowledge 正确在 dynamic_parts 中，不在 inject_parts 中")


if __name__ == "__main__":
    print("=" * 60)
    print("Stage 3 主模型 inject_flashlite_context 静态/动态分离测试")
    print("=" * 60)
    
    test_dual_list_init()
    test_static_sections()
    test_dynamic_sections()
    test_time_not_in_static()
    test_final_injection_logic()
    test_no_knowledge_in_inject_parts()
    
    print()
    print("=" * 60)
    print("✅ 所有测试通过！Stage 3 主模型分离验证完成")
    print("=" * 60)
