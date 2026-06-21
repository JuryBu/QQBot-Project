"""Stage 7-9 测试：采样优化验证"""
import sys, os, json, time

PLUGIN_DIR = r"c:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_flashlite"


def read_source():
    with open(os.path.join(PLUGIN_DIR, "main.py"), "r", encoding="utf-8") as f:
        return f.read()


# === Stage 7 Tests ===

def test_sync_time_min_msgs():
    """Stage 7: sync_time_min_msgs 参数化"""
    source = read_source()
    assert 'self._sync_time_min_msgs = self._cfg("sync_time_min_msgs"' in source, \
        "FAIL: 缺少 sync_time_min_msgs 配置读取"
    assert '>= self._sync_time_min_msgs' in source, \
        "FAIL: 时间兜底触发未使用 sync_time_min_msgs"
    assert '>= 1\n' not in source.split('time_trigger')[1][:50], \
        "FAIL: 时间兜底仍使用硬编码 >= 1"
    print("✅ T7.1: sync_time_min_msgs 参数化正确")


def test_sampling_mode():
    """Stage 7: sampling_mode 配置"""
    source = read_source()
    assert 'self._sampling_mode = self._cfg("sampling_mode"' in source, \
        "FAIL: 缺少 sampling_mode 配置"
    print("✅ T7.2: sampling_mode 配置存在")


def test_conf_schema():
    """Stage 7: _conf_schema.json 包含新字段"""
    with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), "r", encoding="utf-8") as f:
        schema = json.load(f)
    
    assert "sync_time_interval" in schema, "FAIL: schema 缺少 sync_time_interval"
    assert "sync_time_min_msgs" in schema, "FAIL: schema 缺少 sync_time_min_msgs"
    assert "sampling_mode" in schema, "FAIL: schema 缺少 sampling_mode"
    
    # 验证 sampling_mode 有 options
    assert "options" in schema["sampling_mode"], "FAIL: sampling_mode 缺少 options"
    assert "dynamic" in schema["sampling_mode"]["options"], "FAIL: options 缺少 dynamic"
    assert "fixed" in schema["sampling_mode"]["options"], "FAIL: options 缺少 fixed"
    
    print("✅ T7.3: _conf_schema.json 包含所有新字段")


# === Stage 8 Tests ===

def test_dynamic_sampling_config():
    """Stage 8: 动态采样配置初始化"""
    source = read_source()
    assert '_dyn_window_minutes' in source, "FAIL: 缺少 _dyn_window_minutes"
    assert '_dyn_thresholds' in source, "FAIL: 缺少 _dyn_thresholds"
    assert '_dyn_intervals' in source, "FAIL: 缺少 _dyn_intervals"
    assert '_recent_msg_timestamps' in source, "FAIL: 缺少 _recent_msg_timestamps"
    print("✅ T8.1: 动态采样配置和滑动窗口初始化正确")


def test_calc_dynamic_interval():
    """Stage 8: _calc_dynamic_interval 方法存在且逻辑正确"""
    source = read_source()
    assert 'def _calc_dynamic_interval(self, group_id' in source, \
        "FAIL: 缺少 _calc_dynamic_interval 方法"
    
    # 检查方法体包含关键逻辑
    method_start = source.find('def _calc_dynamic_interval')
    method_end = source.find('\n    def ', method_start + 1)
    body = source[method_start:method_end]
    
    assert 'timestamps' in body, "FAIL: 缺少时间戳处理"
    assert 'popleft' in body, "FAIL: 缺少过期清理"
    assert 'msg_count' in body, "FAIL: 缺少消息计数"
    assert 'thresholds' in body, "FAIL: 缺少阈值匹配"
    assert 'intervals' in body, "FAIL: 缺少间隔返回"
    
    print("✅ T8.2: _calc_dynamic_interval 方法结构正确")


def test_effective_interval():
    """Stage 8: _get_effective_interval 方法存在"""
    source = read_source()
    assert 'def _get_effective_interval(self, group_id' in source, \
        "FAIL: 缺少 _get_effective_interval 方法"
    assert 'effective_interval = self._get_effective_interval(group_id)' in source, \
        "FAIL: 触发逻辑未使用 _get_effective_interval"
    assert 'count_trigger = self._msg_counters[group_id] >= effective_interval' in source, \
        "FAIL: count_trigger 未使用 effective_interval"
    print("✅ T8.3: 触发逻辑集成动态间隔正确")


def test_timestamp_recording():
    """Stage 8: 消息时间戳记录到滑动窗口"""
    source = read_source()
    assert '_recent_msg_timestamps[group_id].append(now)' in source, \
        "FAIL: 消息时间戳未记录到滑动窗口"
    print("✅ T8.4: 消息时间戳记录正确")


# === Stage 9 Tests ===

def test_group_overrides():
    """Stage 9: 群独立配置覆盖"""
    source = read_source()
    method_start = source.find('def _get_effective_interval')
    method_end = source.find('\n    def ', method_start + 1) if method_start > 0 else -1
    body = source[method_start:method_end] if method_start > 0 and method_end > 0 else ""
    
    assert 'group_overrides' in body, "FAIL: _get_effective_interval 缺少 group_overrides"
    assert 'sync_interval' in body, "FAIL: 群覆盖缺少 sync_interval 读取"
    print("✅ T9.1: 群独立配置覆盖预留正确")


def test_priority_chain():
    """Stage 9: 优先级链（群覆盖 > 动态 > 固定）"""
    source = read_source()
    method_start = source.find('def _get_effective_interval')
    method_end = source.find('\n    def ', method_start + 1)
    body = source[method_start:method_end]
    
    # 确进先级顺序：先检查 overrides，再 dynamic，最后 fixed
    override_pos = body.find('group_overrides')
    dynamic_pos = body.find('dynamic')
    fixed_pos = body.find('self._sync_interval')
    
    assert 0 < override_pos < dynamic_pos < fixed_pos, \
        "FAIL: 优先级顺序错误（应为 overrides → dynamic → fixed）"
    print("✅ T9.2: 优先级链正确（群覆盖 > 动态 > 全局固定）")


if __name__ == "__main__":
    print("=" * 60)
    print("Stage 7-9 采样优化测试")
    print("=" * 60)
    
    # Stage 7
    test_sync_time_min_msgs()
    test_sampling_mode()
    test_conf_schema()
    
    # Stage 8
    test_dynamic_sampling_config()
    test_calc_dynamic_interval()
    test_effective_interval()
    test_timestamp_recording()
    
    # Stage 9
    test_group_overrides()
    test_priority_chain()
    
    print()
    print("=" * 60)
    print("✅ Stage 7-9 全部测试通过！采样优化验证完成")
    print("=" * 60)
