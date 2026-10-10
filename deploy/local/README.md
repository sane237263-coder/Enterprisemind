# EM 本机开发环境

本机配置使用 **WSL Ubuntu 原生 PostgreSQL 18/main，端口 5433**，应用数据库为 `enterprise_mind`。Redis、MinIO 和 Office 转换器使用独立容器，Database MCP 使用独立只读业务连接。

Windows 虚拟环境与 node_modules 用于备用运行模式。默认启动脚本使用 WSL 模式；`-Runtime Windows` 为备用模式，禁止同时启动两套应用进程。

## 日常使用

Windows 下可以直接双击项目根目录的 **`项目开关.cmd`**，按编号选择启动、停止、查看状态或打开网页。关闭开关窗口不会停止项目；停止项目请选择 `2`，数据会保留，共享 PostgreSQL 和其他项目继续运行。开关沿用下面的 WSL 启停脚本，不会自动初始化或重置环境。

开关自动查找 PowerShell 7.5+（包括本机已有的 Codex 运行时）；找不到时会明确提示。操作错误会留在窗口中，多开窗口时同一项目的操作会互斥。项目已经运行时仍可选择 `1`：核对全部项目服务及 API、网页、Office 健康状态后，提示“项目已在运行”，不会重启。部分服务未就绪或状态查询失败时会报告问题并保留现状。选择 `4` 可打开现有网页。

在项目根目录使用 PowerShell 7.5 或更新版本：

```powershell
./scripts/local-runtime.ps1 -Action Start
./scripts/local-runtime.ps1 -Action Status
./scripts/local-runtime.ps1 -Action Stop
```

前端：<http://127.0.0.1:5173>。API：<http://127.0.0.1:18000>。首个管理员为 `localadmin`，初始密码保存在 `.local/runtime/secrets/bootstrap_admin_password`，通过本机文件读取，勿提交或复制进日志。更改管理员密码后，此文件中的初始值不再适用于登录验证。

WSL 模式的 `Start` 启动依赖容器与五个本项目 systemd 服务，检查 API 与前端代理就绪；服务退出后由 systemd 重启。`Stop` 停止本项目原生服务、核对归属后的 Windows 备用进程和本项目三个容器，保留数据卷，不停止 Ubuntu PostgreSQL。通过项目开关或上述命令手动启动。

Ubuntu 工具与依赖在 `~/.local/share/enterprise-mind/`；本机配置的项目源代码位于 Windows 桌面，通过 `/mnt/c/Users/<Windows用户名>/Desktop/EnterpriseMind` 读取（新环境请按实际克隆目录映射）。前端 Linux 工作目录引用同一份源码，使用独立 node_modules，并轮询源文件变化。Windows 与 Linux 字节码缓存分开。原生日志及配置在 `.local/runtime/wsl/`。WSL 资源上限由宿主的 `.wslconfig` 管理。

| 服务 | 本机地址 | 持久化位置 |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:5433` | Ubuntu `/var/lib/postgresql/18/main` |
| Redis | `127.0.0.1:36379` | `enterprise-mind-local_redis_data`，AOF |
| MinIO | `127.0.0.1:39000` | `enterprise-mind-local_minio_data`，私有 Bucket |
| Office 转换器 | `127.0.0.1:38002` | 临时文件，无业务持久化 |
| Database MCP | `127.0.0.1:38001` | 独立只读连接 Ubuntu `demo_db` |

PG 使用独立的 owner（不可登录）、migrator、app、backup、checkpoint 角色。日常进程使用 app，迁移使用 migrator，检查点使用 checkpoint，凭据分别保存为 Secret 文件。WSL 模式读取 `config/providers.local.yaml` 与 `config/rag.local.yaml`；Windows 备用模式点源 `.local/model-env.ps1`。提交模板保留默认配置。

## 初始化与版本

已有环境通常只需 `Start`。`Init` 必须在 EM 停止时运行，不清库，不覆盖已有凭据或管理员密码：

```powershell
./scripts/local-runtime.ps1 -Action Init
```

WSL 模式的 Init 使用 `scripts/local_wsl_setup.sh` 准备用户目录中的官方 uv、Node 和锁定依赖，保留 Windows 环境。模型配置、Ubuntu PG 和 Compose 引用的本机镜像需事先准备。当前 Ubuntu PG18.6 已安装 pgvector 0.8.6，迁移版本为 `0016_database_csv_export`；仅复制安装扩展不会更改其他数据库。

开发业务数据源可使用 `demo_db`，初始化命令为 `./scripts/local-runtime.ps1 -Action Init -EnableDemoDatabase`。专用账号 `enterprise_mind_db_readonly` 仅授予 CONNECT、public USAGE 和 public.items SELECT，禁止 CREATE/TEMP/写权限。初始化会保存原始 ACL 和数据指纹到 `.local/runtime/wsl/demo-permissions-before.json`；运行前核对目标数据库及授权范围。

该账号在 `demo_db` 中使用专属日志设置：不记录 SQL 语句、参数或语句错误，满足 MCP 的查询内容保护要求。管理端登记来源后，通过测试连接、刷新和发布 Schema 启用允许访问的对象。

PDF 渲染支持 Noto CJK；本机 WSL 配置优先使用 `/mnt/c/Windows/Fonts/msyh.ttc`。独立 Linux 主机按[部署说明](../README.md#构建和字体)准备 CJK 字体。

`scripts/local_wsl_vector.sh` 是本机首次扩展安装辅助脚本：检查本机 PG 版本与既有镜像 ID，复制已有 PG18 镜像中的 0.8.6 扩展，校验动态库依赖及已有目标文件，记录 SHA256。它依赖本机已存在的测试镜像容器；换机器需要单独准备对应扩展和镜像。Compose 使用 `pull_policy: never`，避免启动时隐式下载。

原生服务仍逐个启动并检查端口、健康与进程就绪，记录 Ubuntu 可用内存；真实启动失败会清理本项目启动资源。运行日志及进程记录在 `.local/runtime/`，凭据目录限制为当前 Windows 用户和 SYSTEM；这些文件均由 `.gitignore` 排除。

## 备份与恢复

```powershell
./scripts/local-runtime.ps1 -Action Stop
./scripts/local-runtime.ps1 -Action Backup
./scripts/local-runtime.ps1 -Action Start
```

备份写入 `.local/runtime/backups/<时间>/`，包括原生 PG 的 custom-format dump、离线 MinIO/Redis 卷归档及文件 SHA256 清单。需要在 EM 应用和三个依赖容器停止时进行，并避免外部客户端同时写入 EM 数据。Ubuntu PG 保持运行，其他数据库可继续使用。

Secret 文件不包含在备份中，需单独安全保存；仅保留 dump 不能恢复原有角色密码和应用签名密钥。dump 不携带 owner/ACL。正式恢复需要先配置 EM 角色与扩展、恢复数据库和卷，再通过迁移及检查点初始化重新授予权限。Compose 环境的备份与独立目标恢复见[备份与恢复说明](../backup/README.md)。
