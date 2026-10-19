# 灵码 · 电商 RAG 智能客服

面向店铺「灵码」公开政策与商品咨询的访客客服。后端 Python 3.11 / FastAPI，前端 React 18 / TypeScript / Vite / Tailwind。

在线链路：限流 → Redis 缓存 → FAQ（BM25 + bge-m3）→ LLM 路由 → Milvus 混合检索 → 重排 → 带 `[n]` 引用的生成；资料不足或敏感问题拒答。支持 SSE 流式回答、多轮上下文和浏览器历史对话。

完整的架构、接口、全部配置项与默认值、评测、部署说明和决策记录见 **[开发文档](docs/电商RAG智能客服-开发文档.md)**；前端见 [frontend/README.md](frontend/README.md)；演示流程与遇到的问题见 [docs/演示准备.md](docs/演示准备.md)。

## 快速开始

需要 uv、Python 3.11、Docker Compose v2、Node.js ≥ 18.18。本地模型 `models/bge-m3`、`models/bge-reranker-v2-m3` 需自行准备（运行时不会自动下载）。

**1. 配置**：在仓库根目录创建 `.env`（不要提交）。最小配置：

```dotenv
MYSQL_PASSWORD=改成你的本地密码
MYSQL_ROOT_PASSWORD=改成你的本地密码
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ROUTER_MODEL=glm-5.3-flash
GENERATE_MODEL=glm-5.3-flash
LLM_EXTRA_BODY='{"thinking":{"type":"enabled","effort":"low"}}'
```

glm-5.3-flash 始终思考，必须按上面开启思考；用其他 OpenAI 兼容模型时按其文档设置，思考类模型的输出配额见开发文档。

**2. 启动依赖**（Milvus、MySQL、Redis）：

```bash
docker compose up -d
```

**3. 安装后端并入库**：

```bash
cd backend
uv sync --python 3.11
uv run python -m ingest.migrate_db
uv run python -m ingest.run --dir ../data/docs
uv run python -m ingest.load_faq ../data/faq.csv
```

**4. 启动后端**（8010）：

```bash
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010
```

**5. 启动前端**（3000，`/api` 代理到 8010）：

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:3000>。不想启动后端，可以用 `VITE_MOCK=1 npm run dev` 体验离线固定响应。

检查后端：`curl http://127.0.0.1:8010/health`。

## 常用配置

完整列表与默认值见开发文档第 4 节。最常调整的几项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_EXTRA_BODY` | `{}` | 智谱需开启思考，见上 |
| `LLM_MAX_CONCURRENCY` | 2 | 对模型服务的并发上限，应与账号并发配额一致，演示建议 4 |
| `REFUSE_THRESHOLD` | 0.2 | 重排最高分低于它就拒答 |
| `RATE_LIMIT_PER_MIN` / `RATE_LIMIT_PER_DAY` | 10 / 200 | 每 IP 与每会话限流，单机演示可调大 |
| `ALLOW_TRACE` | false | 开启后，请求带 `debug=true` 才返回链路 |
| `TRUST_PROXY_HEADERS` / `CLIENT_IP_HEADER` | false / x-forwarded-for | 放在可信代理后才开启；Cloudflare 隧道用 `cf-connecting-ip` |

前端变量见 [frontend/README.md](frontend/README.md)。`VITE_*` 会公开到浏览器，不能放密钥。

## 测试

平时只跑单元测试：

```bash
cd backend && uv run pytest -q
cd frontend && npm test && npm run typecheck
```

大提交、合并到 main 前再跑全量（需要依赖服务和真实模型）：

```bash
cd backend
uv run python -m eval.run_eval --eval-file data/eval.jsonl --output logs/eval-standard.json
uv run python -m eval.run_eval --eval-file data/eval_colloquial.jsonl --output logs/eval-colloquial.json
uv run python -m eval.run_eval --eval-file data/eval_multiturn.jsonl --output logs/eval-multiturn.json
uv run python -m eval.check_retrieval
```

再加前端 `npm run build` 和并发压测（做法见开发文档第 7 节）。

## 数据维护

在 `backend/` 执行：

```bash
uv run python -m ingest.run --dir ../data/docs      # 文档入库（--dry-run 只切块）
uv run python -m ingest.load_faq ../data/faq.csv    # FAQ 入库
uv run python -m ingest.clear_cache                 # 清 FAQ 缓存
uv run python -m ingest.migrate_db                  # 数据库补列（幂等）
```

改了文档或 FAQ 后重新入库并清缓存；标题或切块变化需先按[开发文档 5.2 节](docs/电商RAG智能客服-开发文档.md#52-入库与维护命令)清理受影响资料的旧块，再导入，避免保留旧文案。FAQ 更新需重启后端重建内存索引。

## 部署

本机 + Cloudflare 固定隧道（临时隧道不支持流式输出）。后端只监听 127.0.0.1，经本机反向代理托管 `frontend/dist` 并转发 `/api`，关闭缓存、压缩和代理缓冲，并配置真实 IP 头。详见开发文档第 8 节。

## 常见问题

- **启动失败或一直预热**：先确认 `docker compose ps` 三项都 healthy、数据已入库、模型权重完整。
- **前端连不上**：后端 8010、前端 3000；检查 `VITE_MOCK` 是否关闭。
- **看不到链路**：需要前端入口（`?debug=1` 或 `VITE_SHOW_TRACE_TOGGLE=1`）、请求 `debug=true` 与后端 `ALLOW_TRACE=true` 同时满足。
- **返回 502/空答案**：多半是模型参数问题，先跑 `uv run python -m eval.llm_ping`。
- **变慢或频繁超时**：本机内存不足会让重排慢 10 倍；关闭无关应用、先预热。海外访问智谱的网络抖动见演示准备第六章。

当前没有登录、个人订单查询或真实人工工单。对话原文保存在 MySQL，请自行管理访问与保留期限（`ingest.purge_chat_logs`）。
