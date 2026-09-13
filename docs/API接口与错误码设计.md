# EnterpriseMind V1 API 接口与错误码设计（确认稿）

## 1. 文档目的与边界

本文档承接《需求说明书》《系统总体架构设计》《数据库表设计》和《数据库关系与约束设计》，用于确认 EnterpriseMind V1 对前端开放的 HTTP、SSE 与 WebSocket 接口边界。

本稿只定义：

- 接口路径、方法、权限和主要输入输出。
- 高风险操作的统一确认方式。
- 异步任务、流式回答和状态恢复协议。
- 统一错误结构与稳定错误码。

本稿不包含：

- 可执行后端代码、OpenAPI 文件、SDK 或测试脚本。
- 独立搜索、仪表盘、通知、审计、审核、模型配置或 Prompt 管理接口。
- Database MCP 原始 SQL 查询接口。
- 跨知识库移动、文档版本、标签、评论、复杂 ACL、数据库行级或列级权限接口。

## 2. 通用协议

### 2.1 基础约定

| 项目 | 约定 |
|---|---|
| API 前缀 | `/api/v1` |
| 数据格式 | JSON；文件上传使用 `multipart/form-data` |
| 字段命名 | `snake_case` |
| 时间 | ISO 8601 UTC，例如 `2026-09-12T08:00:00Z` |
| 主键 | 数据库使用 `bigint`，API 统一序列化为十进制字符串，避免 JavaScript 精度丢失 |
| 分页 | 列表使用 `limit + cursor`；响应返回 `next_cursor` |
| 并发控制 | 修改聚合根时提交 `row_version`；版本不一致返回冲突 |
| 幂等 | 创建、上传、启动 Run、确认响应等可重试写请求携带 `Idempotency-Key` |
| 内容语言 | 用户可见消息按当前交互语言返回；`error.code` 始终稳定且与语言无关 |

### 2.2 身份认证

V1 使用短时效签名访问令牌，不建立数据库登录会话或 Refresh Token 表。

- 登录成功后返回访问令牌，后续请求使用 `Authorization: Bearer <token>`。
- 令牌只标识用户、签发时的 `credential_version` 和有效期；每次请求仍从数据库检查凭据版本、用户状态、角色和部门状态。
- 管理员重置密码后目标用户的 `credential_version` 递增，全部旧访问令牌立即失效。
- 用户停用后，即使旧令牌尚未过期也不能继续调用接口。
- 前端退出登录只需清除本地令牌；V1 不提供设备会话管理和单令牌撤销接口。
- SSE 使用支持请求头的流式 `fetch` 携带 Bearer Token；浏览器 WebSocket 建连后的第一条消息必须提交访问令牌，鉴权完成前服务端不订阅或发送业务事件。令牌不得写入 URL、日志或持久数据。

### 2.3 成功响应

```json
{
  "data": {},
  "meta": {
    "request_id": "运行时请求标识"
  }
}
```

列表响应的 `meta` 可增加：

```json
{
  "next_cursor": "下一页游标或 null"
}
```

### 2.4 错误响应

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "可直接展示给用户的安全说明",
    "details": {},
    "request_id": "运行时请求标识"
  }
}
```

约束：

- `details` 只包含字段错误、当前状态和允许动作等安全信息。
- 不返回堆栈、内部文件路径、MinIO Object Key、密码、Secret 引用、Prompt、无权限对象名称、SQL、SQL 摘要或数据库结果行。
- 对用户无权读取的资源统一返回 `RESOURCE_NOT_FOUND`，避免通过错误差异枚举资源。

### 2.5 常用 HTTP 状态

| 状态 | 用途 |
|---:|---|
| `200` | 查询或同步修改成功 |
| `201` | 资源创建成功 |
| `202` | 异步任务已创建，或操作正在等待确认 |
| `204` | 物理删除成功且无响应正文 |
| `400` | 请求结构无法解析 |
| `401` | 未登录、令牌无效或凭据错误 |
| `403` | 身份有效但系统角色不允许该操作 |
| `404` | 资源不存在或当前用户不可见 |
| `409` | 名称、状态、版本、确认或层级冲突 |
| `410` | 引用来源已经永久删除 |
| `413` | 上传大小超过配置限制 |
| `415` | 文件格式或 MIME 不支持 |
| `422` | 字段或业务参数不合法 |
| `500` | 未分类服务端错误 |
| `502` | Tool 或模型上游调用失败 |
| `503` | Database MCP、MinIO、Redis 或模型服务当前不可用 |
| `504` | Database MCP 或模型调用超时 |

### 2.6 核心请求字段

| 操作 | 允许提交的主要字段 |
|---|---|
| 登录 | `username`、`password` |
| 创建/修改用户 | `username`、创建时的 `password`、`role`、`department_id`、`status`、修改时的 `row_version`；普通修改不接受密码 |
| 管理员重置用户密码 | `new_password`、目标用户的 `row_version` |
| 创建/修改部门 | `name`、`status`、修改时的 `row_version` |
| 创建/修改知识库 | `name`、`type`、`description`、部门知识库的 `department_id`、修改时的 `row_version` |
| 创建/修改文件夹 | `knowledge_base_id`、`parent_id`、`name`、修改时的 `row_version` |
| 修改文档 | `name`、`description`、同知识库内的 `folder_id`、`row_version` |
| 上传/覆盖 | 文件、目标知识库、`folder_id`、显示名称、描述；覆盖还必须提交目标文档 `row_version` |
| 创建消息 | `content_text` 或 `content_parts`，至少一项非空 |
| Pending Action 响应 | `decision`；消歧时只能增加一个服务端候选目标 ID |
| 创建/修改 Memory | `content`、`category`、修改时的 `row_version` |
| 生成物写入知识库 | `knowledge_base_id`、`folder_id`、文档名称和描述 |
| Database MCP 数据源 | `name`、主机、端口、数据库名、SSL 模式、Secret 引用、状态和修改时的 `row_version` |
| 关系策略 | `schema_name`、`relation_name`、`enabled`、部门 ID 集合和更新时的 `row_version` |

未在本表或具体接口中列出的系统字段不得由客户端写入。服务端必须使用白名单请求模型，不能把请求 JSON 直接映射为数据库更新。

## 3. 权限与资源可见性

### 3.1 权限规则

| 资源 | 管理员 | 普通用户 |
|---|---|---|
| 用户、部门 | 管理 | 不可访问管理接口 |
| 公共知识库 | 管理知识库及内部内容 | 读取并管理文档、文件夹 |
| 本部门知识库 | 管理知识库及内部内容 | 读取并管理文档、文件夹 |
| 其他部门知识库 | 管理 | 不可见 |
| 会话、Memory、生成物 | 仅访问自己的个人数据 | 仅访问自己的个人数据 |
| Database MCP 配置与白名单 | 管理 | 不可访问管理接口 |
| Database MCP 查询 | 全部已启用全局白名单关系 | 已启用全局白名单与本部门授权的交集 |

管理员角色不自动获得其他用户的会话正文、Memory 或个人生成物访问权。

### 3.2 权限检查时点

以下时点都必须重新读取当前权限，不能只使用令牌、LLM 参数或历史 Checkpoint：

1. 接口接收请求时。
2. 创建 Pending Action 前。
3. 用户接受确认后、实际执行操作前。
4. Agent Run 恢复时。
5. RAG 检索和 Database MCP Schema 暴露、SQL 执行前。

## 4. 登录、用户与部门接口

### 4.1 登录

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `POST` | `/api/v1/auth/login` | 公开 | 用户名和密码登录，返回访问令牌与当前用户摘要 |
| `GET` | `/api/v1/auth/me` | 已登录 | 返回当前用户、角色、部门和状态 |

登录接口不说明用户名是否存在；用户名不存在、密码错误、账号已停用或普通用户所属部门已停用，统一返回 `AUTH_INVALID_CREDENTIALS`。`ACCOUNT_DISABLED` 和 `DEPARTMENT_DISABLED` 只用于已建立身份后发现状态变化的请求。

### 4.2 用户管理

首个管理员由全新系统的一次性初始化任务创建，不提供公开注册、首管理员创建或初始化密码 HTTP 接口。初始化完成后的账号管理只允许已认证管理员使用以下接口。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/admin/users` | 管理员 | 按状态、部门分页列出用户 |
| `POST` | `/api/v1/admin/users` | 管理员 | 创建本地用户 |
| `GET` | `/api/v1/admin/users/{user_id}` | 管理员 | 查看用户基本信息 |
| `PATCH` | `/api/v1/admin/users/{user_id}` | 管理员 | 修改用户名、角色、部门或状态，必须提交 `row_version` |
| `POST` | `/api/v1/admin/users/{user_id}/password-reset` | 管理员 | 重置目标用户密码，必须提交新密码和 `row_version`；成功后旧访问令牌全部失效 |

密码重置接口对管理员账号和普通用户账号使用相同规则。新密码只存在于 HTTPS 请求和哈希计算所需内存，不写入响应、日志、会话、Tool、Pending Action 或业务表；接口只返回更新后的用户摘要与 `row_version`。校验、并发或写入失败时原密码和凭据版本保持不变。

V1 不提供用户删除、普通用户自助修改密码、忘记密码、邮件找回或重置链接接口；停用使用 `status = disabled`。

### 4.3 部门管理

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/admin/departments` | 管理员 | 分页列出部门 |
| `POST` | `/api/v1/admin/departments` | 管理员 | 创建平级部门 |
| `GET` | `/api/v1/admin/departments/{department_id}` | 管理员 | 查看部门 |
| `PATCH` | `/api/v1/admin/departments/{department_id}` | 管理员 | 修改名称或状态，必须提交 `row_version` |
| `DELETE` | `/api/v1/admin/departments/{department_id}` | 管理员 | 仅在无用户、无知识库引用时物理删除 |

部门没有父级字段，也没有树形或部门迁移接口。Database MCP 部门授权是依附记录，不额外阻止部门删除。

## 5. 知识库与文件夹接口

### 5.1 知识库

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/knowledge-bases` | 已登录 | 返回当前用户可访问的知识库；管理员可查看全部 |
| `POST` | `/api/v1/knowledge-bases` | 管理员 | 创建公共或部门知识库 |
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 可访问该库 | 查看名称、类型、描述、部门和由 `deleted_at` 派生的 `active` / `recycle_bin` 有效状态；不新增知识库启停状态 |
| `PATCH` | `/api/v1/knowledge-bases/{knowledge_base_id}` | 管理员 | 修改名称、描述或部门归属，必须提交 `row_version` |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/deletion-requests` | 管理员 | 创建删除确认，不直接删除 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/restore` | 管理员 | 从回收站恢复，无需二次确认 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/purge-requests` | 管理员 | 创建永久删除确认 |

`PATCH` 只允许部门知识库修改 `department_id`；不提供将公共知识库与部门知识库相互转换的接口。

### 5.2 文件夹

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/folders` | 可访问该库 | 返回当前库的文件夹树或指定父级直接子项 |
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/folders` | 可管理该库内容 | 创建文件夹 |
| `GET` | `/api/v1/folders/{folder_id}` | 可访问所属库 | 查看文件夹及路径 |
| `PATCH` | `/api/v1/folders/{folder_id}` | 可管理所属库内容 | 重命名或修改 `parent_id`，必须提交 `row_version` |
| `POST` | `/api/v1/folders/{folder_id}/deletion-requests` | 可管理所属库内容 | 空文件夹直接进入回收站；非空文件夹返回待确认动作 |
| `POST` | `/api/v1/folders/{folder_id}/restore` | 可管理所属库内容 | 恢复本批次文件夹及子项 |
| `POST` | `/api/v1/folders/{folder_id}/purge-requests` | 可管理所属库内容 | 创建永久删除确认 |

移动接口只修改同一 `knowledge_base_id` 内的 `parent_id`。请求不得提交新的知识库 ID，也不提供跨知识库迁移接口。

## 6. 文档、上传、预览与处理接口

### 6.1 文档管理

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/knowledge-bases/{knowledge_base_id}/documents` | 可访问该库 | 按文件夹、处理状态或格式分页列出文档，不提供独立全文搜索 |
| `GET` | `/api/v1/documents/{document_id}` | 可访问所属库 | 查看文档元数据和当前处理状态 |
| `PATCH` | `/api/v1/documents/{document_id}` | 可管理所属库内容 | 修改名称、描述或同库 `folder_id`，必须提交 `row_version` |
| `POST` | `/api/v1/documents/{document_id}/deletion-requests` | 可管理所属库内容 | 创建删除确认 |
| `POST` | `/api/v1/documents/{document_id}/restore` | 可管理所属库内容 | 从回收站恢复，无需二次确认 |
| `POST` | `/api/v1/documents/{document_id}/purge-requests` | 可管理所属库内容 | 创建永久删除确认 |

文档接口不暴露修改文件类型、上传时间、上传人、Object Key、处理状态、Chunk 或 Embedding 的字段。

### 6.2 上传与覆盖

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `POST` | `/api/v1/knowledge-bases/{knowledge_base_id}/document-uploads` | 可管理该库内容 | 单文件或批量上传；每个成功文件创建独立任务 |
| `POST` | `/api/v1/documents/{document_id}/overwrite-requests` | 可管理所属库内容 | 上传候选新文件并创建覆盖确认，必须绑定 `row_version` |

上传请求允许提交 `folder_id`、显示名称、描述和一个或多个文件。批量响应逐文件返回：

- `accepted`：文档与处理任务 ID。
- `rejected`：稳定错误码与安全消息。
- `name_conflict`：现有文档 ID 与名称，用户可选择改名或发起覆盖请求。

允许格式严格限定为 PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、CSV、Markdown、TXT、PNG、JPG/JPEG 和 WEBP。

覆盖确认前，新文件只能处于隔离的暂存状态，不参与 RAG。确认后创建新处理代次；处理期间继续使用旧活跃代次，新代次成功后原子切换，失败时继续使用旧内容。

Agent 消息携带文件时复用同一上传服务和权限规则，不建立绕过文件校验、同名检查或覆盖确认的独立上传路径。

### 6.3 处理状态和失败重试

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/documents/{document_id}/processing` | 可访问所属库 | 返回文档状态、当前阶段、阶段进度和失败单元摘要 |
| `POST` | `/api/v1/documents/{document_id}/processing/retry` | 可管理所属库内容 | 只重试失败部分，无需二次确认 |
| `WS` | `/api/v1/ws/document-processing` | 已登录 | 推送当前用户可访问文档的处理状态变化 |

WebSocket 只推送 `document_id`、状态、阶段、阶段内进度和安全错误摘要。断线重连后以前述 GET 接口重新读取 PostgreSQL 当前状态，不依赖补发完整事件历史。

### 6.4 预览与引用定位

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/documents/{document_id}/preview` | 可访问所属库 | 返回预览类型、短时效签名地址和可用来源单元 |
| `GET` | `/api/v1/answer-sources/{answer_source_id}/preview` | 可访问所属会话及来源 | 返回来源标题、定位结构、短引用和高亮信息 |

Assistant 消息返回的 `sources` 必须按来源类型包含：

- 知识库来源：知识库名称、文档名称、页码/Slide/Sheet/段落或单元格定位、采用的原文片段和来源可用状态。
- 数据库来源：数据源与数据库名称、表/View、查询时间、查询条件或统计口径、`has_more` 和来源可用状态。

知识库来源创建时必须同时绑定实际采用的 Chunk 和来源锚点；一个结论由多个位置共同支撑时返回多个来源项。各文件格式的定位 JSON 和高亮坐标约定见《内容分块、来源锚点与索引结构设计》。

引用来源已永久删除时返回 `SOURCE_UNAVAILABLE`，只保留回答时的最小标题和定位快照，不返回已删除全文。

## 7. 会话、Agent Run 与确认接口

### 7.1 会话与消息

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/conversations` | 已登录 | 分页列出自己的会话 |
| `POST` | `/api/v1/conversations` | 已登录 | 新建会话 |
| `GET` | `/api/v1/conversations/{conversation_id}` | 会话所有者 | 查看会话摘要 |
| `PATCH` | `/api/v1/conversations/{conversation_id}` | 会话所有者 | 重命名，必须提交 `row_version` |
| `POST` | `/api/v1/conversations/{conversation_id}/deletion-requests` | 会话所有者 | 创建永久删除确认 |
| `GET` | `/api/v1/conversations/{conversation_id}/messages` | 会话所有者 | 按序分页读取用户和 Assistant 消息 |
| `POST` | `/api/v1/conversations/{conversation_id}/messages` | 会话所有者 | 创建用户消息并启动一个 Agent Run |

消息历史只包含 `user` 和 `assistant`。Tool 临时结果和内部推理不作为消息返回。

`content_parts` 只允许已登记的附件、结构化表格和受控 `client_action`。V1 的客户端动作只补足不能让密码进入模型上下文的本地用户创建和管理员密码重置：`action = open_admin_user_create` 可携带服务端校验后的用户名、角色、部门 ID 和状态预填值；`action = open_admin_user_password_reset` 只能携带已经重新鉴权确认的目标用户 ID、显示名和 `row_version`。两者都不得携带密码、Token、任意 URL 或自由路由。只有管理员可收到并执行这些动作，最终提交仍由管理页面直接调用既有专用 API。

### 7.2 Agent Run

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/agent-runs/{run_id}` | 所属会话所有者 | 返回 Run 状态、当前高层步骤和计数 |
| `GET` | `/api/v1/agent-runs/{run_id}/events` | 所属会话所有者 | SSE 输出步骤、回答增量、确认请求和终态 |
| `POST` | `/api/v1/agent-runs/{run_id}/stop` | 所属会话所有者 | 请求停止，状态转为 `cancelled` |
| `POST` | `/api/v1/agent-runs/{run_id}/resume` | 所属会话所有者 | 从可恢复 Checkpoint 继续并重新鉴权 |

SSE 事件类型限定为：

- `run.status`
- `step.updated`
- `answer.delta`
- `pending_action.required`
- `run.completed`
- `run.failed`
- `run.cancelled`

事件不包含模型内部推理、Prompt、SQL、数据库结果行或无权限 Schema。涉及 Database MCP 的 Run 恢复后重新生成、校验和执行 SQL。

### 7.3 Pending Action

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/pending-actions/{pending_action_id}` | 绑定用户 | 查看确认问题或候选对象 |
| `POST` | `/api/v1/pending-actions/{pending_action_id}/responses` | 绑定用户 | 接受、拒绝确认，或提交消歧选择 |

Agent 发起的记录使用 `origin = agent` 并绑定 Run；文档、文件夹、知识库、会话和生成物页面的 `*-requests` 使用 `origin = api`，绑定确定的 `operation_name` 和 HTTP `Idempotency-Key`，不创建虚假的会话、消息或 Agent Run。API 来源已有路径中的精确资源 ID，不用于对象消歧。

响应体只允许：

- 确认：`decision = accepted | rejected`。
- 消歧：提交服务端返回的一个候选目标 ID。

创建 Pending Action 和响应 Pending Action 使用彼此独立的 `Idempotency-Key`。第一次响应时将响应键和决策写入 `pending_actions.response`；同一响应键重试返回原结果，其他键不能改写已经解析的记录。

接受后执行前必须再次核对用户、权限、有效期、绑定参数、目标 ID 和 `row_version`。任何变化都使原确认失效，不得静默改用新参数。

V1 必须使用 Pending Action 的高风险操作只有：

1. 删除文档或知识库。
2. 删除非空文件夹。
3. 删除会话。
4. 永久删除回收站内容。
5. 覆盖同名文档。
6. 将生成内容正式写入知识库。

重命名、同库移动、恢复、普通上传和失败重试不创建确认。

## 8. Memory 与生成物接口

### 8.1 Memory

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/memories` | 已登录 | 按分类分页读取自己的 Memory |
| `POST` | `/api/v1/memories` | 已登录 | 主动新增 Memory，写入前经过 Memory Guard |
| `PATCH` | `/api/v1/memories/{memory_id}` | 所有者 | 修改内容或分类，必须提交 `row_version` |
| `DELETE` | `/api/v1/memories/{memory_id}` | 所有者 | 物理删除，不保留正文副本 |

发生同类冲突时更新现有 Memory，并在响应中返回被更新的 ID，不创建重复记录。系统自动提取只从本轮用户消息中提取长期稳定的偏好、工作上下文或持续任务上下文；候选须经过与主动新增相同的 Memory Guard、用户内去重和冲突更新，并与成功保存本轮回答在同一终态事务中落库。候选提取或 Guard 失败时以空更新继续，不改变回答结果；最终回答事务失败时自动 Memory 也不提交。自动提取不得从 RAG、Database MCP、Tool 结果或 Assistant 生成内容中复制企业事实。

### 8.2 生成文件和导出

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/generated-artifacts/{artifact_id}` | 所有者 | 查看生成状态、格式、来源说明和下载信息 |
| `POST` | `/api/v1/generated-artifacts/{artifact_id}/save-requests` | 所有者且可管理目标库 | 创建“写入知识库”确认 |

生成或导出由 Agent Tool 发起，不提供脱离 Agent 的通用内容生成接口。写入知识库前只保存生成物；用户确认后才创建正式文档并进入异步处理链。

Agent 生成能力严格限定为单/多文档总结对比、知识归纳、普通内容生成、中英互译、文档或指定章节翻译、翻译后总结、明确请求的柱状图/折线图/饼图/表格，以及既有数据库导出。文档类输出为 DOCX、PDF 或 Markdown；表格和数据库导出为 XLSX 或 CSV；图表导出为 PNG、PDF、XLSX 或 CSV。来源型生成物必须保留原始知识库引用或数据库来源说明。

图表只在用户明确要求时生成。Database MCP 导出的 CSV/XLSX 可以保存结果文件，但生成物来源说明不得包含 SQL 或结果行副本。

## 9. Database MCP 管理接口

### 9.1 数据源和 Schema

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/admin/database-mcp/source` | 管理员 | 查看唯一数据源的非密钥配置和状态 |
| `PUT` | `/api/v1/admin/database-mcp/source` | 管理员 | 创建唯一 PostgreSQL 数据源配置；已存在时不得创建第二条记录 |
| `PATCH` | `/api/v1/admin/database-mcp/source` | 管理员 | 修改唯一数据源配置或状态，必须提交 `row_version` |
| `POST` | `/api/v1/admin/database-mcp/schema-refresh` | 管理员 | 创建新 Schema 快照刷新任务 |
| `GET` | `/api/v1/admin/database-mcp/schema-refreshes/{schema_snapshot_id}` | 管理员 | 查看本次刷新状态和安全失败摘要 |
| `GET` | `/api/v1/admin/database-mcp/schema` | 管理员 | 查看当前 active 快照中的表、View、字段、主外键和注释 |

V1 不提供第二个数据源创建接口，也不返回密码、Secret 内容或 EnterpriseMind 自身数据库信息。

### 9.2 全局白名单和部门授权

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/admin/database-mcp/relation-policies` | 管理员 | 查看当前 Schema 关系的全局启用状态和部门授权 |
| `PUT` | `/api/v1/admin/database-mcp/relation-policies` | 管理员 | 按 Schema 名和关系名创建或更新策略，并原子替换部门 ID 集合；更新时必须提交 `row_version` |

策略只能引用当前 Schema 快照中实际存在的表或 View。V1 不提供列级、行级权限或每关系行数上限；普通查询统一采用系统全局默认最多 100 行。

### 9.3 查询边界

- 不提供 `/query`、`/sql` 或任意 SQL 调试接口。
- 业务数据库查询只能由 Agent 通过 SQL Guard 和 Database MCP 发起。
- SQL Guard 每轮重新生成并校验 SQL，MCP Server 再次校验只读性、对象范围、超时和行数限制。
- 关键查询条件存在歧义时，Agent Run 进入 `waiting_confirmation` 并创建消歧 Pending Action；用户选择前不得猜测条件或执行查询。
- SQL、SQL 摘要和结果行不写入 PostgreSQL、Redis 持久数据、Checkpoint、日志、指标或普通 Tool Call 字段。
- 最终回答只保存回答当时的数据源名称、数据库名称、表/View、查询时间、条件摘要、统计口径和 `has_more`。

## 10. 回收站接口约定

知识库、文件夹和文档列表接口通过 `scope = active | recycle_bin` 选择正常数据或回收站数据，不建立独立的第二套资源模型。

规则：

- 进入回收站立即停止正常访问和 RAG。
- 回收记录返回 `deleted_at` 和 `purge_after`。
- 30 天内允许恢复；恢复遇到名称冲突时返回 `NAME_CONFLICT`，不自动改名。
- 到期自动永久清理不依赖前端接口。
- 用户主动永久删除必须先调用资源对应的 `purge-requests`，再通过 Pending Action 接受确认。

## 11. 错误码

### 11.1 通用、认证与权限

| HTTP | 错误码 | 含义 |
|---:|---|---|
| `400` | `INVALID_REQUEST` | 请求体、查询参数或文件表单无法解析 |
| `401` | `AUTH_REQUIRED` | 缺少或无法验证访问令牌 |
| `401` | `AUTH_INVALID_CREDENTIALS` | 用户名或密码错误 |
| `403` | `ACCOUNT_DISABLED` | 当前用户已停用 |
| `403` | `DEPARTMENT_DISABLED` | 普通用户所属部门已停用 |
| `403` | `PERMISSION_DENIED` | 当前角色不允许执行该类操作 |
| `404` | `RESOURCE_NOT_FOUND` | 资源不存在或对当前用户不可见 |
| `409` | `ROW_VERSION_CONFLICT` | 提交版本与当前资源版本不一致 |
| `422` | `VALIDATION_ERROR` | 字段格式或组合不符合约束 |
| `500` | `INTERNAL_ERROR` | 未分类服务端错误，响应不暴露内部细节 |

### 11.2 组织、知识库和文档

| HTTP | 错误码 | 含义 |
|---:|---|---|
| `409` | `NAME_CONFLICT` | 规范化后的同范围名称已存在 |
| `409` | `DEPARTMENT_IN_USE` | 部门仍被用户或知识库引用 |
| `409` | `FOLDER_CYCLE` | 移动后会形成目录环 |
| `422` | `CROSS_KNOWLEDGE_BASE_MOVE_FORBIDDEN` | 请求尝试跨知识库移动文件夹或文档 |
| `409` | `DOCUMENT_NAME_CONFLICT` | 同目录文档同名，需要改名或发起覆盖 |
| `409` | `DOCUMENT_PROCESSING_IN_PROGRESS` | 文档已有待处理或处理中的最新候选代次，不能再次覆盖 |
| `413` | `FILE_TOO_LARGE` | 文件超过配置的上传限制 |
| `415` | `UNSUPPORTED_FILE_TYPE` | 扩展名或 MIME 不在 V1 支持范围 |
| `422` | `FILE_CONTENT_MISMATCH` | 文件扩展名、MIME 与内容检测不一致 |
| `409` | `PROCESSING_NOT_RETRYABLE` | 当前文档没有可重试失败单元或状态不允许重试 |
| `409` | `RECYCLE_STATE_CONFLICT` | 资源当前不处于请求所需的回收站状态 |
| `410` | `SOURCE_UNAVAILABLE` | 回答引用的底层来源已永久删除 |

### 11.3 Agent、确认与生成物

| HTTP | 错误码 | 含义 |
|---:|---|---|
| `409` | `RUN_NOT_STOPPABLE` | Run 已进入不可停止终态 |
| `409` | `RUN_NOT_RESUMABLE` | Run 没有可恢复 Checkpoint 或状态不允许恢复 |
| `409` | `RUN_LIMIT_REACHED` | 达到配置的 Tool Call、Replan 或 Retry 上限 |
| `409` | `PENDING_ACTION_EXPIRED` | 确认或消歧请求已过期 |
| `409` | `PENDING_ACTION_RESOLVED` | Pending Action 已被处理，不能重复响应 |
| `409` | `PENDING_ACTION_BINDING_CHANGED` | 权限、参数、目标或版本变化，原确认失效 |
| `422` | `PENDING_ACTION_RESPONSE_INVALID` | 决策值或候选目标不合法 |
| `409` | `OBJECT_AMBIGUOUS` | 存在多个高相关候选，需要用户选择 |
| `409` | `ARTIFACT_NOT_READY` | 生成文件尚未成功完成 |
| `502` | `TOOL_EXECUTION_FAILED` | Tool 最终失败；部分失败信息通过 Run 结果说明 |

### 11.4 Database MCP 与外部依赖

| HTTP | 错误码 | 含义 |
|---:|---|---|
| `503` | `DATABASE_SOURCE_UNAVAILABLE` | 唯一业务数据源未配置、停用或连接失败 |
| `409` | `DATABASE_SOURCE_ALREADY_CONFIGURED` | 已存在唯一数据源记录，不能创建第二条配置 |
| `503` | `SCHEMA_SNAPSHOT_UNAVAILABLE` | 没有可用的 active Schema 快照 |
| `403` | `DATABASE_RELATION_FORBIDDEN` | 请求关系不在当前权限交集中；不返回无权限 Schema 详情 |
| `422` | `DATABASE_QUERY_REJECTED` | SQL Guard 拒绝只读性、对象、函数或语句结构 |
| `504` | `DATABASE_QUERY_TIMEOUT` | 企业数据库查询超过执行时限 |
| `502` | `MODEL_PROVIDER_FAILED` | LLM、Embedding、Rerank、Vision 或 OCR 上游最终失败 |
| `503` | `STORAGE_UNAVAILABLE` | MinIO 当前不可用 |
| `503` | `QUEUE_UNAVAILABLE` | Redis 或任务投递当前不可用 |

`has_more = true`、可激活的 `partial_success` 和部分 Tool 失败后继续回答都属于业务结果，不作为 HTTP 错误；响应应同时说明缺失数据源和结论局限。

## 12. 接口状态与事务要求

### 12.1 写操作

- 所有资源修改在领域服务中完成权限、参数、名称唯一性和 `row_version` 校验。
- 路由层不直接操作数据库、MinIO 或企业业务数据库。
- 上传、覆盖、删除、恢复、永久清理和生成内容写入知识库必须遵守既有数据库事务与 Outbox 边界。
- 同一个 `Idempotency-Key` 重试只能返回原结果，不能重复创建资源、任务或 Pending Action。

### 12.2 异步操作

- 文档上传、覆盖处理、失败重试、Schema 刷新和生成内容写入知识库返回 `202`。
- 返回体至少包含适用的 `document_id`、`processing_task_id`、`run_id` 或 `pending_action_id`。
- PostgreSQL 是状态真实来源；SSE/WebSocket 只承担实时通知。

### 12.3 高风险操作

- 高风险资源接口不提供可绕过确认的直接 `DELETE` 或立即执行参数。
- 创建 Pending Action 与保存目标快照、参数摘要、用户和资源版本在同一事务完成。
- 接受确认时锁定 Pending Action 并重新校验；确认成功后才执行或投递实际操作。

## 13. 明确不提供的接口

V1 不提供：

- 独立全文搜索、关键词搜索或向量搜索接口。
- 原始 SQL、SQL 历史、查询审计或查询结果历史接口。
- 操作日志、安全事件、登录设备或会话管理接口。
- 第二业务数据库、多租户、个人 ACL、文档 ACL、文件夹 ACL、跨部门分享接口。
- 数据库行级、列级授权和复杂 BI 接口。
- 跨知识库移动、文档版本、标签、评论批注或在线 Office 编辑接口。
- 模型配置、Prompt 管理、A/B Test、通知中心、首页仪表盘或审核接口。

## 14. 实现落地顺序

实现阶段依次产出：

1. 请求与响应 Pydantic Schema 详细字段表。
2. OpenAPI 文档。
3. 路由、领域服务与 Repository 实现。
4. API 单元测试和关键流程集成测试。

上述实现产物以本确认稿为边界生成；生成代码、OpenAPI 和测试时不得增加未设计接口。
