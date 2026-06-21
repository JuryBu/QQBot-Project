# Plan 2-CP 压缩策略与 Prompt 工程

## 压缩触发条件

### 三重守卫

压缩需要 **同时满足** 三个条件才能触发：

```python
should_compress = (
    total_tokens > checkpoint_token_limit              # ① token 超限
    and len(messages) > keep_recent                    # ② 有足够多消息可压缩
    and (now - last_compress_time) > cooldown_seconds  # ③ 冷却期已过
)
```

### 面板参数

| 参数名 | 默认值 | 说明 |
|---|---|---|
| `checkpoint_token_limit` | `50000` | 总 token 超过此阈值触发压缩 |
| `checkpoint_keep_recent` | `10` | 至少保留最近 N 条消息不压缩 |
| `checkpoint_compress_front_ratio` | `0.7` | 压缩 candidate 前多少比例（0.7 = 压缩 70%） |
| `checkpoint_cooldown_seconds` | `300` | 两次压缩最小间隔（秒） |
| `checkpoint_target_min` | `0.20` | 压缩率下限 |
| `checkpoint_target_max` | `0.40` | 压缩率上限 |

## 压缩范围计算

```python
# candidate = 当前完整的 T（T1_msg + messages）
candidate = build_candidate_from_t_file(t_file)

# 计算需要压缩的消息数
compress_count = max(1, int(len(candidate) * compress_front_ratio))

# 确保至少保留 keep_recent 条消息不被压缩
compress_count = min(compress_count, len(candidate) - keep_recent)

# 分割
to_compress = candidate[:compress_count]   # 送入压缩的部分（含 T1 摘要）
to_keep = candidate[compress_count:]       # 保留为原文的部分
```

### RNN 遗忘效应

- 当 T1 摘要存在时，它位于 `candidate[0]`（或 [0]+[1] 的消息对）
- 压缩范围包含 T1 → 旧的压缩摘要和新消息一起被再次压缩
- 旧信息密度自然递减 = 遗忘效应
- 类似 RNN 的隐藏状态不断被新信息洗涤

## Prompt 工程

### 当前问题

旧 Prompt 只说"压缩到 20%-40%"，Flash Lite 实际生成了极短的摘要（<1% 压缩率）。

### 改进策略

**核心改进**：不用百分比描述目标，用 **具体字数和 token 数** 明确要求。

### 新 Prompt 模板

```python
def build_compress_prompt(
    messages_text: str,
    original_tokens: int,
    target_min_ratio: float,
    target_max_ratio: float,
    has_previous_summary: bool,
) -> str:
    target_mid = (target_min_ratio + target_max_ratio) / 2
    target_tokens = int(original_tokens * target_mid)
    target_chars = int(target_tokens * 1.5)  # 中文 ~1.5 字/token

    min_chars = int(original_tokens * target_min_ratio * 1.5)
    max_chars = int(original_tokens * target_max_ratio * 1.5)

    summary_note = ""
    if has_previous_summary:
        summary_note = (
            "\n注意：输入内容开头有一段 [对话历史压缩摘要]，"
            "这是之前轮次的压缩结果。"
            "请将其与后续新消息融合为一份统一的新摘要，"
            "旧摘要中的信息可以适当精简但不要完全丢弃。\n"
        )

    return f"""你是一个对话压缩引擎。将以下对话内容压缩为结构化摘要。
{summary_note}
## 输出长度要求（关键！）
- 目标长度：约 {target_chars} 个中文字（{min_chars}~{max_chars} 字范围内）
- 原始内容约 {original_tokens} tokens
- 你的输出必须在 {min_chars} 到 {max_chars} 字之间
- 过短（< {min_chars} 字）或过长（> {max_chars} 字）都是失败

## 压缩原则
1. 按话题/时间段分块，用简洁的标题标注每个话题段
2. 保留所有参与者名字和 QQ 号
3. 保留关键事实：人名、地名、数字、日期、结论、决定
4. 保留情感倾向和关系动态
5. 用「」包围重要原文引用
6. 去除：重复内容、纯表情、日常闲聊（你好/再见）、无信息量的应答
7. 如涉及图片/文件/工具调用，注明 [图片]  [文件] [工具:名称→结果摘要]

## 输出格式
直接输出摘要，不要输出其他说明文字。格式参考：

【话题：xxx（时间段）】
参与者A 和 B 讨论了...关键信息:「原文引用」

## 原始内容（{len(messages_text)} 字）
{messages_text}"""
```

## 压缩率验证机制

```python
async def compress_and_validate(
    to_compress: list[dict],
    flash_lite_caller,
    target_min: float,
    target_max: float,
) -> tuple[str, float]:
    """压缩并验证压缩率"""

    # 序列化待压缩消息
    messages_text = serialize_messages(to_compress)
    original_tokens = estimate_tokens(messages_text)

    # 检测是否包含旧的 T1 摘要
    has_previous_summary = (
        len(to_compress) > 0
        and to_compress[0].get("role") == "user"
        and "[对话历史压缩摘要]" in to_compress[0].get("content", "")
    )

    # 构建 prompt
    prompt = build_compress_prompt(
        messages_text, original_tokens,
        target_min, target_max,
        has_previous_summary,
    )

    # 调用 Flash Lite
    compressed_text = await flash_lite_caller(prompt)
    compressed_tokens = estimate_tokens(compressed_text)

    # 计算实际压缩率
    actual_ratio = compressed_tokens / max(original_tokens, 1)

    # 验证
    if actual_ratio < target_min:
        logger.warning(
            f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 低于目标 "
            f"{target_min:.0%}，摘要可能过于简略 "
            f"(原文 {original_tokens} tokens → {compressed_tokens} tokens)"
        )
    elif actual_ratio > target_max:
        logger.warning(
            f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 高于目标 "
            f"{target_max:.0%}，摘要可能保留过多细节"
        )
    else:
        logger.info(
            f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} ✓ "
            f"(目标 {target_min:.0%}~{target_max:.0%})"
        )

    return compressed_text, actual_ratio
```

## 压缩后的 T 文件更新

```python
# 压缩后更新 T 文件
t_file["T1"] = {
    "compressed_summary": compressed_text,
    "token_count": compressed_tokens,
    "compression_ratio": actual_ratio,
    "original_msg_count": t_file["T1"]["original_msg_count"] + len(msgs_compressed),
    "compression_count": t_file["T1"]["compression_count"] + 1,
    "last_compress_time": now_iso,
    "compress_history": t_file["T1"]["compress_history"] + [{
        "time": now_iso,
        "before_tokens": original_tokens,
        "after_tokens": compressed_tokens,
        "ratio": actual_ratio,
        "msgs_compressed": len(msgs_compressed),
    }],
}
t_file["messages"] = to_keep_messages  # 未被压缩的消息
t_file["metadata"]["updated_at"] = now_iso
t_file["metadata"]["total_compressions"] += 1
```
