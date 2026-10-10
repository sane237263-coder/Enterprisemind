# EnterpriseMind 前端

基于 React 19.2.7、TypeScript 与 Vite 构建的知识工作台，通过 `/api/v1` 连接后端，提供会话、知识库管理与授权文档预览。

## 项目图标

统一使用 [EM 矢量图标](src/assets/em-icon.svg)：深蓝 E 与向右下错位的浅蓝 M，透明背景，字形已转为路径，任意尺寸保持清晰且不依赖系统字体。浏览器标签页、登录页、侧栏和助手欢迎页共用这一份源文件，页面通过 `BrandMark` 组件引用。后续项目图标沿用此文件，不使用截图放大版本。

## 时间显示

界面时间统一通过 `src/shared/datetime.ts` 按 `Asia/Shanghai` 显示为 `YYYY-MM-DD HH:mm:ss（北京时间）`，不跟随设备时区。API 时间字段与过期判断继续使用带时区的原始时间；日期字段、用户输入和原文内容保持原义。

## 本地运行

先按[根 README 的运行与配置](../README.md#运行与配置)准备独立数据库、Redis、MinIO 和 Secret，完成迁移、Checkpoint 与存储初始化。模型能力按需配置；各后端终端需加载自己的进程环境，并启动 Dispatcher 与 Worker。

后端在仓库根目录启动：

```powershell
$env:EM_PUBLIC_ORIGIN='http://127.0.0.1:5173'
& 'backend/.venv/Scripts/python.exe' -m enterprise_mind.cli serve --port 18000
```

本目录执行：

```powershell
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173`。开发代理将 `/api`（包括 WebSocket）和 `/health` 转到 `127.0.0.1:18000`。端口变更需同步 `vite.config.ts` 与后端允许的 Origin。生产构建输出到 `dist`。

## 本地检查

```powershell
npm run check
npm test
npm run build
```

2026-10-10 本地 TypeScript 类型检查、8 项测试和生产构建通过。Node 24 可直接运行 SSE 任意 UTF-8 分包和预览坐标回归。生产构建包含 TypeScript 检查；PDF 解析器和 Worker 单独打包，按需加载。

## 页面与交互

- 登录、权限变化提示、管理员导航；Token 仅存内存，刷新页面需重新登录。
- 会话历史、新建/重命名/删除、附件、SSE 回答、停止/继续、待确认操作、来源预览。
- 知识库/文件夹/文档管理、移动、上传、覆盖、处理状态、处理详情与重试。
- 回收站按删除批次根恢复和永久删除确认。
- 用户/部门管理、专用创建用户与重置密码表单、受控 Agent 导航入口。
- 鉴权 HTML/PDF 预览及来源高亮；HTML 禁止脚本和外链资源。
- 预览独立生成/重建、进度自动刷新、旧版本预览保留与重试；Slide 页图及 OCR/模型语义分开标注。

运行完整应用应迁移到最新版本（目前为 `0016_database_csv_export`），并启动 Dispatcher 与 Worker。功能介绍与配置入口见[项目 README](../README.md)。
