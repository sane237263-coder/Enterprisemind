# 第三方组件与素材说明

本仓库保留项目源码、锁文件、配置模板和演示资料；不将本机虚拟环境、node_modules、模型权重、Tokenizer 下载缓存、数据库备份或容器镜像打包提交。项目原创内容的授权状态见 [COPYRIGHT.md](COPYRIGHT.md)，不覆盖第三方原有许可。

## 主要依赖

以下是发布整理时核对的主要组件，不是完整的传递依赖清单，也不是组合分发合规结论。精确版本以 [Python 锁文件](backend/uv.lock)、[前端锁文件](frontend/package-lock.json)、各 Dockerfile 和 Compose 的镜像摘要为准。安装或再分发时应保留对应发行包中的 LICENSE、NOTICE 与版权文件。

| 组件 | 本项目中的用途 | 上游许可或说明 |
| --- | --- | --- |
| FastAPI、LangGraph、LangGraph Checkpoint PostgreSQL | API 与 Agent 编排、检查点 | 已安装发行包元数据为 MIT |
| python-docx、python-pptx、openpyxl | Office 解析与生成 | 已安装发行包元数据为 MIT |
| MinIO Python SDK | S3 兼容对象存储客户端 | 已安装发行包元数据为 Apache-2.0；与 MinIO 服务端许可不同 |
| PDF.js / pdfjs-dist | 浏览器 PDF 预览 | 发行包 package.json 为 Apache-2.0，见 [PDF.js](https://github.com/mozilla/pdf.js/blob/master/LICENSE) |
| PyMuPDF / MuPDF | PDF 解析、渲染与生成 | AGPL 与商业许可双授权，使用时须满足适用条款；商业授权须另行取得，见 [官方许可说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright) |
| MinIO 服务端 | 独立对象存储服务 | 当前构建脚本固定源码使用 AGPLv3，见 [固定源码的 LICENSE](https://github.com/minio/minio/blob/9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a/LICENSE) |
| Redis 8 | 队列、发布订阅与运行协调 | 官方列出 RSALv2 / SSPLv1 / AGPLv3 选项，见 [Redis 许可](https://redis.io/legal/licenses/)；以实际安装版本所选条款为准 |
| LibreOffice | 可选隔离转换与渲染服务 | 版本及组件许可见 [LibreOffice 许可说明](https://www.libreoffice.org/licenses/) |
| PostgreSQL、pgvector、React、Vite、Lucide 及其他依赖 | 数据存储、前端及图标等 | 保留各发行包许可文件；本表不代替完整依赖审计 |

提供打包镜像、二进制安装包、衍生产品或对外服务时，须核查 PyMuPDF、MinIO 和 Redis 的适用条款。项目采用“暂不授予开源许可”的展示方式，第三方依赖继续适用各自许可。

## 模型、字体与素材

- Provider 模板中的模型和服务名用于说明已有适配配置，不代表项目拥有模型权重、服务商商标或其输出的独占权利。模型和托管 API 的使用遵循各自条款；仓库不提供服务商凭据。
- Tokenizer 由 `scripts/prepare_model_tokenizers.py` 从 `config/tokenizers.yaml` 所列来源下载并核验摘要，缓存不提交。再分发下载文件时应检查对应来源许可。
- 容器中的 Noto CJK 与本机字体由运行环境提供。本仓库不复制 Windows 字体；使用其他字体生成文件或镜像时须遵循对应字体许可。
- [界面截图](assets/chat-demo.png)来自 2026-10-07 本地 Chrome，展示会话管理与流式回答功能，包含账号名 `localadmin` 和通用问答内容；不含访问令牌、登录密码或企业业务资料。
- [EM 图标](frontend/src/assets/em-icon.svg)为仓库内的项目标识；其他界面图标来自 Lucide 依赖。

本说明于 2026-10-10 整理。新增依赖、公开镜像或更改项目授权方式时，应同步更新。
