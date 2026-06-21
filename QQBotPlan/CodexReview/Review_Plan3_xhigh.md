1. **Critical：缓存策略口径冲突（隐式 vs 显式）**  
证据：[Plan_3.md:62](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3.md:62) 把显式缓存定义为“可选升级”，但 [Plan_3_2_KVCache优化.md:216](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:216) 到 [Plan_3_2_KVCache优化.md:242](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:242) 已进入 `gemini_source.py` 的显式缓存改造语义。  
影响：执行团队会对计费项（含 storage）、TTL、失效条件理解不一致。  
建议：先在总纲固定“唯一缓存基线”，再统一 3_2/3_3/3_4 的术语、验收口径和成本模型。

2. **Critical：成本监控闭环不完整，主模型有漏记风险**  
证据：面板目标含“总成本/主模型成本” [Plan_3_4_面板与成本监控.md:101](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:101)、[Plan_3_4_面板与成本监控.md:112](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:112)；但采集清单只点名 `_call_flash_lite/_call_tool_model` [Plan_3_4_面板与成本监控.md:193](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:193)。  
影响：报表可能系统性低估，优化决策会偏离真实瓶颈。  
建议：在 Plan_3_4 明确主模型“常规 provider 链路 + 直连旁路”的统一记账入口与 `call_type` 分类规范。

3. **High：主模型动态前缀注入缺少兜底路径**  
证据：[Plan_3_2_KVCache优化.md:210](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:210) 到 [Plan_3_2_KVCache优化.md:214](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:214) 仅处理“第一条且 role=user”。  
影响：`contexts` 为空或首条非 user 时，动态块可能丢失。  
建议：补充 fallback（插入新的首条 user context）并把该场景列入验收用例。

4. **High：`_ensure_kv_cache` 哈希简化建议存在误导**  
证据：[Plan_3_2_KVCache优化.md:241](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:241) 提到“hash 只基于静态部分”。  
影响：若忽略 model/tools 维度，可能复用到不兼容缓存。  
建议：文档改成“通过移除动态内容稳定 hash，不缩减 hash 维度”。

5. **High：P0 级改造缺少灰度/回滚设计**  
证据：执行顺序强调连续实施 [Plan_3.md:98](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3.md:98)；3_2/3_3 任务清单未定义 feature flag 或快速回退门 [Plan_3_2_KVCache优化.md:224](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:224)、[Plan_3_3_工具模型KVCache.md:114](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_3_工具模型KVCache.md:114)。  
影响：上线后若回复质量回退，处置窗口会拉长。  
建议：新增“按模型开关 + 一键回退到旧提示词路径”。

6. **Medium：`countTokens` 使用策略不清，可能引入额外开销**  
证据：[Plan_3_4_面板与成本监控.md:23](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:23)、[Plan_3_4_面板与成本监控.md:36](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:36)。  
影响：若在线逐次调用，会增加延迟和额外调用成本。  
建议：限定为“离线抽样校准/发布前基线测量”。

7. **Medium：模型命名口径不统一，计费映射有漂移风险**  
证据：总纲使用 `flash-lite/flash-preview` [Plan_3.md:14](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3.md:14)、[Plan_3.md:15](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3.md:15)；定价表使用 `gemini-3.*` 全名 [Plan_3_4_面板与成本监控.md:59](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:59)、[Plan_3_4_面板与成本监控.md:65](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:65)。  
建议：新增“模型别名归一化表 + 未识别模型降级策略”。

8. **Medium：`group_overrides` 定义跨文件不一致**  
证据：3_1 只覆盖 `sync_interval` [Plan_3_1_FlashLite采样优化.md:50](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_1_FlashLite采样优化.md:50)；3_4 又扩展“可关闭 FlashLite” [Plan_3_4_面板与成本监控.md:183](/C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:183)。  
建议：先冻结统一 schema，再让前后端按同一契约实现。

**需要你拍板的 3 个点**  
1. Plan_3 最终基线到底是“隐式缓存”还是“显式缓存优先”。  
2. 主模型成本采集放插件层 hook 还是 provider 层统一采集。  
3. `countTokens` 是在线实时还是离线抽样。  

**简短结论**  
Plan_3 系列方向对，但当前最大的风险是“口径不统一 + 观测闭环不完整”。先把上述 3 个拍板点固化进文档，再进入实施会更稳。