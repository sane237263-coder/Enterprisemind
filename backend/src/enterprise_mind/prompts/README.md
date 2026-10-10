# 内置 Prompt

已实现功能的系统提示按 `agent/`、`rag/`、`conversation/` 和 `vision/` 集中保存，作为 Python 包资源随 wheel 发布，由代码与 Git 管理。`load_prompt` 只允许代码内登记的名称，与运行时工作目录无关；修改后重启相关进程。

工具 Schema、运行时反馈、问题、原文和回执由代码按既有权限及隐私边界组装，不作为 Prompt 文件保存。权限控制由服务端执行。Database 的选择关系与 SQL 生成规则已登记为 `agent/database_select` 和 `agent/database_generate`；Memory 通过 Agent runner 和 Memory service 接入，长期写入由服务端分类、敏感内容限制及用户级修订号约束。
