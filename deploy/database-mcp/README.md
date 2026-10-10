# Database MCP 内部服务

独立服务部署模板与上级 [Compose 拓扑](../README.md) 配套。MCP 对模型暴露 `fetch_schema` 和 `execute_query`，另有仅供应用/Worker 调用的内部导出端点，不发布宿主机端口。API 和 Worker 应加入 `database-mcp-internal` 网络；将 `database-egress` 的出口限制到单一企业 PostgreSQL。反向代理不得将 MCP 或内部导出端点暴露给浏览器。

## 配置

1. 将 `config/database-mcp-server.example.yaml` 复制为部署者自己的本地配置，填写固定 PostgreSQL 地址、库名、专用只读用户名及 Secret 引用；以绝对路径设置 `EM_DATABASE_MCP_CONFIG_FILE`，由 Compose 只读挂载。
2. 用部署 Secret 管理器准备 `secrets/database_readonly_password`、`secrets/database_mcp_token`。内部 token 至少 32 字符；API、Worker 和 MCP 共用该 token，企业数据库密码仅交给 MCP。该目录已被 Git 排除。
3. 将 `config/database.example.yaml` 复制为 API/Worker 本地配置，通过既有应用配置机制启用。默认配置为 `null`；主 LLM 也维持默认 `null`，配置完成后才启用查询。
4. 管理员登记的 host、port、database_name、ssl_mode、secret_ref、connection_options 必须与固定服务配置一致。更换连接参数或凭据引用后重启 MCP，并重新刷新 Schema；请求不能动态指定其他连接目标。

构建前必须设置 `EM_PYTHON_IMAGE` 和 `EM_UV_IMAGE`，使用经核验的 digest 固定引用；版本填写方式见上级 `deploy/.env.example`，不能保留占位符。独立 Compose 将这两个值显式传给 Dockerfile；缺少任一值时配置解析即失败，不隐式选择浮动基础镜像。

启动独立进程可用 `python -m enterprise_mind.database.mcp_server`，配置路径由 `EM_DATABASE_MCP_SERVER_CONFIG` 指定。Compose 使用仓库 `backend/uv.lock` 和 `--frozen` 安装依赖，非 root、只读文件系统、受限内存与进程数运行。

## 企业数据库账号

由 DBA 为受检的 Table/View 单独授权，只允许 CONNECT、必要 Schema 的 USAGE 和指定对象的 SELECT。该角色不得为 superuser、数据库/业务表 owner，不得具备 CREATEDB、CREATEROLE、REPLICATION、BYPASSRLS、任何角色成员资格、数据库 CREATE/TEMP、Schema CREATE 或表写权限。PUBLIC 赋予的权限同样计入预检；仅对账号执行 REVOKE 不能覆盖 PUBLIC，须由 DBA 在适用数据库权限方案中满足该边界。

以下是账号授权形式，具体对象和密码由 DBA 的部署流程提供：

```sql
CREATE ROLE enterprise_mind_db_readonly LOGIN NOSUPERUSER NOCREATEDB
  NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
GRANT CONNECT ON DATABASE enterprise_business TO enterprise_mind_db_readonly;
GRANT USAGE ON SCHEMA business TO enterprise_mind_db_readonly;
GRANT SELECT ON business.orders TO enterprise_mind_db_readonly;
ALTER ROLE enterprise_mind_db_readonly SET default_transaction_read_only = on;
```

MCP 每次重新校验角色和元数据，并使用只读事务、固定 `search_path=pg_catalog`、超时和取消清理；连接后不执行任何客户端提供的 SET。

## 查询内容不持久记录

DBA 应确认该服务角色及数据库代理不另存 SQL、参数或原始结果；服务和客户端不添加查询日志、不启用模型回调追踪或缓存，不打印驱动异常。部署方同时核验外部既有代理和企业监控的保存策略。

Schema 抓取仅读取固定系统目录，不抽样业务行；Schema 注释只作为不可信元数据。包含函数、`SECURITY DEFINER`、RLS 或其他复杂行为的 View/表须先经 DBA 审核，再纳入全局白名单；AST 校验不能替代对既有数据库对象内部定义的审核。`pglast==8.4` 的 GPL-3.0-or-later 分发要求在对外发布前单独核对。
