chore: 安装依赖并启动本地开发服务, AI=100%
1. 在 `.venv` 虚拟环境安装根目录 Python 依赖，并在 `web/` 通过 `npm ci` 安装前端依赖
2. 启动 `web` 的 Next.js 开发服务，确认 `http://localhost:3000` 可访问并正常渲染页面

feat: s01 支持流式输出和响应中断, AI=100%
1. `agents/s01_agent_loop.py` 新增 `stream_llm` 和 `InterruptController`，改用 `client.messages.stream` 边接收边输出模型响应
2. 模型响应期间支持通过 `Esc` 或 `Ctrl+C` 取消当前输出，并写入 `[Interrupted by user]` 占位，避免半截响应进入后续历史
3. 更新 `web/src/data/generated/docs.json` 和 `web/src/data/generated/versions.json`，同步文档站点的源码快照与 LOC 统计

docs: 补充上下文压缩设计注释, AI=100%
1. `agents/s06_context_compact.py` 补充压缩思想注释，说明有限上下文缓存、微压缩、文件读取保留和摘要压缩的设计取舍

docs: 补充任务系统设计架构注释, AI=100%
1. `agents/s07_task_system.py` 新增中文注释，整理任务持久化、依赖解除、工具暴露和代理主循环的设计思想

docs: 补充后台任务设计总结注释, AI=100%
1. `agents/s08_background_tasks.py` 在文件末尾追加设计思想和实现方法总结，说明后台线程、通知队列、结果注入和任务查询的协作方式

docs: 补充代理团队设计总结注释, AI=100%
1. `agents/s09_agent_teams.py` 在文件末尾追加设计思想和实现方法总结，说明持久化队友、JSONL 文件邮箱、团队状态和多代理协作方式
