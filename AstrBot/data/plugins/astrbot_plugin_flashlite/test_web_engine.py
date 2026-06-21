"""Web Engine 测试 — 验证 ContentExtractor + SessionManager + aiohttp降级"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from web_engine import ContentExtractor, WebFetchEngine

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
    print("=" * 50)
    print("1. ContentExtractor 测试")
    print("=" * 50)
    
    html = "<html><head><title>Test</title></head><body><h1>Hello</h1><p>World</p><script>var x=1;</script></body></html>"
    
    text = ContentExtractor.html_to_text(html)
    check("HTML→文本去script", "var x" not in text)
    check("HTML→文本保留内容", "Hello" in text and "World" in text)
    
    md = ContentExtractor.html_to_markdown(html)
    check("HTML→Markdown有内容", "Hello" in md or "World" in md)
    
    links_html = '<a href="https://example.com">Example</a> <a href="https://test.com">Test</a> <a href="/relative">Skip</a>'
    links = ContentExtractor.extract_links(links_html)
    check("链接提取数量", len(links) == 2, f"got {len(links)}")
    check("链接包含Example", any(l["url"] == "https://example.com" for l in links))
    check("跳过相对链接", not any("/relative" in l["url"] for l in links))
    
    long_text = "A" * 10000
    compressed = ContentExtractor.compress_text(long_text, 3000)
    check("文本压缩", len(compressed) <= 3500, f"compressed={len(compressed)}")
    check("压缩含省略标记", "省略" in compressed)
    
    short_text = "短文本"
    check("短文本不压缩", ContentExtractor.compress_text(short_text, 3000) == short_text)

    print("\n" + "=" * 50)
    print("2. WebFetchEngine 初始化")
    print("=" * 50)
    
    engine = WebFetchEngine()
    check("引擎创建", engine is not None)
    check("初始未初始化", not engine._initialized)
    
    print("\n" + "=" * 50)
    print("3. aiohttp 降级抓取测试")
    print("=" * 50)
    
    # 强制降级
    engine._use_playwright = False
    await engine.init()
    check("降级模式初始化", engine._initialized)
    
    # URL 校验
    r1 = await engine.fetch_page("ftp://invalid", mode="text")
    check("非HTTP URL拒绝", "error" in r1)
    
    # 实际抓取（对本地不可用的URL测试错误处理）
    r2 = await engine.fetch_page("http://localhost:99999/nonexist", mode="text", timeout=3000)
    check("不可达URL返回error", "error" in r2)
    
    # interact 在降级模式下应报错
    r3 = await engine.interact(action="click", url="http://example.com")
    check("降级模式交互报错", "error" in r3)

    print(f"\n{'=' * 50}")
    print(f"WebEngine 测试结果: {passed} 通过, {failed} 失败")
    print(f"{'=' * 50}")

if __name__ == "__main__":
    asyncio.run(test_all())
