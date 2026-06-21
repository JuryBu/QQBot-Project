1. `P0` 内容安全策略在设计上被全量关闭，存在合规与风控阻断风险。证据：[`Plan_1_data.md:121`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_data.md:121 )、[`Plan_1_data.md:125`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_data.md:125 )。  
建议：至少按场景分级开启 `safetySettings`，并加一层输出审计/拒答策略，而不是全 OFF。

2. `P0` 控制台是高权限控制平面，但认证授权模型不完整，且允许直接改配置/数据库/重启进程。证据：[`Plan_1_webui.md:229`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_webui.md:229 )、[`Plan_1_webui.md:425`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_webui.md:425 )、[`Plan_1_webui.md:492`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_webui.md:492 )、[`Plan_1_webui.md:493`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_webui.md:493 )、[`Plan_1_webui.md:495`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_webui.md:495 )。  
建议：补齐最小安全基线（本地绑定/反代鉴权、RBAC、CSRF、防重放、操作审计）。

3. `P0` 启停脚本会强杀系统中所有 `python.exe/node.exe`，会误伤无关进程。证据：[`Plan_1_webui.md:270`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_webui.md:270 )、[`Plan_1_webui.md:271`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_webui.md:271 )。  
建议：改为 PID 文件或进程组定向停止。

4. `P1` 顶层架构文档自相矛盾：一处要求“状态机+语义混合”，另一处明确“状态机全部废弃”。证据：[`Plan_1.md:54`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1.md:54 )、[`Plan_1.md:58`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1.md:58 )、[`Plan_1_gaps.md:170`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_gaps.md:170 )、[`Plan_1_gaps.md:172`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_gaps.md:172 )。  
建议：建立唯一“权威架构基线”(ADR)，其余文档只允许引用，不允许并列定义。

5. `P1` T 文件一致性策略在损坏与并发场景下仍有丢消息风险。证据：[`Plan_2_CP_T_file.md:216`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_T_file.md:216 )、[`Plan_2_CP_T_file.md:221`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_T_file.md:221 )、[`Plan_2_CP_integration.md:152`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_integration.md:152 )、[`Plan_2_CP_integration.md:213`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_integration.md:213 )。  
建议：用消息 ID/指纹做增量对齐，损坏后支持“重建”而非“空 T 回退”，并明确跨进程锁策略。

6. `P1` 并发方案存在文档冲突：一份主张“压缩期间不阻塞”，另一份主张“整段加锁”。证据：[`Plan_2_CP_P2_3_并发安全.md:10`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_P2_3_并发安全.md:10 )、[`Plan_2_CP_P2_3_并发安全.md:12`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_P2_3_并发安全.md:12 )、[`Plan_2_CP_缺漏_P2优化.md:233`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_缺漏_P2优化.md:233 )。  
建议：选定单一策略并写入正式决策记录，避免实现漂移。

7. `P1` 窗口键命名规范不一致（`PrivateMessage` vs `FriendMessage`），会引发数据分区错位。证据：[`Plan_1_memory.md:79`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1_memory.md:79 )、[`Plan_2_CP_architecture.md:90`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_architecture.md:90 )、[`Plan_2_CP_T_file.md:14`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_2_CP_T_file.md:14 )。  
建议：统一 canonical key（含迁移脚本和兼容映射）。

8. `P1` 任务治理可追溯性不足：Stage 标记已完成，但同节大量子任务仍未勾选。证据：[`Task_3.md:20`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Task_3.md:20 ) + [`Task_3.md:29`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Task_3.md:29 )；[`Task_3.md:254`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Task_3.md:254 )。  
建议：定义 DoD（代码、测试、截图、回归）并自动校验状态一致性。

9. `P2` 测试策略偏“源码字符串匹配”，行为验证不足且路径硬编码，不利于 CI 与复现。证据：[`test_stage1_flashlite.py:5`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/test_stage1_flashlite.py:5 )、[`test_stage1_flashlite.py:27`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/test_stage1_flashlite.py:27 )、[`test_stage3_main_model.py:4`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/test_stage3_main_model.py:4 )、[`test_stage3_main_model.py:21`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/test_stage3_main_model.py:21 )。  
建议：增加黑盒集成测试（真实请求/响应/持久化副作用校验）。

10. `P2` 成本监控方案有精度漂移风险：价格与汇率依赖人工维护，且校验口径前后不统一。证据：[`Plan_3_4_面板与成本监控.md:55`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_%E9%9D%A2%E6%9D%BF%E4%B8%8E%E6%88%90%E6%9C%AC%E7%9B%91%E6%8E%A7.md:55 )、[`Plan_3_4_面板与成本监控.md:202`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_%E9%9D%A2%E6%9D%BF%E4%B8%8E%E6%88%90%E6%9C%AC%E7%9B%91%E6%8E%A7.md:202 )、[`Plan_3_4_面板与成本监控.md:220`]( /c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_%E9%9D%A2%E6%9D%BF%E4%B8%8E%E6%88%90%E6%9C%AC%E7%9B%91%E6%8E%A7.md:220 )。  
建议：价格与汇率加版本时间戳、自动更新源、每日对账任务。

开放问题/假设：
1. 控制台是否只允许本机访问？若未来要远程访问，当前安全模型需要整体升级。  
2. 运行形态是否永远单进程？若多 worker/多实例，T 文件锁方案要重构为跨进程锁。  
3. 对聊天数据与用户画像是否有明确合规边界（存储期限、删除请求、加密要求）？

总体结论：体系思路成熟（分层、阶段化、可追溯），但当前阻塞上线的是“控制面安全边界 + T 文件一致性 + 文档/任务治理一致性”。先清 P0/P1，再做功能扩展会更稳，主人。