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
| `LLM_MAX_CONCURRENCY` / `RERANK_REMOTE_MAX_CONCURRENCY` | 50 / 50 | LLM 与远程重排的并发及 HTTP 连接上限；供应商配额仍独立生效 |
| `MODEL_IO_WORKERS` / `MODEL_IO_QUEUE_SIZE` | 50 / 50 | 模型任务执行线程与额外等待容量；模型单例仍由锁保护 |
| `RATE_LIMIT_IO_WORKERS` / `RATE_LIMIT_IO_MAX_IN_FLIGHT` | 50 / 50 | 限流检查线程与总在飞任务上限 |
| `REFUSE_THRESHOLD` | 0.2 | 重排最高分低于它就拒答 |
| `RATE_LIMIT_PER_MIN` / `RATE_LIMIT_PER_DAY` | 50 / 200 | 每 IP 与每会话的频率额度，日额度不是并发限制 |
| `RATE_LIMIT_IP_ENABLED` | true | 是否额外按 IP 限流；关闭后仍保留每会话限流 |
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

再加前端 `npm run build`。并发压测推荐在 `backend/`、已激活项目 Python 环境且本机服务就绪后执行（会访问真实服务与模型）：

```bash
python -m eval.load_test --concurrency 10 --mode json
python -m eval.load_test --concurrency 20 --mode stream --debug
python -m eval.load_test --concurrency 20 --requests 100 --mode both --ramp 30 --output logs/load-test.json
```

工具先检查 `/health`，按固定 worker 数发送请求，报告成功延迟分位数、SSE 首字/完整回答耗时与错误码；`--debug` 的阶段统计需服务端开启 `ALLOW_TRACE`。默认循环使用 `data/eval_colloquial.jsonl` 的去重问题，不存在或为空时使用内置 10 题。报告只存问题前 20 字（常见凭据格式脱敏）与答案长度，不保存答案原文。若 RATE_LIMIT_PER_MIN / RATE_LIMIT_IP_ENABLED 未放开，高并发会触发 42901/42902，统计单列为「被本服务限流」。非 `127.0.0.1/localhost` 目标需显式加 `--i-know-this-is-not-localhost`；更多参数和指标口径见[开发文档第 7.3 节](docs/电商RAG智能客服-开发文档.md#73-并发与端到端验收)。

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

当前樱花 FRP/Caddy 本机部署使用 `sh deploy/start-local.sh` 启动后端（在仓库根目录执行）。脚本显式设定上述容量为 50，避免旧 `.env` 中的小值覆盖默认值；不修改 `.env`。由于现有隧道尚未透传可验证的真实 IP，该脚本暂时关闭共享 IP 桶，仅按会话限流，避免手机和压测相互占用额度。session 不是可信身份，接入可信真实 IP 后应恢复 IP 限流。操作步骤见 [本机部署说明](docs/樱花FRP本机部署.md)。

## 常见问题

- **启动失败或一直预热**：先确认 `docker compose ps` 三项都 healthy、数据已入库、模型权重完整。
- **前端连不上**：后端 8010、前端 3000；检查 `VITE_MOCK` 是否关闭。
- **看不到链路**：需要前端入口（`?debug=1` 或 `VITE_SHOW_TRACE_TOGGLE=1`）、请求 `debug=true` 与后端 `ALLOW_TRACE=true` 同时满足。
- **返回 502/空答案**：多半是模型参数问题，先跑 `uv run python -m eval.llm_ping`。
- **变慢或频繁超时**：本机内存不足会让重排慢 10 倍；关闭无关应用、先预热。海外访问智谱的网络抖动见演示准备第六章。

当前没有登录、个人订单查询或真实人工工单。对话原文保存在 MySQL，请自行管理访问与保留期限（`ingest.purge_chat_logs`）。

## 远程重排（智谱 GLM Rerank）

本地 `bge-reranker-v2-m3` 与云端后端可选，默认仍为本地。把以下模板追加到自己的 `.env`；远程模式复用 `LLM_API_KEY`、`LLM_BASE_URL`，启动时不加载本地重排模型，FAQ/检索的 bge-m3 仍需准备：

```dotenv
RERANK_PROVIDER=remote
# 仅在重排使用独立服务地址或密钥时填写，否则留空复用 LLM_*。
RERANK_REMOTE_BASE_URL=
RERANK_REMOTE_API_KEY=
RERANK_REMOTE_MODEL=rerank
RERANK_REMOTE_TIMEOUT_S=10
RERANK_REMOTE_MAX_RETRIES=1
RERANK_REMOTE_RETRY_BACKOFF_S=0.5
RERANK_REMOTE_MAX_CONCURRENCY=50
RERANK_REMOTE_EXTRA_BODY=
RERANK_FALLBACK=rrf
REMOTE_REFUSE_THRESHOLD=
REMOTE_RAG_CONTEXT_MIN_SCORE=
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RERANK_PROVIDER` | `local` | `local` / `remote`；`RERANK_ENABLED=false` 优先，完全跳过重排 |
| `RERANK_REMOTE_BASE_URL` | 空 | 回退到 `LLM_BASE_URL`；去掉末尾斜杠后加 `/rerank`，智谱为 `https://open.bigmodel.cn/api/paas/v4` |
| `RERANK_REMOTE_API_KEY` | 空 | 回退到 `LLM_API_KEY`；均为空视为未配置 |
| `RERANK_REMOTE_MODEL` | `rerank` | 非空模型名；当前智谱仅支持 `rerank` |
| `RERANK_REMOTE_TIMEOUT_S` | 10 | 单次传输超时，正的有限秒数；超时不重试 |
| `RERANK_REMOTE_MAX_RETRIES` | 1 | 非负整数；仅连接故障、5xx、429 重试 |
| `RERANK_REMOTE_RETRY_BACKOFF_S` | 0.5 | 非负有限秒数，指数退避 |
| `RERANK_REMOTE_MAX_CONCURRENCY` | 50 | 正整数；全局共享并发上限，排队和退避计入整请求 deadline |
| `RERANK_REMOTE_EXTRA_BODY` | 空 | JSON 对象，空表示无扩展；只记录顶层键名，核心参数优先 |
| `RERANK_FALLBACK` | `rrf` | `rrf` / `local` / `error`，见下 |
| `REMOTE_REFUSE_THRESHOLD` | 空 | [0,1] 内的有限浮点数，含边界；空时使用 `REFUSE_THRESHOLD` 并在装配时警告需校准 |
| `REMOTE_RAG_CONTEXT_MIN_SCORE` | 空 | 远程重排的上下文过滤阈值，允许任意有限浮点数；空时保留全部 TopK，设置后仍至少保留最高分资料；本地及本地回退继续使用 `RAG_CONTEXT_MIN_SCORE` |

远程调用失败时，`rrf` 使用原始 RRF 顺序和分数，仅在无结果时拒答，并跳过改写补救与相关度上下文过滤；trace 标记 `provider=fallback_rrf, fallback=rrf`。`local` 首次失败时才加载本地模型，并在线程池重排，使用本地 `REFUSE_THRESHOLD`；本地再失败则退 RRF。`error` 抛出脱敏的 AppError（50201/502、超时 50401/504；上游持续 429 为 SERVICE_BUSY 50302/503，与 LLM 客户端一致）。42901/429 仅表示本服务分钟频率限流。取消和整请求 deadline 不触发额外回退工作。

在 `backend/` 真实环境中依次执行（这些命令会调用服务）：

```bash
uv run python -m eval.rerank_ping
uv run python -m eval.check_retrieval --rerank-provider remote
uv run python -m eval.check_retrieval --rerank-provider local
```

先用 ping 核实 index、分数范围、返回顺序与 usage，再比较 Hit@1/Hit@5、每题排名/分数、拒答最高分与重排耗时，设置 `REMOTE_REFUSE_THRESHOLD`。建议阈值为 RAG 命中最低分与拒答最高分的中点，仅供参考；分布重叠或存在未命中题时需检查数据。校准工具强制调用失败即报错，防止把回退分数用于远程阈值。`eval.run_eval` 的 rerank 耗时同样包含远程调用与回退。

接口每次最多 128 条候选，超过时分批；问题与每条候选最多 4096 字符，发送前截断。`RERANK_CANDIDATES`、child/parent 模式与既有 `RERANK_MAX_CHARS` 规则仍生效；远程不使用本地 batch size、token max length 或 FP16 设置。

官方文档未明确 index 从 0 还是 1 开始、分数取值范围、结果是否降序。请求始终设置 `return_documents=true`；index 构成完整 0 基双射（无重复、无越界、覆盖全部候选）时以 index 为准，回显文本被空白规整、截断等差异仅记一条诊断计数，日志不含问题、候选文本或 key。index 非法或缺项时按文本逐一匹配（重复文本依出现顺序各用一次）；仍无法一一对应或结果数量不完整视为 `bad_response`。分数须为有限数，不做变换，由客户端自行排序；ping 打印实测分数最小/最大值与返回顺序是否降序。

隐私：用户问题与检索候选文本会发送到智谱，和 LLM 调用处于同一信任边界。日志与诊断不记录问题、候选文本或密钥；供应商错误仅保留脱敏、截断的允许字段。离线测试全部使用 MockTransport，不发送真实请求。
