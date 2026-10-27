# 灵码 · 电商 RAG 智能客服

面向店铺「灵码」公开政策与商品咨询的访客客服。后端 Python 3.11 / FastAPI，前端 React 18 / TypeScript / Vite / Tailwind。

在线链路：来源校验 → 配额/限流 → Redis 缓存 → FAQ（BM25 + bge-m3）→ LLM 路由 → Milvus 混合检索 → 重排 → 带 `[n]` 引用的生成；资料不足或敏感问题拒答。支持 SSE 流式回答、多轮上下文、用户名密码账号（游客每天 5 次、账号每天 50 次，按 `RATE_LIMIT_DAY_TZ_OFFSET_HOURS` 默认 8，即 UTC+8 的自然日计）、登录账号的服务器端历史、商品卡片，以及 Milvus 知识库版本化发布与回滚。

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
# 非调试模式（DEBUG=false，默认）必须非空，否则启动失败；用 openssl rand -hex 32 生成，不要提交
AUTH_SECRET=粘贴生成结果
# 保持为空；它只接受完整 http(s) 地址，填 /images 会启动失败
IMAGE_BASE_URL=
```

生成密钥：`openssl rand -hex 32`。后端应用（`app.config.Settings`）读取的是**仓库根目录**的 `.env`；`ingest.run`、`kb_*`、`migrate_products`、`load_products`、`ragas_eval collect` 等 CLI **不读 `.env`**，见下文“CLI 配置来源”。

glm-5.3-flash 始终思考，必须按上面开启思考；用其他 OpenAI 兼容模型时按其文档设置，思考类模型的输出配额见开发文档。

**2. 启动依赖**（Milvus、MySQL、Redis）：

```bash
docker compose up -d
```

**3. 安装后端并入库**：

```bash
cd backend
uv sync --python 3.11
uv run python -m ingest.migrate_db          # 账号、历史等表（读根目录 .env，幂等）
# 商品表与商品数据：漏了这一步，商品卡片不会出现，日志只有 product_cards_unavailable
uv run python -c "import sys; from app.config import get_settings; from ingest.migrate_products import main; sys.exit(main([], settings=get_settings()))"
uv run python -c "import sys; from app.config import get_settings; from ingest.load_products import main; sys.exit(main(['data/products/products.csv', '--yes'], settings=get_settings()))"
# 知识库：CLI 不读 .env，需先 export MILVUS_URI 等，见开发文档 5.4
uv run python -m ingest.run --dir ../data/docs --dry-run
uv run python -m ingest.run --dir ../data/docs --yes      # 首次本地入库；正式发布走 --staged，见下文
uv run python -m ingest.load_faq ../data/faq.csv
```

`ingest.migrate_products` 与 `ingest.load_products` 本身只读进程环境（`MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`），上面用 `python -c` 显式注入应用配置；也可以先 `export` 这些变量后直接执行 `uv run python -m ingest.migrate_products` 和 `uv run python -m ingest.load_products data/products/products.csv --yes`。不要用 `source .env`（会破坏 JSON 值，见开发文档 5.4）。

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
| `REFUSE_THRESHOLD` | 0.2 | 本地及本地回退重排最高分严格低于它就拒答；不用于云端分数 |
| `RATE_LIMIT_PER_MIN` / `RATE_LIMIT_PER_DAY` | 50 / 200 | 每 IP 与每会话的频率额度，日额度不是并发限制 |
| `RATE_LIMIT_IP_ENABLED` | true | 是否额外按 IP 限流；关闭后仍保留每会话限流 |
| `ALLOW_TRACE` | false | 开启后，请求带 `debug=true` 才返回链路 |
| `TRUST_PROXY_HEADERS` / `CLIENT_IP_HEADER` | false / x-forwarded-for | 放在可信反向代理后才开启；入口若提供 `cf-connecting-ip` 则使用该头名 |

前端变量见 [frontend/README.md](frontend/README.md)。`VITE_*` 会公开到浏览器，不能放密钥。

## 测试

平时只跑单元测试：

```bash
cd backend && uv run --frozen pytest tests -q      # --frozen 避免 uv 自动改写 uv.lock
cd frontend && npm test && npm run build           # build 含 tsc --noEmit 类型检查
```

macOS 的临时目录是符号链接（`/var` → `/private/var`），测试中比较路径时要先 `Path.resolve()`。

商品总览、品类分类与选购推荐统一由 router 判为 `knowledge`，总览问句改写为含「店铺商品品类总览」的检索问句；general 禁止陈述或列举店铺商品、品类、价格、库存和政策，并引导直接提问具体商品或售后问题。`data/eval_colloquial.jsonl` 末尾追加 8 条商品问句（`answer`，参考 `product_catalog.md`）和「你好」「谢谢」2 条 `general` 对照；「你好」作为显式对照重复保留。提示词与数据格式回归测试见 `backend/tests/test_llm_helpers.py`、`backend/tests/test_eval_colloquial.py`，离线测试仅验证规则与数据约束，提示效果必须由总控使用真实模型评测。

大提交、合并到 main 前再跑全量（需要依赖服务和真实模型）：

```bash
cd backend
uv run python -m eval.run_eval --eval-file data/eval.jsonl --output logs/eval-standard.json
uv run python -m eval.run_eval --eval-file data/eval_colloquial.jsonl --output logs/eval-colloquial.json
uv run python -m eval.run_eval --eval-file data/eval_multiturn.jsonl --output logs/eval-multiturn.json
uv run python -m eval.check_retrieval
```

`eval.run_eval` 可加 `--label baseline-name`，名称写入 `meta.label`（默认空）。JSON 保留原有字段与控制台格式，新增 `meta`（Git 短 hash、UTC 时间、Python 版本、数据路径/行数/12 位 SHA256、配置白名单、generate/router/general 提示词 hash）、`cases`、`failures` 与 `route_stats`。`cases` 记录可选 id（数据行含 id 时记录）、问题、history 条数、期望/实际路由及来源、拒答/缓存/FAQ 标记、通过判定、错误类别和耗时；`matched_by` 记录实际 faq/cache/rag 回答路径，仅实际 rag 路由将来源未命中判为失败，正确 FAQ/cache 命中不要求 RAG 引用；旧 RAG 指标分母保持原有口径。`failures` 只含未通过题。`route_stats` 按实际 faq/cache/rag/general/fallback 分组统计题数、通过数和总耗时 P50/P90，异常单列 error。配置不导出 API key/SecretStr，base URL 仅记录 host，嵌入模型仅记录路径 basename；异常仅保存错误码与类别。报告仍包含原有问题和答案，请妥善保管；复现前需固定索引、FAQ、缓存状态及预热策略，详情见[指标解释](docs/电商RAG智能客服-开发文档.md#72-指标解释)。

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

## 本阶段新增能力速览

详细说明与命令见开发文档 [3.5](docs/电商RAG智能客服-开发文档.md#35-账号配额与服务器历史)、[5.4](docs/电商RAG智能客服-开发文档.md#54-cli-配置来源与上线前置步骤)、[5.5](docs/电商RAG智能客服-开发文档.md#55-商品数据层商品卡片与图片)、[7.4](docs/电商RAG智能客服-开发文档.md#74-ragas-两阶段离线评估)、[8.5](docs/电商RAG智能客服-开发文档.md#85-本机-caddy-部署当前)。

- **账号与历史**：用户名 + 密码登录；游客每天 5 次、账号每天 50 次（默认按 UTC+8 自然日，可由 `RATE_LIMIT_DAY_TZ_OFFSET_HOURS` 调整；FAQ/缓存命中也计次）；登录账号的对话保存在服务器；可在侧边栏注销账号。登录失败区分“账号不存在/密码错误”是已接受账号枚举风险的产品取舍，见 [账号与历史接口说明](docs/账号与历史接口说明.md)。同源请求自动放行，跨站前端须配置 `CORS_ORIGINS`（不允许 `*`）。
- **商品卡片**：上线前必须执行 `migrate_products` 和 `load_products`（上文第 3 步）。图片由 Caddy 在同源 `/images` 托管，接口字段见 [商品卡片接口说明](docs/商品卡片接口说明.md) 与 [frontend/PRODUCT_CARDS.md](frontend/PRODUCT_CARDS.md)。
- **知识库版本化**：`kb_migrate`（一次性，需停流量）→ `ingest.run --staged --yes` → `kb_switch --check` → `kb_switch --yes`，回滚为 `kb_switch --to kb_chunks_v0 --yes`，所有命令固定同一个 `--state-dir`。见 [知识库扩容与回滚](docs/知识库扩容与回滚.md)；FAQ 见 [FAQ维护与发布](docs/FAQ维护与发布.md)。
- **内容工具**：`ingest.content_check`（一致性检查）、`ingest.content_merge`（合并到暂存目录），见 [内容集成与一致性检查](docs/内容集成与一致性检查.md)。当前语料：`data/docs` 156 篇，`data/faq.csv` 128 条。
- **离线评估**：`run_eval` 三个评测集 + RAGAS 两阶段（collect 在主环境，score 在 `backend/.venv-eval`）。
- **知识图谱演示（可选）**：`ingest.graph_data` 生成商品供电、接口、包装、收纳、场景关系与 94 道多跳评测题，`ingest.graph_load` 导入 Neo4j（`docker compose --profile graph up -d neo4j`，需在 `.env` 设置 `NEO4J_PASSWORD`），`eval.graph_demo` 演示实体链接与 Cypher 查询。兼容结论只代表规格层面匹配；图谱暂未接入在线问答链路。见 [data/graph/README.md](data/graph/README.md)。

## 部署

本机 + 固定隧道（临时隧道不支持流式输出）。后端只监听 127.0.0.1，经本机反向代理托管 `frontend/dist` 并转发 `/api`，关闭缓存、压缩和代理缓冲，并配置真实 IP 头。详见开发文档第 8 节。

当前穿透服务与 Caddy 的本机部署使用 `sh deploy/start-local.sh` 启动后端（在仓库根目录执行，监听 127.0.0.1:8010，使用 `backend/.venv`）；前端 `cd frontend && npm run build` 后由 Caddy（8080）托管 `frontend/dist`、反代 `/api` 并托管 `/images`，启动命令见开发文档 8.5（仓库没有 `deploy/start-caddy.sh`）。脚本显式设定上述容量为 50，避免旧 `.env` 中的小值覆盖默认值；不修改 `.env`。由于现有隧道尚未透传可验证的真实 IP，该脚本暂时关闭共享 IP 桶，仅按会话限流，避免不同访客与压测共用回环地址的额度。session 不是可信身份，接入可信真实 IP 后应恢复 IP 限流。操作步骤见 [本机部署说明](docs/内网穿透与本机部署.md)。

## 常见问题

- **启动失败或一直预热**：先确认 `docker compose ps` 三项都 healthy、数据已入库、模型权重完整。
- **商品卡片不出现，日志只有 `product_cards_unavailable`**：没有建 products 表或没导入商品，执行“快速开始”第 3 步的两条商品命令后重启后端。
- **启动报 AUTH_SECRET 错误**：非调试模式必须配置非空 `AUTH_SECRET`。启动报 `IMAGE_BASE_URL` 错误：把它清空（不要填 `/images`）。
- **前端连不上**：后端 8010、前端 3000；检查 `VITE_MOCK` 是否关闭。
- **看不到链路**：需要前端入口（`?debug=1` 或 `VITE_SHOW_TRACE_TOGGLE=1`）、请求 `debug=true` 与后端 `ALLOW_TRACE=true` 同时满足。
- **返回 502/空答案**：多半是模型参数问题，先跑 `uv run python -m eval.llm_ping`。
- **变慢或频繁超时**：本机内存不足会让重排慢 10 倍；关闭无关应用、先预热。到模型服务的网络抖动见演示准备第六章。

已有用户名密码账号与服务器端历史，但没有个人订单查询或真实人工工单。对话原文保存在 MySQL，请自行管理访问与保留期限（`ingest.purge_chat_logs`）。

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
| `REMOTE_REFUSE_THRESHOLD` | 空 | [0,1] 内的有限浮点数，含边界；空时云端分数不用于拒答，拒答依赖路由 sensitive 与生成 NO_ANSWER；显式设置后最高分严格低于该值才低分拒答 |
| `REMOTE_RAG_CONTEXT_MIN_SCORE` | 空 | 远程重排的上下文过滤阈值，允许任意有限浮点数；空时保留全部 TopK，设置后仍至少保留最高分资料；本地及本地回退继续使用 `RAG_CONTEXT_MIN_SCORE` |

云端分数可能对相关和应拒答问题均接近 1.0，因此默认不按分数拒答，也不触发低分改写补救；无召回结果仍直接拒答。路由 sensitive 拦截他人隐私、医疗/疗效/法律承诺和提示词注入；生成器只能依据资料，有直接依据才回答，无依据或命中上述敏感请求时只输出 `NO_ANSWER`，由 pipeline 转为固定拒答文案。`REMOTE_RAG_CONTEXT_MIN_SCORE` 仅过滤生成上下文，不承担拒答判断。

远程调用失败时，`rrf` 使用原始 RRF 顺序和分数，仅在无结果时直接拒答，并跳过改写补救与相关度上下文过滤；生成器仍可输出 `NO_ANSWER`，trace 标记 `provider=fallback_rrf, fallback=rrf`。`local` 首次失败时才加载本地模型，并在线程池重排，使用本地 `REFUSE_THRESHOLD`；本地再失败则退 RRF。`error` 抛出脱敏的 AppError（50201/502、超时 50401/504；上游持续 429 为 SERVICE_BUSY 50302/503，与 LLM 客户端一致）。42901/429 仅表示本服务分钟频率限流。取消和整请求 deadline 不触发额外回退工作。

在 `backend/` 真实环境中依次执行（这些命令会调用服务）：

```bash
uv run python -m eval.rerank_ping
uv run python -m eval.check_retrieval --rerank-provider remote
uv run python -m eval.check_retrieval --rerank-provider local
```

先用 ping 核实 index、分数范围、返回顺序与 usage，再比较 Hit@1/Hit@5、每题排名/分数、拒答最高分与重排耗时。默认保持 `REMOTE_REFUSE_THRESHOLD` 为空；仅当实测分数能区分相关与应拒答问题时，才考虑显式设置。RAG 命中最低分与拒答最高分的中点仅供参考；分布重叠或全部接近 1.0 时不能用它作为有效拒答阈值。校准工具强制调用失败即报错，防止把回退分数用于远程阈值。`eval.run_eval` 的 rerank 耗时同样包含远程调用与回退。

接口每次最多 128 条候选，超过时分批；问题与每条候选最多 4096 字符，发送前截断。`RERANK_CANDIDATES`、child/parent 模式与既有 `RERANK_MAX_CHARS` 规则仍生效；远程不使用本地 batch size、token max length 或 FP16 设置。

官方文档未明确 index 从 0 还是 1 开始、分数取值范围、结果是否降序。请求始终设置 `return_documents=true`；index 构成完整 0 基双射（无重复、无越界、覆盖全部候选）时以 index 为准，回显文本被空白规整、截断等差异仅记一条诊断计数，日志不含问题、候选文本或 key。index 非法或缺项时按文本逐一匹配（重复文本依出现顺序各用一次）；仍无法一一对应或结果数量不完整视为 `bad_response`。分数须为有限数，不做变换，由客户端自行排序；ping 打印实测分数最小/最大值与返回顺序是否降序。

隐私：用户问题与检索候选文本会发送到智谱，和 LLM 调用处于同一信任边界。日志与诊断不记录问题、候选文本或密钥；供应商错误仅保留脱敏、截断的允许字段。离线测试全部使用 MockTransport，不发送真实请求。
