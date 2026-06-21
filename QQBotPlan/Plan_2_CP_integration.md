# Plan 2-CP 集成点：代码修改清单

## 修改总览

| 文件 | 操作 | 说明 |
|---|---|---|
| `checkpoint.py` | **重写** | 新增 T 文件管理类 `TFileManager`，保留 token 估算工具 |
| `main.py` on_llm_request | **修改** | 替换 req.contexts 为 T 文件内容 |
| `main.py` 同步触发 | **修改** | 删除旧 CHECKPOINT 调用，FlashLite 改用 T 文件上下文 |
| `main.py` _build_judgment_prompt | **修改** | 上下文来源改为 T 文件 |
| `agent.py` | **删除部分** | 移除 `_get_checkpoint_summary`、`build_contents` |
| 面板配置 | **新增** | compress_front_ratio 和 cooldown_seconds 参数 |

---

## 1. checkpoint.py 重写

### 保留的工具函数
```
estimate_tokens(text) → int
estimate_message_tokens(msg) → int   # 需要适配新格式
_save_checkpoint(...)                 # 供面板统计
get_stats(...)                        # 供面板统计
```

### 删除的函数
```
check_and_compress()         → 删除（整个旧逻辑）
build_context_for_main_model() → 删除
_build_compress_prompt()     → 替换为新版本
```

### 新增 TFileManager 类

```python
class TFileManager:
    """Per-window T 文件管理器"""

    CHECKPOINTS_DIR = os.path.join(DB_DIR, "checkpoints")

    def __init__(self, checkpoint_mgr: CheckpointManager):
        self._checkpoint_mgr = checkpoint_mgr
        self._locks: Dict[str, asyncio.Lock] = {}  # per-window 锁

    def _get_lock(self, window_key: str) -> asyncio.Lock:
        if window_key not in self._locks:
            self._locks[window_key] = asyncio.Lock()
        return self._locks[window_key]

    def _file_path(self, window_key: str) -> str:
        """获取 T 文件路径"""
        safe_name = window_key.replace(":", "_")
        return os.path.join(self.CHECKPOINTS_DIR, f"{safe_name}.json")

    async def load(self, window_key: str) -> dict:
        """加载 T 文件，不存在则创建空文件"""

    async def save(self, window_key: str, t_file: dict) -> None:
        """原子保存 T 文件（先写临时文件再重命名）"""

    async def append_messages(self, window_key: str, new_messages: list[dict]) -> dict:
        """追加新消息到 T 文件"""

    async def compress_if_needed(
        self,
        window_key: str,
        t_file: dict,
        flash_lite_caller,
        token_limit: int,
        keep_recent: int,
        compress_front_ratio: float,
        cooldown_seconds: int,
        target_min: float,
        target_max: float,
    ) -> tuple[dict, Optional[dict]]:
        """检查并执行压缩，返回 (更新后的 t_file, 压缩结果或 None)"""

    def build_llm_contexts(self, t_file: dict) -> list[dict]:
        """从 T 文件构建 OpenAI 格式 contexts"""

    def build_flashlite_context(self, t_file: dict, max_tokens: int = 8000) -> str:
        """从 T 文件构建 FlashLite 触发判断用的上下文字符串"""
```

---

## 2. main.py on_llm_request 修改

### 删除的逻辑（L2667-2687）
```python
# 删除：旧的 CHECKPOINT 摘要注入到 system_prompt
if window_key:
    checkpoint_text = await self._agent_builder._get_checkpoint_summary(window_key)
    if checkpoint_text:
        inject_parts.append(f"## CHECKPOINT 历史压缩摘要\n...")
```

### 新增的逻辑（在 inject_parts 构建之后，返回之前）

```python
# === 新 CHECKPOINT：T 文件管理 ===
try:
    window_key = self._extract_window_key(event)
    if window_key:
        t_file = await self._t_file_mgr.load(window_key)

        # 1. 从 req.contexts 增量提取新消息追加到 T
        new_msgs = self._extract_new_messages(req.contexts, t_file)
        if new_msgs:
            await self._t_file_mgr.append_messages(window_key, new_msgs)
            t_file = await self._t_file_mgr.load(window_key)

        # 2. 检查是否需要压缩
        t_file, compress_result = await self._t_file_mgr.compress_if_needed(
            window_key=window_key,
            t_file=t_file,
            flash_lite_caller=self._call_flash_lite,
            token_limit=self._config.get("checkpoint_token_limit", 50000),
            keep_recent=self._config.get("checkpoint_keep_recent", 10),
            compress_front_ratio=self._config.get("checkpoint_compress_front_ratio", 0.7),
            cooldown_seconds=self._config.get("checkpoint_cooldown_seconds", 300),
            target_min=self._config.get("checkpoint_target_min", 0.20),
            target_max=self._config.get("checkpoint_target_max", 0.40),
        )

        if compress_result:
            self._stats["checkpoints"] += 1
            logger.info(f"[CHECKPOINT] {window_key}: {compress_result}")

        # 3. 替换 req.contexts 为 T 文件内容
        req.contexts = self._t_file_mgr.build_llm_contexts(t_file)

except Exception as e:
    logger.error(f"[CHECKPOINT] T 文件处理异常: {e}，回退到原始 contexts")
    # 异常时不替换 req.contexts，保持 AstrBot 原始行为
```

### 新增的辅助方法

```python
def _extract_window_key(self, event) -> Optional[str]:
    """从 event 中提取 window_key"""
    umo = getattr(event, "unified_msg_origin", "")
    if ":GroupMessage:" in umo:
        return "GroupMessage:" + umo.split(":")[-1]
    elif ":FriendMessage:" in umo:
        return "FriendMessage:" + umo.split(":")[-1]
    return None

def _extract_new_messages(self, contexts: list[dict], t_file: dict) -> list[dict]:
    """从 req.contexts 中提取 T 文件尚未记录的新消息"""
    existing_count = len(t_file.get("messages", []))
    # T1 压缩了的消息数 + T 文件中现有消息数 = 已处理的总数
    processed_count = t_file["T1"].get("original_msg_count", 0) + existing_count

    if len(contexts) > processed_count:
        return contexts[processed_count:]
    return []
```

---

## 3. main.py 同步触发修改

### 删除的逻辑（L866-881 / L1169-1184）
```python
# 删除：旧的 check_and_compress 调用
cp_result = await self._checkpoint_mgr.check_and_compress(
    window_id=group_id,
    window_type="group",
    flash_lite_caller=self._call_flash_lite,
)
```

### FlashLite 上下文来源修改

当前 `_build_judgment_prompt` 使用 `recent_context`（从 messages.db 读的最近 N 条消息）。
升级后改为从 T 文件读取更完整的上下文：

```python
# 原来的
recent_context = await self._get_recent_context(group_id, ...)

# 改为
t_file = await self._t_file_mgr.load(f"GroupMessage:{group_id}")
recent_context = self._t_file_mgr.build_flashlite_context(t_file, max_tokens=8000)
```

> ⚠️ FlashLite 仍然需要一个 `max_tokens` 限制来截断上下文，因为触发判断不需要全部历史。

---

## 4. LLM 回复后回写 T 文件

主模型回复后，assistant 消息需要追加到 T 文件。

### 方案：利用已有的延迟持久化逻辑

当前 `on_llm_request` 中 L2546-2596 有「延迟持久化」逻辑，从 `req.contexts` 中提取最近的 assistant 回复写入 persistence。

**改进**：在这段逻辑中同时将 assistant 回复追加到 T 文件。

```python
# 在延迟持久化逻辑中追加
if window_key and t_file:
    # 从 req.contexts 中找最近的 assistant 回复
    for msg in reversed(req.contexts):
        if msg.get("role") == "assistant":
            await self._t_file_mgr.append_messages(window_key, [msg])
            break
```

> 注意：这里存在时序问题——`on_llm_request` 在 LLM 调用**之前**运行，此时 req.contexts 中的 assistant 消息是**上一轮**的回复。这恰好是正确的：上一轮的回复在本轮被追加记录。

---

## 5. Knowledge 系统兼容

### 当前流程
```
QQ 消息 → messages.db → recent_context → FlashLite prompt
       → Flash Lite 输出 knowledge_update → KnowledgeCache.update_window()
```

### 升级后流程
```
QQ 消息 → messages.db（记录不变）
       → T 文件读取上下文 → FlashLite prompt  ← 上下文来源变更
       → Flash Lite 输出 knowledge_update → KnowledgeCache.update_window()
```

**影响分析**：
- Knowledge 更新逻辑本身不变（仍是 Flash Lite 输出中的 `knowledge_update` 字段）
- 变化的只是 FlashLite 的上下文输入：从 messages.db 的最近 N 条 → T 文件的完整 T
- **预期效果**：Knowledge 更新质量会提升，因为 FlashLite 有更完整的上下文（包含压缩历史）

---

## 6. agent.py 修改

### 删除的方法
```python
async def _get_checkpoint_summary(self, window_key: str) -> Optional[str]:
    # 整个方法删除——不再从 checkpoint_history 表读取摘要
```

### 保留/不变
- `build_system_instruction()` — 不变
- `build_contents()` — 暂时保留但不再使用（后续可删除）
- `_build_system_env()` — 不变
- `_build_tool_section()` — 不变

---

## 7. 面板配置新增

### BossLady Console 后端（models.py）

在 FlashLite 配置模型中新增：
```python
checkpoint_compress_front_ratio: float = 0.7  # 压缩前比例
checkpoint_cooldown_seconds: int = 300         # 冷却秒数
```

### BossLady Console 前端

在 CHECKPOINT 设置区域新增两个参数输入框。

### FlashLite main.py __init__

初始化 TFileManager：
```python
self._t_file_mgr = TFileManager(self._checkpoint_mgr)
```
