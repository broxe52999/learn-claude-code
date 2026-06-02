chore: 安装依赖并启动本地开发服务, AI=100%
1. 在 `.venv` 虚拟环境安装根目录 Python 依赖，并在 `web/` 通过 `npm ci` 安装前端依赖
2. 启动 `web` 的 Next.js 开发服务，确认 `http://localhost:3000` 可访问并正常渲染页面

feat: s01 支持流式输出和响应中断, AI=100%
1. `agents/s01_agent_loop.py` 新增 `stream_llm` 和 `InterruptController`，改用 `client.messages.stream` 边接收边输出模型响应
2. 模型响应期间支持通过 `Esc` 或 `Ctrl+C` 取消当前输出，并写入 `[Interrupted by user]` 占位，避免半截响应进入后续历史
3. 更新 `web/src/data/generated/docs.json` 和 `web/src/data/generated/versions.json`，同步文档站点的源码快照与 LOC 统计
