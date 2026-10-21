# CI/CD：构建、部署与旧镜像清理

## 触发与目标

- 源码仓库：`david-yang-pixel/bajiao`，公开；镜像仓库为同名 GHCR package，按私有镜像发布和认证拉取。
- 向当前默认分支 `feat/swing-analysis` 推送，自动测试、构建并部署；支持 Actions 页面手动运行。
- Pull Request 仅构建与测试，不访问部署密钥或发布生产镜像。
- 目标服务器使用既有 SSH 配置首次接入，项目目录固定 `/opt/bstocks-bot`，容器固定 `binance-bstocks-analysis-bot`，端口 8000 与现有 Nginx 保持一致。
- 生产单实例、单 worker；切换容器存在短暂服务中断，不是零停机方案。

## 流程

1. 按固定提交 SHA 检出源码；第三方 Action 固定完整提交 SHA。
2. 构建 Linux amd64 镜像，记录项目、源码与 revision 标签；生产镜像不包含本地 `.env`、数据库、测试或虚拟环境。
3. 在该镜像内、无外网环境运行回归测试，测试目录只读挂载；另检查前端脚本语法。
4. 仅发布分支可用工作流临时 `GITHUB_TOKEN` 推送至 GHCR；部署使用完整 `@sha256:...`，不是 `latest`。
5. 检查分支仍指向本次提交，过期运行不得覆盖更新版本。工作流与服务器端均串行化部署，不主动取消正在发布的运行。
6. 专用受限 SSH 密钥发送版本、摘要与临时拉取凭据；不获得通用 shell，不接受用户指定路径或其他项目镜像。
7. 服务器先拉取并验证镜像标签，再暂停旧容器，备份 SQLite 和有效环境配置，切换至镜像内的代码（不再挂载旧 `backend` 源码）。
8. 确认 Docker 健康状态、HTTP 健康返回的 revision、数据库配置/持仓及 `.env` 均符合预期。
9. 成功后删除旧容器，再清理本项目未被任何容器引用的旧镜像。失败则恢复旧容器，不做镜像清理。

## 首次接入与部署密钥

从已登录 `gh`、已建立可信服务器 SSH 连接的本机运行：

```bash
python3 scripts/setup_cicd.py --ssh-host zendesk-agent-server
```

脚本会：

- 新建独立 Ed25519 密钥，不读取或上传现有登录私钥；临时私钥在完成后删除。
- 使用既有信任记录提取服务器 host keys，严格校验主机身份，不使用关闭校验或盲目信任 `ssh-keyscan`。
- 安装 root 管理的 `ci/deploy_image.py`，为公钥配置 forced command、禁止转发与 PTY，并验证任意 shell 命令会被拒绝。
- 创建 Secrets：`DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS`。
- 创建 Variables：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_PORT`。
- 仅轮换带 `bajiao-github-actions` 标记的专用 key，不删除其他 SSH 授权记录。

应用密钥始终保留在服务器既有环境或 SQLite 中，不复制到 GitHub。GHCR 登录使用本次工作流令牌与临时 Docker 配置目录，结束后删除；不要将账号 PAT、应用 API Key 或服务器原有私钥写入 workflow。

发布器是服务器预安装的受限入口。修改 `scripts/deploy_image.py` 后，应先测试、重新执行接入脚本更新入口，再推送触发发布；普通应用更新不需要重复接入。更换部署分支时，需要同时修改 workflow 的 push 分支、发布 guard 与过期提交检查。

## 保留内容与回滚边界

- 不覆盖服务器 `.env`、`data/`、`logs/` 或旧源码目录；数据库采用 SQLite backup API 备份，文件权限 0600，备份目录位于 `backups/cicd-*`。
- 原有容器的有效应用环境优先保留；新增配置可从服务器 `.env` 或页面填写。数据库配置仍优先于环境变量。
- 新容器只挂载 data/logs，代码来自已经测试的镜像。不要再用旧目录的 `docker compose up --build` 或旧代码热更新脚本覆盖 CI 管理的生产容器。
- 切换失败时恢复旧容器，但**不会自动回灌数据库备份**，避免丢弃新版本已接收的用户写入。数据库迁移必须保持向后兼容；破坏性迁移不能直接走本流程。
- 健康检查证明进程、版本、配置保留与持仓保留，不证明 FMP、模型或消息供应商实际可用，也不验证策略收益；不会通过测试接口向真实渠道发测试消息。
- 若新版本已健康、仅后续清理失败，流水线失败并明确说明，不会因此把健康应用回滚。待清理容器的名称和镜像身份会持久化，下次发布按身份核对后重试，不会永久遗留占用旧镜像的回滚容器。

## 旧镜像清理范围

只考虑 `io.bajiao.project=bajiao` 标签或发布器曾记录的本项目镜像，包含首次迁移时识别的 `bstocks-bot-bstocks-bot:latest`。

删除前检查所有运行中和已停止容器的引用，保留当前镜像、有容器引用的镜像、以及带其他项目 tag/digest 的镜像。不使用 `docker system prune`、全局 `image prune` 或强制删除，不处理其他服务、卷、构建缓存或数据库备份。

按用户要求，成功部署后不保留闲置旧镜像作为长期回滚副本；若事后需要历史版本，需从 GHCR 重新拉取对应摘要。GHCR 历史镜像不在本次“清理服务器旧镜像”范围内。

## 排错

- 查看 GitHub Actions 本次构建和部署日志；服务健康接口为 `/api/health`，`revision` 应等于源码提交。
- 服务器当前版本元数据：`/opt/bstocks-bot/ci/current.json`，不含密钥。
- GHCR 拉取、镜像标签或备份失败时不切换；应用失败时恢复旧容器。只有日志明确显示 `deployed` 且工作流成功才表示整个发布与清理完成。
- 服务器发布器拒绝其他仓库、非摘要地址、非法版本、过期 run number 和额外字段；主机 key 变化需要管理员独立核验并更新 Secret。
- 如需停用自动发布，禁用 workflow 并移除该项目专用公钥/Secrets，保留其他服务的 SSH 配置。
