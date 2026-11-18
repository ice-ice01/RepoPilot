# 研伴 RepoPilot 本地启动说明

本文按 Windows PowerShell 编写，假设 `backend\.venv`、`frontend\node_modules` 和 Docker Desktop 都已准备好。

## 需要启动的服务

完整模式需要启动：

| 服务 | 地址 | 作用 |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:5432` | 业务数据库 |
| etcd | Docker 内部 `2379` | Milvus 依赖 |
| MinIO | `127.0.0.1:9000`，控制台 `9001` | Milvus 依赖 |
| Milvus | `127.0.0.1:19530` | RAG 向量检索 |
| FastAPI 后端 | `http://127.0.0.1:8000` | API、Agent、RAG |
| Next.js 前端 | `http://127.0.0.1:3000` | Web 页面 |

Redis 是 `optional` profile，当前启动流程不依赖它，不需要单独启动。项目也没有单独的 Celery/Worker 进程，定时同步等后台任务由 FastAPI 进程处理。

## 推荐方式：一条命令启动完整环境

在项目根目录执行：

```powershell
.\scripts\start_local.ps1
```

这个脚本会启动四个 Docker 依赖、后端和前端，并等待健康检查通过。启动完成后访问：

- 前端：<http://127.0.0.1:3000>
- 后端 API 文档：<http://127.0.0.1:8000/docs>

如果 MinIO 镜像还没有构建过，先执行一次：

```powershell
docker compose build minio
```

端口冲突时可以传参，例如：

```powershell
.\scripts\start_local.ps1 -BackendPort 8001 -FrontendPort 3001
```

停止由该脚本启动的前后端：

```powershell
.\scripts\stop_local.ps1
```

该命令不会停止 Docker 服务，也不会删除数据。只停止 Docker 基础设施时执行：

```powershell
docker compose stop
```

## 保留前端 `npm run dev` 时的启动方式

如果前端仍然单独执行 `npm run dev`，需要另外打开两个 PowerShell 终端。

### 终端 1：启动基础设施

在项目根目录执行：

```powershell
docker compose up -d --no-build --wait --wait-timeout 180 postgres etcd minio milvus
```

如果提示 MinIO 镜像不存在，先执行上一节的 `docker compose build minio`。

### 终端 2：启动后端

```powershell
cd backend
$env:PYTHONPATH = (Get-Location).Path
$env:DATABASE_URL = "postgresql+psycopg://devflow:devflow@127.0.0.1:5432/devflow"
$env:MILVUS_ENABLED = "true"
$env:MILVUS_URI = "http://127.0.0.1:19530"
$env:DEMO_MODE = "false"
$env:AUTO_SYNC_ENABLED = "false"
$env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:8000"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

这就是后端的直接启动命令：`python -m uvicorn app.main:app --reload`。使用项目虚拟环境启动更稳妥，不要直接调用系统 Python。

### 终端 3：启动前端

```powershell
cd frontend
npm run dev
```

如果后端使用了其他端口，要在前端启动前同步设置：

```powershell
$env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:8001"
npm run dev -- -p 3001
```

## AI 和本地 Embedding

后端会读取项目根目录 `.env`。聊天和 Agent 功能还需要在当前用户或系统环境变量中配置：

```powershell
$env:AI_API_KEY = "你的 API Key"
$env:AI_BASE_URL = "你的 OpenAI 兼容接口地址"
$env:AI_MODEL = "你的模型名"
```

不要把真实密钥提交到项目文件中。完整模式使用本地 `Qwen/Qwen3-Embedding-0.6B`，`start_local.ps1` 会使用 `.data\huggingface` 中的缓存并执行启动前检查。手动启动时，如果模型缓存不在默认位置，可设置：

```powershell
$env:HF_HOME = (Resolve-Path ..\.data\huggingface).Path
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
```

## 仅演示页面的 SQLite 模式

只想验证页面、接口和基础结构化数据时，可以执行：

```powershell
.\scripts\start_rag.ps1
```

该脚本使用 SQLite 和 deterministic embedding，不启动 Milvus，向量检索会显示 `degraded`，不能代替完整模式。

## 启动检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
docker compose ps
```

健康检查正常时应返回 `status: ok`。不要随意执行 `docker compose down -v`，它会删除 PostgreSQL、Milvus 和 MinIO 的本地数据卷。
