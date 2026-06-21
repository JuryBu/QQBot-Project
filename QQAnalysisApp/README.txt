# QQ 分析系统 (QQ Analysis App) 使用说明

## 1. 项目简介
本项目是一个基于 NapCat (OneBot 协议) 和 LLM (大语言模型) 的本地 Web 应用程序。它可以帮助你分析 QQ 好友或群聊成员的性格、兴趣爱好，并推荐聊天话题。

## 2. 快速启动
双击根目录下的 `start_app.bat` 脚本即可启动。
脚本会自动：
1. 启动后端 Python 服务 (端口 8000)。
2. 打开默认浏览器访问前端页面 (http://localhost:8000/static/index.html)。

## 3. 功能介绍
- **NapCat 管理**：在网页端直接启动/停止 NapCat，并扫码登录。
- **联系人获取**：自动获取当前登录账号的好友和群聊列表。
- **消息发送**：支持发送文本消息和 Emoji。
- **性格分析**：选择目标后，系统会爬取公开资料（名片、QQ空间、聊天记录）并让 AI 生成分析报告。
- **自定义配置**：支持配置 OpenAI 格式的 API Key 和 Base URL。

## 4. 常见问题
- **Q: 扫码显示登录失败？**
  A: 可能是二维码过期或网络问题。请尝试点击“停止 NapCat”后重新点击“启动”。如果多次失败，请检查 NapCat 目录下的日志。
- **Q: 退出登录没反应？**
  A: 尝试刷新网页。如果后台进程卡死，请手动关闭打开的 "QQAnalysisApp Backend" 终端窗口。

## 5. 项目结构
- backend/: Python FastAPI 后端代码。
- frontend/: HTML/JS/CSS 前端代码。
- start_app.bat: 启动脚本。

注意：请确保 `NapCat` 目录位置正确，本项目依赖于 `NapCat.Shell.Windows.OneKey` 目录。
