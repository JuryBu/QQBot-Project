"""F3.6 smoke 测试：_msg_fingerprint 多模态假阳性修复 + 区分度提升。

为什么不直接 import main.py：
main.py 顶部 import astrbot.api / aiohttp 等重依赖，纯逻辑单测环境拉不起来。
_msg_fingerprint 是无外部依赖的 @staticmethod 纯函数，因此本测试从 main.py 源码中
**精确抽取该方法体并 exec 还原真实函数**（而非手工等价复刻），保证测的是线上真代码。

运行：AstrBot/.venv/Scripts/python.exe test_msg_fingerprint_f36.py
（普通 python 也可，无第三方依赖）
"""
import ast
import os
import textwrap


def _load_real_fingerprint():
    """从 main.py 抽取 _msg_fingerprint 的真实实现并编译成可调用函数。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_msg_fingerprint":
            func_src = ast.get_source_segment(src, node)
            # 去掉 @staticmethod 装饰器残留与缩进，作为顶层函数 exec
            func_src = textwrap.dedent(func_src)
            ns = {}
            exec(func_src, ns)
            return ns["_msg_fingerprint"]
    raise RuntimeError("未在 main.py 中找到 _msg_fingerprint")


fp = _load_real_fingerprint()


def test_multimodal_no_false_positive():
    """两条内容不同的多模态消息（content=list）指纹必须不同（修假阳性根因）。"""
    msg_a = {
        "role": "user",
        "content": [
            {"type": "text", "text": "帮我看看这张猫的照片"},
            {"type": "image_url", "image_url": {"url": "https://x/cat.png"}},
        ],
    }
    msg_b = {
        "role": "user",
        "content": [
            {"type": "text", "text": "这张狗的照片怎么样"},
            {"type": "image_url", "image_url": {"url": "https://x/dog.png"}},
        ],
    }
    fa, fb = fp(msg_a), fp(msg_b)
    # 旧实现 str(list)[:50] → 两条都以 "[{'type': 'text'..." 开头 → 前缀全同 → 假阳性
    assert not fa.startswith("user|[{"), f"仍是裸 str(list) 假阳性: {fa}"
    assert fa != fb, f"两条不同多模态消息指纹相同（假阳性未修）:\n  A={fa}\n  B={fb}"
    print("[PASS] 多模态不同消息指纹不同（假阳性已修）")
    print(f"       A = {fa}")
    print(f"       B = {fb}")


def test_str_content_compat():
    """content 为 str 时保持兼容，不同文本指纹不同。"""
    fa = fp({"role": "user", "content": "你好世界"})
    fb = fp({"role": "user", "content": "再见世界"})
    assert fa != fb
    assert fa.startswith("user|你好世界")
    print("[PASS] str content 兼容且区分正常")


def test_tool_calls_distinguish():
    """assistant 带不同 tool_calls.id 时指纹不同（提区分度）。"""
    base_content = "好的，我来查询"
    msg_a = {
        "role": "assistant",
        "content": base_content,
        "tool_calls": [{"id": "call_aaa", "function": {"name": "view_file"}}],
    }
    msg_b = {
        "role": "assistant",
        "content": base_content,
        "tool_calls": [{"id": "call_bbb", "function": {"name": "view_file"}}],
    }
    fa, fb = fp(msg_a), fp(msg_b)
    assert fa != fb, f"相同 content 不同 tool_calls.id 指纹却相同:\n  A={fa}\n  B={fb}"
    assert "call_aaa" in fa and "call_bbb" in fb
    print("[PASS] tool_calls.id 纳入指纹，区分度提升")


def test_length_200():
    """摘要长度提升到 200（旧为 50）。"""
    long_text = "甲" * 300
    f = fp({"role": "user", "content": long_text})
    # role(user) + | + 200 字 content + 尾部 |tc=|...
    assert ("甲" * 200) in f, "长度未提升到 200"
    assert ("甲" * 201) not in f, "截断长度异常（超过 200）"
    print("[PASS] 摘要长度 50 → 200")


if __name__ == "__main__":
    test_multimodal_no_false_positive()
    test_str_content_compat()
    test_tool_calls_distinguish()
    test_length_200()
    print("\nALL_PASS F3.6")
