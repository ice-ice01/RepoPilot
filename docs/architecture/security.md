# 登录、权限与数据隔离

## 初始化与用户

- 所有业务 API 均要求真实登录。只有 `/health`、认证状态、登录和受控注册入口可匿名调用；GitHub Webhook 使用独立的 HMAC 验签。
- 无默认账号或密码。第一次注册创建初始化管理员；开发环境仅允许 loopback 客户端初始化。远程或生产环境必须配置随机的 `AUTH_BOOTSTRAP_TOKEN`，请求使用 `X-Bootstrap-Token`。反向代理会改变客户端地址，首次启动应始终配置密钥，避免依赖代理后的 loopback 判断。
- 首次初始化沿用旧演示用户 ID，将原先无归属的仓库、分析、评测及旧工作区归属该管理员。会话、文档、任务等随仓库归属，原 ID 和历史内容保留。没有密码的旧用户不能直接登录。
- 管理员可通过 `POST /api/auth/users` 创建账号；页面右上角账号设置提供同样功能。其他 `owner` 没有账号管理权限，也不会自动获得他人的数据。
- 自助注册由 `AUTH_ALLOW_REGISTRATION` 控制，默认关闭。开放时角色固定为 `developer`，客户端不能提升自身权限。

## 密码与会话

- 密码至少 12 个字符；数据库存储随机盐的 PBKDF2-HMAC-SHA256 摘要（600,000 次），不存明文。
- 登录返回 HttpOnly、SameSite=Lax 的随机会话 Cookie；服务端只保存令牌及 CSRF 值的 SHA-256 摘要。前端不把会话令牌写入 localStorage。
- `AUTH_SESSION_HOURS` 默认 720（30 天），允许 1–720 小时。退出登录删除当前服务端会话；修改密码撤销所有旧会话，再签发当前新会话。过期、撤销、停用账号均返回 401。
- 登录失败按账号（5 次）和客户端 IP（30 次）在 15 分钟窗口内限制尝试，返回 429 和 Retry-After。客户端 IP 依赖可信反向代理设置，不信任用户自行传来的任意代理头。
- Cookie 鉴权的 POST/PATCH/DELETE 等请求必须携带 `X-CSRF-Token`，其值取自可读的 `devflow_csrf` Cookie；同时校验 Origin。登录和注册也校验请求来源。
- API 响应禁用缓存。前端在会话失效、退出或切换账号时卸载旧账号的业务组件。

## 数据与工具边界

- 仓库、分析结果、评测有明确 owner；会话、消息、记忆、RAG 文档、Issue、PR、CI、任务、草稿等通过仓库归属隔离。工作区按创建人隔离。不存在团队共享成员功能，角色只决定自有数据上的操作。
- 请求数据库 Session 统一附加归属条件，覆盖 ORM 查询、分页计数、别名、关联加载及批量更新/删除。写入校验所有者和外键；入口还检查 URL、查询和 JSON 中的资源 ID，再调用向量检索、文件服务或提交后台任务。访问他人资源返回 404。
- `viewer` 只读；`developer` 可连接仓库、运行分析/对话和创建草稿；`maintainer` 另外可同步和确认草稿；`owner` 可管理自有项目设置。创建用户、服务器目录和自定义 GitHub API 地址只对初始化管理员开放。
- 工具使用服务端授权后的仓库/会话 ID，不能由模型参数切换到另一用户。服务器内部后台任务使用提交时已授权的具体 ID；它们不提供匿名调用入口。
- 同一上游仓库在不同账号下有独立记录、凭据、UUID checkout 和知识索引；不回退复用同名 checkout。自有仓库 Token 加密持久化，不回退借用全局 `GITHUB_TOKEN`。
- GitHub 写操作只经有权限用户确认的安全草稿执行；聊天工具仅生成草稿。配置 `GITHUB_WEBHOOK_SECRET` 后，Webhook 才接受签名正确的请求；未配置返回 503。

## 部署与接口使用

生产设置 `APP_ENV=production`，强制 Secure Cookie，并通过 HTTPS 提供服务。设置精确的 `CORS_ORIGINS`，不要把不受信任的前端加入白名单。前端/API 使用相同主机名（或 `/api` 反向代理），以便 CSRF Cookie 可读取；本地开发自动统一 localhost 与 127.0.0.1 的主机名。生产模式还必须设置随机 `TOKEN_ENCRYPTION_KEY`（加密仓库 GitHub Token），使用内置默认值时后端拒绝启动。反向代理部署时配置 `TRUSTED_PROXIES=loopback`（或代理 IP/CIDR 列表）：只有受信代理直连时才解析 X-Forwarded-For 最后一跳作为客户端地址，使登录 IP 限流按真实用户生效；不配置时所有请求都按代理地址计数。

自动化脚本先 `POST /api/auth/login` 获取 Cookie，后续复用 Cookie 并给写请求加 `X-CSRF-Token`。可信非浏览器客户端也可将同一会话令牌作为 `Authorization: Bearer <session-token>` 使用；此时不使用 Cookie 鉴权，不要求 CSRF，令牌仍有同样的过期和撤销规则。登录不会在响应 JSON 中返回令牌；从登录响应 Cookie 读取，勿写入日志或源码。

`scripts/verify_local.py` 需要现有 `owner` 账号：设置 `DEVFLOW_VERIFY_USERNAME`，运行时交互输入密码；无人值守可用短期环境变量 `DEVFLOW_VERIFY_PASSWORD`。脚本不会创建默认管理员或绕过鉴权。

启动会幂等补齐旧表认证列、归属列和索引，移除仓库名的全局唯一限制；PostgreSQL 下多 worker 启动由咨询锁串行化，并为 issues/pull_requests/workflow_runs 按 (repo_id, github_id) 去重后补建唯一索引，作为同步 upsert 的并发兜底。部署前按常规备份数据库；迁移后第一次初始化将旧数据划归管理员。认证迁移已在 SQLite 回归用例验证；生产 PostgreSQL 迁移应在发布环境完成验证。重启时遗留的 queued/running 评测会被标记为 error，前端轮询可正常结束。

## 回归测试

`backend/app/tests/test_auth_isolation.py` 使用独立内存数据库，通过真实 ASGI HTTP 请求验证业务路由拒绝匿名访问、初始化权限、密码/会话撤销、CSRF、角色控制、跨用户资源访问以及旧 SQLite 数据迁移；不调用真实 GitHub、AI 或向量服务。
