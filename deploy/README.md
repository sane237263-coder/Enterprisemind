# EnterpriseMind Linux / Compose / HTTPS 部署

本目录提供一个固定 Compose 项目、单独迁移步骤、专用 Secret 引用、内部 Database MCP、API/Dispatcher/Worker、MinIO、Redis、pgvector PostgreSQL 和唯一 HTTPS 边缘入口。数据库、Redis、MinIO 和 MCP 没有宿主机端口；只有 `edge` 发布 HTTP 重定向和 HTTPS。

部署前准备获批准的 Linux 主机、域名、TLS 证书、企业数据库连接和生产 Secret，再按下列步骤完成配置、预检与启动。

## 发布目录

在获批准的 Linux 主机上为每个发布版本建立独立目录。配置和 Secret 必须离开 Git 工作区：

```text
/srv/enterprise-mind/releases/<release-id>/
  config/application.yaml
  config/database.yaml
  config/database-mcp-server.yaml
  config/database-ca.pem
  config/providers.yaml
  config/rag.yaml
  config/tokenizers/...
  secrets/
  tls/fullchain.pem
  tls/privkey.pem
  release.env
```

Database MCP 的企业数据库连接使用 `verify-full`。部署者须从 DBA 或获批准的证书分发渠道取得该数据库的公开 CA 信任链，保存为 `config/database-ca.pem`；不要放入 CA 私钥，也不要用网页 HTTPS 的叶证书替代数据库 CA。该公开文件沿用 `/run/config:ro` 挂载，Compose 仅为 `database-mcp` 设置 `PGSSLROOTCERT=/run/config/database-ca.pem`，文件须允许 UID 65532 读取。

锁定的 [asyncpg 0.31.0 实现](https://github.com/MagicStack/asyncpg/blob/v0.31.0/asyncpg/connect_utils.py)在 `verify-full` 下读取 `PGSSLROOTCERT` 或用户目录中的 `root.crt`；容器不会自动继承宿主的信任文件。预检会检查 CA 文件的挂载范围、OpenSSL 可加载性、至少包含一张 CA 证书、SHA-256 和运行用户可读性，并从实际 YAML 检查 `sslmode: verify-full`、`target_session_attrs: read-only`。缺少或损坏 CA 时应补齐批准材料，不能降低 TLS 校验级别。CA 加载成功只证明本地信任材料可用，实际服务端证书链、主机名匹配、有效期及撤销仍需目标连接验证；MCP 的 TCP 存活检查不能代替这项验证。

将 `deploy/config/application.production.yaml` 复制为发布目录的 `config/application.yaml`，并保持 `api.workers: 1`。当前查询页和导出上下文在 API 进程内，单 Worker 是部署的必要条件。将 `deploy/config/database.yaml` 和按已批准模型资料填写的 `config/providers.example.yaml` 分别复制为发布目录的 `config/database.yaml`、`config/providers.yaml`。RAG 配置必须与所选 Provider 配套：示例 Provider 启用了重排，对应 `config/rag.models.example.yaml`，复制后命名为 `config/rag.yaml`；按所选模型和业务资料设置匹配的重排阈值。不要将它与重排模型键为空的默认 `config/rag.yaml` 混用。将 `deploy/config/database-mcp-server.yaml` 复制到发布目录后填写获批准的企业 PostgreSQL 地址和 DBA 提供的只读账号。该账号必须保留 `verify-full`、`target_session_attrs: read-only` 和项目既有的角色/对象权限预检。Provider 配置必须明确选择至少 LLM 和 Embedding，预检会拒绝 `null`；Secret 只通过 `*_FILE` 挂载，不能把 Key 写入 YAML。

本机 Provider 示例中的 `../.local/tokenizers/...` 路径供本地开发使用。发布时将匹配的 Tokenizer 文件放入发布目录的 `config/tokenizers/embedding/tokenizer.json` 和 `config/tokenizers/rerank/tokenizer.json`，并只在发布副本 `config/providers.yaml` 中把两处 `tokenizer_file` 改为 `tokenizers/embedding/tokenizer.json`、`tokenizers/rerank/tokenizer.json`。保留与文件对应的 `tokenizer_sha256`，不要以修改摘要的方式绕过错误。容器将整个发布配置目录挂载为 `/run/config:ro`；相对路径以 Provider YAML 所在目录为基准，也可使用 `/run/config/tokenizers/...` 绝对路径，宿主机的其他绝对路径在容器中不可用。预检通过 YAML 解析路径，并拒绝 `..`、越界符号链接、缺失文件或摘要不符。本机 `.local` 文件和 `providers.local.yaml` 无需移动或修改。

`deploy/.env.example` 是变量名模板。发布文件必须填写每个镜像的完整 `@sha256:` 引用，使用独立的 `EM_COMPOSE_PROJECT=enterprise-mind-prod-...`，并设置首次空库使用的 `EM_BOOTSTRAP_ADMIN_USERNAME`、批准的 `EM_PUBLIC_HOST`、`EM_EDGE_IP`、HTTPS origin、TLS 文件、配置目录和 Secret 目录。不要把实际 `release.env`、证书或 Secret 复制回仓库。

## Secret 约定

`preflight.py` 要求以下文件存在、不是符号链接，且 Linux 权限为 `0400` 或 `0600`；组和其他用户均无权限：

```text
postgres_owner_password
postgres_migrator_password
postgres_app_password
postgres_backup_password
postgres_checkpoint_password
bootstrap_admin_password
redis.acl
redis_app_password
minio_root_access
minio_root_secret
minio_app_access
minio_app_secret
token_signing_key
database_mcp_token
database_readonly_password
llm_api_key
embedding_api_key
rerank_api_key
ocr_api_key
ocr_secret_id
ocr_secret_key
vision_api_key
converter_token
```

`postgres_owner_password` 这个 Secret 文件名为备份/恢复工具兼容而保留；它现在只作为 PostgreSQL 首次初始化登录 `enterprise_mind_bootstrap` 的密码。该初始化超级用户用于创建受限角色，应用表对象仍由独立的 `enterprise_mind_owner` NOLOGIN 角色拥有。`bootstrap_admin_password` 只挂载给 `migrate`，用于空系统创建首个管理员；用户名由必填的 `EM_BOOTSTRAP_ADMIN_USERNAME` 提供，模板不提供默认账号或密码。

`redis.acl` 需要由部署者按 Redis ACL 格式生成，并使 `enterprise_mind_app` 只能执行队列所需命令；应用密码文件与 ACL 中的密码应一致。MinIO root 只给 `storage-init` 和 MinIO 服务，API/Worker 只接收独立的 `minio_app_*` 身份。企业 PostgreSQL 密码只挂给 `database-mcp`，不会挂给 API 或 Worker。TLS 私钥只挂给 `edge`。

所有 Secret 都由 Compose Secret 文件引用。Compose 的 `_FILE` 变量只在项目代码明确读取该路径的组件使用；不能把环境变量名本身当成已加载 Secret 的证明。

文件型 Compose Secret 通过 bind mount 提供，不能依靠 Secret 的 `uid/gid/mode` 参数重映射文件属主，见 [Compose 官方说明](https://docs.docker.com/reference/compose-file/services/#secrets)。部署配置固定自建后端、Database MCP、Office converter 的 UID/GID 为 `65532:65532`，edge 为 `101:101`；各服务仍只获得原有声明的 Secret 挂载。由批准目标的管理员在工作区外准备文件，按下表设置属主；预检只检查，不自动改权限或复制密码。

| 文件或目录 | 宿主 UID | 权限要求与消费者 |
| --- | --- | --- |
| `EM_SECRET_DIR` 本身 | `0` | `0700`，由本机 rootful Docker 守护进程逐文件挂载，不将整个目录挂入应用 |
| 上述清单除 `redis.acl` 的文件 | `65532` | `0400` 或 `0600`，供自建服务读取；数据服务的 root 入口也可读取 |
| `redis.acl` | `999` | `0400` 或 `0600`，供降权后的 Redis 主进程读取 |
| `EM_TLS_KEY_FILE` | `101` | `0400` 或 `0600`，只挂给 edge |
| `EM_TLS_CERT_FILE` | `101` | 所有者可读，组/其他用户不可写；证书是公开材料，可用 `0644` |

PostgreSQL、Redis、MinIO 的 Compose 启动用户显式为 `0:0`，保留所选官方镜像族的入口约定。PostgreSQL 入口先读取 `_FILE`，导出密码并清除文件变量，再切换为 postgres 账号；因此原 `postgres_owner_password` 文件同时供入口和 `db-provision` 使用，无需第二份密码。[PostgreSQL 入口源码](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh)说明了这个顺序。Redis 入口切换主进程到 UID 999，Docker 健康检查仍以配置的 root 身份读取应用密码；UID 依据 [Redis 官方镜像模板](https://github.com/redis/docker-library-redis/blob/master/Dockerfile.template)。MinIO 保留 root 运行约定，共用的 root 凭据同时供 UID 65532 的 `storage-init` 读取。不要仅凭镜像 `Config.User` 推断入口内部的降权行为；最终锁定镜像仍需单独核验。

这套宿主 UID 对应关系只适用于同一批准 Linux 主机上的 rootful、无 user namespace 重映射的 Docker。生产预检必须带 `--compose-check`，只读检查 Docker `SecurityOptions` 并拒绝 rootless 或 userns-remap；这些模式的 UID 映射不同，见 [Docker 用户命名空间说明](https://docs.docker.com/engine/security/userns-remap/)。已启用其他映射方式的目标应另行设计文件属主映射。启动后核对镜像中的实际挂载读取权限。

发布 `EM_CONFIG_DIR` 是整目录只读挂载，它本身及 Tokenizer 子目录还须允许 UID 65532 遍历，YAML/Tokenizer 文件须允许该 UID 读取；其中不得放入 Secret 值。它与 root 私有的 `EM_SECRET_DIR` 使用不同的访问方式，目标环境仍需分别核验。

预检检查五份 YAML 和已配置 Tokenizer 的挂载路径。允许相对符号链接在配置目录内解析，拒绝越界、断链、循环和绝对路径链接；即使宿主绝对链接指向配置目录内部，挂到 `/run/config` 后也不能假定它仍有效。配置字段中的 `/run/config/...` Tokenizer 路径仍受支持。若 `providers.yaml` 相对链接到子目录，Tokenizer 相对路径按产品加载器的规则，以解析后 YAML 所在目录为基准。

生产预检由 root 在 Linux 上创建一个短暂子进程，清空附加组并切换为 `65532:65532`，从预先打开的配置挂载根目录进入，实际以只读方式打开 YAML/Tokenizer 并确认其为常规文件；该子进程不读取文件内容，也不改变父进程身份。检查有超时和子进程回收，失败返回固定诊断。目录需要搜索权限，文件需要读取权限；不要求通过放宽为全员可写来解决权限问题。配置目录之外的宿主祖先目录不出现在容器挂载内，因此不要求 UID 65532 能遍历它们。过程依据 [Python 的进程及权限接口](https://docs.python.org/3.13/library/os.html)和 [Linux fchdir 权限语义](https://man7.org/linux/man-pages/man2/chdir.2.html)，无需启动容器。

配置发布目录须在预检至启动期间保持不可变。启动后核对容器挂载、SELinux/AppArmor 策略及镜像的文件访问权限。TLS 证书和私钥则与 Secret 目录一样，必须位于仓库外，预检会拒绝解析后仍在仓库内的路径。

## 预检和启动顺序

生产文件预检使用应用本身的 `ApplicationConfig` 模型解析 `application.yaml`，校验字段类型、允许字段和配置约束，再检查实际生效的 `api.workers` 为 1。省略该字段会使用产品默认值 2，因此不符合当前发布条件。合法的行内 YAML 写法可以使用；重复键按产品 YAML 加载器的最终生效值检查，不能以文本中出现 `workers: 1` 代替实际配置。配置错误只输出固定诊断，不输出配置值。`--static` 不读取发布文件，不执行这项验证。

先在目标 Linux 主机执行预检并保存脱敏 JSON：

```bash
cd "/srv/enterprise-mind/releases/<release-id>/source"
export EM_RELEASE_ENV="/srv/enterprise-mind/releases/<release-id>/release.env"
set -a
. "$EM_RELEASE_ENV"
set +a
uv sync --frozen --no-dev --project backend
sudo -E backend/.venv/bin/python deploy/preflight.py --compose-check \
  --evidence "/srv/enterprise-mind/releases/<release-id>/evidence/deployment/preflight.json"
```

预检和轻量发布检查使用由 `backend/uv.lock` 锁定的 Python 3.13 虚拟环境，其中包含锁定版本的 PyYAML；不要假定系统 Python 已安装该依赖。生产文件预检需要读取 root 私有目录和专属 UID 的 TLS 私钥，因此在获批准目标由管理员运行上述 `sudo -E` 命令，保留已加载的发布变量；不要为便于普通用户读取而放宽文件权限。

预检会拒绝缺失文件、占位符、`latest`、无 digest 镜像、非 HTTPS origin、宽泛 edge bind、仓库内 Secret 目录、非 Linux 目标、非单 Worker 配置、无效 Tokenizer 路径/摘要，以及 Docker Compose 渲染失败。生产模式还检查 TLS 叶证书的当前有效期、精确 SAN 匹配、证书/私钥实际配对，以及 Linux 私钥文件的所有者可读且组/其他用户不可访问；私钥应为无需口令的受限文件，加密私钥会被拒绝而不会触发交互输入。证书/私钥加载依据 [Python SSL 文档](https://docs.python.org/3.13/library/ssl.html#ssl.SSLContext.load_cert_chain)，不建立网络连接。证书链信任、撤销状态及 HTTPS 握手按下文目标平台检查执行。`--static` 不读取发布文件，因此不会执行上述 Tokenizer/TLS 文件验证。输出保留配置及 Tokenizer 哈希和固定诊断标签，不输出 Secret 或证书/私钥内容。

首次空库或受控新环境按以下顺序执行。每个一次性步骤都要检查退出码和日志，再进入下一步：

```bash
docker compose --env-file "$EM_RELEASE_ENV" -f deploy/compose.yaml \
  --project-name "$EM_COMPOSE_PROJECT" --profile bootstrap up -d postgres
docker compose --env-file "$EM_RELEASE_ENV" -f deploy/compose.yaml \
  --project-name "$EM_COMPOSE_PROJECT" --profile bootstrap run --rm db-provision
docker compose --env-file "$EM_RELEASE_ENV" -f deploy/compose.yaml \
  --project-name "$EM_COMPOSE_PROJECT" --profile operations run --rm migrate
# If the selected provider configuration enables the LLM, initialize its
# checkpoint tables after migration and before starting application services.
docker compose --env-file "$EM_RELEASE_ENV" -f deploy/compose.yaml \
  --project-name "$EM_COMPOSE_PROJECT" --profile operations run --rm checkpoint-init
docker compose --env-file "$EM_RELEASE_ENV" -f deploy/compose.yaml \
  --project-name "$EM_COMPOSE_PROJECT" --profile operations run --rm storage-init
docker compose --env-file "$EM_RELEASE_ENV" -f deploy/compose.yaml \
  --project-name "$EM_COMPOSE_PROJECT" up -d database-mcp api dispatcher worker edge
docker compose --env-file "$EM_RELEASE_ENV" -f deploy/compose.yaml \
  --project-name "$EM_COMPOSE_PROJECT" ps
```

已有应用数据库时跳过 `bootstrap`，先检查备份和 `alembic current`，再执行唯一的 `migrate` 服务。空数据库迁移时，`migrate` 会使用 `EM_BOOTSTRAP_ADMIN_USERNAME` 和 `bootstrap_admin_password` 创建首个管理员；这些初始化凭据只交给 `migrate`。已有管理员时迁移保留原账号。API、Dispatcher、Worker 启动时不执行迁移。`checkpoint-init` 只在启用 LLM 检查点时运行，应在 `migrate` 成功后单独执行；它只依赖健康的 PostgreSQL，Compose 不会为它自动重跑已退出的一次性迁移服务。模型配置按发布目录中的 Provider 文件生效。

启用 Office 时，`application.yaml` 显式设置 `office_converter.base_url: http://office-converter:8080`，将上面的应用启动命令替换为以下命令。后续对本项目的 `up`/`restart`/`ps` 等操作也使用这组 Compose 文件和 profile；仅添加 `--profile office` 而仍显式只启动 Worker，不会建立转换服务的健康依赖。

```bash
docker compose --env-file "$EM_RELEASE_ENV" \
  -f deploy/compose.yaml -f deploy/compose.office.yaml --profile office \
  --project-name "$EM_COMPOSE_PROJECT" up -d --wait --wait-timeout 180 \
  database-mcp api dispatcher worker edge
```

`compose.office.yaml` 为 Worker 增加必需的 `service_healthy` 依赖，转换服务未就绪会阻止首次启动 Worker；不启用 Office 时使用基础拓扑，保留配置 `null`。此启动顺序依据 [Compose 依赖条件](https://docs.docker.com/reference/compose-file/services/#depends_on)；运行中服务故障仍走已有任务失败/重试流程，Compose 不会自动暂停已启动的 Worker。Worker 是唯一业务调用方，并持有内部转换服务的 `converter_token` Secret。

内部 `/health/live` 只检查 HTTP 进程；`/health/ready` 检查私有临时目录可写及 LibreOffice 能在 5 秒内启动并报告版本，失败返回不含路径、令牌或引擎输出的 503。健康检查覆盖转换服务基础依赖；实际文档转换与渲染按业务格式检查。固定 LibreOffice 安装包支持 **Linux amd64**，Dockerfile 在下载前校验架构。

发行预检在启用 Office 转换时只接受 `http://office-converter:8080`（允许一个尾斜杠），避免 Worker 将原文件及转换令牌发送到其它主机。未启用时保留 `null`；本地开发使用的 localhost 地址仍由通用配置模型支持，但不能通过生产拓扑预检。

Worker 使用独立的 `model_egress` bridge 网络访问配置的外部模型 API，不发布 Worker 端口。标记为 `internal: true` 的网络没有外部连接；该隔离语义见 [Docker Compose 网络文档](https://docs.docker.com/reference/compose-file/networks/#internal)。模型端点连通性使用[连接检查脚本](../scripts/check_model_connections.py)检查，以 `--config` 指定 Provider YAML，并按配置中的凭据环境变量为检查进程提供 Secret 文件路径。显式执行会发送测试请求并可能计费；Provider 字段见[公开模板](../config/providers.example.yaml)。

`edge` 以 `101:101` 运行，启用只读 rootfs，并将 Nginx 缓存目录挂到 `uid=101,gid=101,mode=0755` 的 tmpfs；挂载选项依据 [Docker tmpfs 文档](https://docs.docker.com/engine/storage/tmpfs/)。入口脚本严格校验 `EM_PUBLIC_ORIGIN`、`EM_PUBLIC_HOST` 与 `EM_HTTPS_PORT` 后，将固定模板渲染到 `/tmp/nginx.conf`，不调用镜像默认的 `/etc/nginx` 模板入口，也不写入 `/etc`。无效、缺失或不匹配的 origin/host/port 会在启动 Nginx 前失败，并且错误信息不会回显输入值。

发布后应记录：发布源码清单哈希、`backend/uv.lock`/`frontend/package-lock.json` 哈希、每个构建镜像的 image ID 和 digest、应用配置哈希、MCP 配置哈希、`alembic current` 与 `heads`、Compose 项目和容器 ID。不要把完整 `docker compose config`（其展开结果可能包含 Secret 路径）放进普通报告。

## 边缘路由和请求大小

边缘只接受已校验且判定为同一主机的 origin 原始 Host、`EM_PUBLIC_HOST` 原始形式及规范化形式；其它主机返回 421。这三个形式按大小写去重，写入 `/tmp/edge-host-map.conf`，使已配置的展开式 IPv6 与压缩式均可访问。HTTP 308 使用规范化后的固定 HTTPS origin，并保留配置端口与原请求路径/查询参数；向 API 转发的 Host 也统一为规范化值。origin 必须是纯 HTTPS origin，可有一个尾斜杠；若带端口，该端口必须与 `EM_HTTPS_PORT` 一致（未设置时为 443）。DNS 名使用 ASCII LDH 标签；IPv4 必须是规范四段十进制地址；IPv6 origin 使用方括号，`EM_PUBLIC_HOST` 中的 IPv6 地址不带方括号。单标签 DNS 名 `default`、`include` 仅在 map 的键位置转义，避免被 Nginx 当作配置指令；这不会改变实际 Host 值。

Nginx 将前端未知路径回退到 `index.html`，所以 `/health/live` 和依赖检查 `/health/ready` 都精确代理到 API，避免健康检查误收 SPA 的 HTML `200`。`/assets/` 只提供实际文件，缺失资源返回 404，不会回退为 HTML。index 与其它前端路径使用 `Cache-Control: no-cache`；Vite 哈希资源只在 200/206/304 响应上使用一年 `immutable` 缓存。API/健康响应保留上游缓存头。安全响应头和缓存头都配置在 TLS server 层，新增 location 时不要单独声明 `add_header`，以免覆盖继承的安全响应头。PDF.js 构建产物使用 `.mjs` 模块 Worker；边缘 MIME 表显式将 `.mjs` 映射为 JavaScript，`nosniff` 响应头保持启用。

Nginx 服务级请求上限保留为 `100m`；`/api/` 路径使用 `1001m`，对应生产配置的 `max_bytes: 104857600`、`max_files: 10` 和接收器额外允许的 `1048576` 字节 multipart 开销，即 `max_bytes × max_files + 1 MiB`。如果调整 `max_bytes` 或 `max_files`，必须同步调整 `/api/` 的 Nginx 上限和边界核对；Nginx 的 `m` 单位按 1,048,576 字节计算。

## HTTPS 和目标平台检查

生产检查必须使用批准域名和有效证书链：

```bash
set -o pipefail
EM_HTTPS_PORT="${EM_HTTPS_PORT:-443}"
EM_HTTP_PORT="${EM_HTTP_PORT:-80}"
PUBLIC_ORIGIN="${EM_PUBLIC_ORIGIN%/}"
PUBLIC_HOST_URL="$EM_PUBLIC_HOST"
EDGE_ADDRESS="$EM_EDGE_IP"
case "$EM_PUBLIC_HOST" in *:*) PUBLIC_HOST_URL="[$EM_PUBLIC_HOST]" ;; esac
case "$EM_EDGE_IP" in *:*) EDGE_ADDRESS="[$EM_EDGE_IP]" ;; esac
curl --fail --silent --show-error --resolve "$PUBLIC_HOST_URL:$EM_HTTPS_PORT:$EDGE_ADDRESS" \
  "$PUBLIC_ORIGIN/health/live" \
  | backend/.venv/bin/python -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("status") == "alive" else 1)'
curl --fail --silent --show-error --resolve "$PUBLIC_HOST_URL:$EM_HTTPS_PORT:$EDGE_ADDRESS" \
  "$PUBLIC_ORIGIN/health/ready" \
  | backend/.venv/bin/python -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("status") == "ready" else 1)'
curl --include --resolve "$PUBLIC_HOST_URL:$EM_HTTP_PORT:$EDGE_ADDRESS" \
  "http://$PUBLIC_HOST_URL:$EM_HTTP_PORT/health/live"
```

检查 HTTP 到 HTTPS 的 308、证书 SAN/链、静态资源、认证 API、WebSocket `/api/v1/ws/document-processing`、Agent SSE `/api/v1/agent-runs/<id>/events`、文件下载和安全响应头。Database MCP 不经过边缘代理，也不向浏览器暴露 `/mcp`。

## 构建和字体

后端镜像按 `backend/uv.lock` 用 `uv sync --frozen --no-dev` 安装，不复制 Windows `.venv`；前端按 `frontend/package-lock.json` 执行 `npm ci`。后端镜像安装 `fonts-noto-cjk`，PDF 渲染器会探测 Linux 的 `NotoSansCJK-Regular.ttc`；Office 转换镜像沿用既有 LibreOffice 和 CJK 字体安装。镜像构建完成后，在同一镜像内检查四格式生成、基础图表、下载及中文文本显示。

四个生产构建均以仓库根目录为上下文，因此使用根 `.dockerignore`。`deploy/.dockerignore` 不参与这些构建；若添加 `Dockerfile.dockerignore`，该文件会覆盖根规则。根规则只包含后端源码/迁移/锁文件、六份默认配置、前端源码/构建配置和必要部署文件；实际本地配置、Secret、证书私钥、虚拟环境、测试证据和已有前端 dist 不进入构建上下文。规则位置和优先级依据 [Docker 文档](https://docs.docker.com/build/concepts/context/#dockerignore-files)。

Office 转换器与后端/MCP 一样，必须传入固定摘要的 `EM_PYTHON_IMAGE` 和 `EM_UV_IMAGE`；Dockerfile 实际使用这两个参数，依赖按锁文件冻结安装，不在构建时另从 PyPI 安装 uv。LibreOffice 发行包仍按 Dockerfile 中的版本和 SHA-256 校验。在最终镜像内检查转换、中文渲染、认证和隔离行为。

可在上述锁定的 Python 3.13 环境执行轻量检查，不需要启动服务：

```bash
backend/.venv/bin/python deploy/backup/security_release_check.py --output /path/to/evidence/local-security.json
backend/.venv/bin/python -m unittest discover -s deploy/backup -p 'test_security_release_check.py' -v
```

检查会记录实际生效的 ignore 文件，验证当前 Dockerfile 的 COPY 输入、敏感路径反例、镜像参数作用域与最终非 root 用户，并扫描应用源码、部署配置和现有前端产物中的明显凭据标记。输出只有问题类型、文件和行号，不包含命中的凭据内容。检查器只支持本仓库使用的简单 ignore glob 和字面 COPY 语法，遇到不支持的语法返回失败；镜像构建、其他秘密检测和依赖公告扫描分别执行。前端产物检查单独记录文件哈希，发布时按当前源码重新构建 dist。

依赖与镜像系统包公告检查记录所查询的源、时间、锁文件或镜像摘要、覆盖范围和原始结果。查询中断时保留错误状态，完成查询后再记录结果。

## 回滚边界

不得用删卷重建证明升级或回滚。先确认旧应用是否兼容当前迁移 head；不兼容时按[独立备份恢复流程](backup/README.md)切换到新的数据目录、MinIO 存储和 Compose 项目。发布前保留旧版本镜像 digest、配置哈希、迁移状态和恢复演练证据。未知 Compose 项目、卷、端口和线上主机不在本材料的操作范围内。
