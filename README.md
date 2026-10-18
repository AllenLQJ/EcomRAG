# 电商 RAG 智能客服

面向 C 端顾客的 Python 3.11 / FastAPI / Pydantic v2 后端，依赖由 uv 管理。当前链路为限流 → 缓存 → FAQ → 一次 LLM 路由 → 通用对话 / RAG 检索 → 带引用生成 / 拒答兜底。聊天接口返回真实服务结果，离线测试全部注入假服务或假 LLM。

## 本地启动

前置条件：uv、Python 3.11、Docker 和 Compose v2（M1 开发机使用支持 Apple Silicon 的 Docker Desktop）。后端直接在本机运行。

1. 在仓库根目录创建 `.env`，使用下方模板，替换本地数据库的占位密码。LLM 服务 key 为空时缓存和 FAQ 仍可回答，进入模型调用则返回 50201「模型服务未配置」。完整环境变量示例保留在本文中。
2. 在根目录检查并启动依赖服务：

   ```bash
   docker compose config --quiet
   docker compose up -d
   ```

3. 安装依赖并启动后端：

   ```bash
   cd backend
   uv sync --python 3.11
   uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
   ```

4. 在 `backend/` 运行测试：

   ```bash
   uv run --no-sync pytest
   ```

首次同步依赖、拉取镜像需要联网；离线时需要已缓存的 Python、依赖和镜像。上述启动命令供开发者执行，脚手架交付期间未执行容器启动。

依赖在 `pyproject.toml` 声明，本次未安装。已有 Python 3.11 测试环境可用 `uv run --no-sync pytest`，不会同步依赖。模块导入不要求安装模型、数据库、Redis、BM25、分词或文档解析 SDK；真实装配的预热和请求需要相应依赖、本地模型及运行中的数据库。

Compose 包含 Milvus Standalone 2.5.10、MySQL 8.0、Redis 7.4。Milvus 使用官方 standalone_embed 方案，内置 etcd 并使用本地存储，不再依赖外部存储服务；`backend/milvus/embedEtcd.yaml` 只读挂载为 `/milvus/configs/embedEtcd.yaml`，etcd 数据与 Milvus 数据统一保存在 `./volumes/milvus/`。全部服务使用 `${DOCKER_PLATFORM:-linux/arm64}`：M1 默认使用 `linux/arm64`，x86 VPS 在根目录 `.env` 中设置 `DOCKER_PLATFORM=linux/amd64`。镜像架构支持仍需按下方「待验收」核实。所有发布端口只绑定 `127.0.0.1`，内置 etcd 的 2379 不发布到宿主机。数据保存在 `./volumes/`。MySQL 仅在数据目录为空的首次启动时自动执行 `backend/sql/init.sql`，创建 `faq_pairs` 和 `chat_logs`；后续修改 SQL 不会自动迁移已有数据库。

## 环境变量

配置统一由 `backend/app/config.py` 读取仓库根目录 `.env`，进程环境变量优先；从 `backend/` 启动同样有效。导入 `app.main` 不创建应用、不读取 `.env`；Uvicorn 通过 `--factory` 调用 `create_app()` 时才加载配置。测试调用 `create_app(settings, services=假服务)`，以 `Settings(_env_file=None, ...)` 禁止读取环境文件，并在每项测试开始前清除继承的应用配置环境变量；日志统一写入临时目录。专门测试环境变量读取的用例自行设置假值。Compose 从根目录 `.env` 读取凭据及平台配置。以下全部为示例或明显占位值，没有真实密钥：

配置名现统一为 `LLM_API_KEY`、`LLM_BASE_URL`。旧名 `QWEN_API_KEY`、`QWEN_BASE_URL` 暂作为兼容别名；新旧同时存在时新名优先（新 key 为空也优先）。只使用旧名时，加载配置会记录弃用 WARNING，仅包含配置名。请把本机旧配置迁移到新名。

```dotenv
DOCKER_PLATFORM=linux/arm64
LLM_API_KEY=
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ROUTER_MODEL=qwen-turbo
GENERATE_MODEL=qwen-plus
ROUTER_MAX_TOKENS=64
GENERATE_MAX_TOKENS=600
LLM_EXTRA_BODY=
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=ecomrag
MYSQL_USER=ecomrag
MYSQL_PASSWORD=CHANGE_ME_LOCAL_MYSQL_PASSWORD
MYSQL_ROOT_PASSWORD=CHANGE_ME_LOCAL_ROOT_PASSWORD
REDIS_URL=redis://127.0.0.1:6379/0
MILVUS_URI=http://127.0.0.1:19530
MILVUS_TIMEOUT_S=10
BGE_M3_PATH=models/bge-m3
RERANKER_PATH=models/bge-reranker-v2-m3
EMBED_BATCH_SIZE=16
CHILD_CHUNK_SIZE=300
CHILD_CHUNK_OVERLAP=50
PARENT_MAX_CHARS=1200
FAQ_THRESHOLD=0.9
FAQ_CACHE_TTL_S=86400
RERANK_ENABLED=true
RERANK_CANDIDATES=10
RERANK_MAX_LENGTH=512
RERANK_BATCH_SIZE=16
RERANK_MAX_CHARS=600
RERANK_TEXT_MODE=child
RERANK_FP16=false
RERANK_TOP_K=5
SPECULATIVE_RETRIEVAL=true
REFUSE_THRESHOLD=0.5
LLM_TIMEOUT_S=20
LLM_MAX_CONCURRENCY=4
LLM_MAX_RETRIES=1
LLM_RETRY_BACKOFF_S=0.5
WARMUP_ON_START=true
ALLOW_TRACE=false
TRUST_PROXY_HEADERS=false
REQUEST_TIMEOUT_S=40
RATE_LIMIT_PER_MIN=10
RATE_LIMIT_PER_DAY=200
HEALTH_TIMEOUT_S=1
LOG_LEVEL=INFO
LOG_DIR=logs
LOG_BACKUP_DAYS=14
LOG_TO_CONSOLE=true
CHAT_LOG_ENABLED=true
CORS_ORIGINS=["http://localhost:3000","http://127.0.0.1:3000"]
DEBUG=false
```

| 参数 | 用途 |
| --- | --- |
| `DOCKER_PLATFORM` | 仅供 Compose 使用；M1 为 `linux/arm64`，x86 VPS 为 `linux/amd64` |
| `LLM_API_KEY`、`LLM_BASE_URL` | 任意 OpenAI 兼容接口的凭据和地址；默认地址为阿里云国内站，可改成其他兼容服务；客户端追加 `/chat/completions` |
| `ROUTER_MODEL`、`GENERATE_MODEL` | 路由、生成模型名称；以所用 LLM 服务实际支持的模型名为准 |
| `ROUTER_MAX_TOKENS`、`GENERATE_MAX_TOKENS` | 路由输出上限默认 64，RAG 生成与通用对话输出上限默认 600；均须大于 0。RAG 提示词要求通常不超过 150 字、最多 5 条，不重复问题或寒暄，保留事实引用 |
| `LLM_EXTRA_BODY` | JSON 字符串，默认空（不添加参数）；解析为对象后合并进每次请求，用于关闭思考模式或传供应商特有参数；非法 JSON/非对象在配置阶段报错 |
| `MYSQL_*` | 本机数据库地址、库名、用户及密码；root 密码供容器初始化 |
| `REDIS_URL`、`MILVUS_URI` | 本机依赖服务地址 |
| `MILVUS_TIMEOUT_S` | Milvus 检索客户端建连及每次 `hybrid_search` 的超时秒数，默认 10；须大于 0，运行入口加载配置后注入检索器 |
| `BGE_M3_PATH` | 已准备好的本地 BGE-M3 目录；不存在或文件不完整时失败，不自动下载 |
| `RERANKER_PATH` | 本地 BAAI/bge-reranker-v2-m3 目录；首次重排才加载，不自动下载 |
| `EMBED_BATCH_SIZE` | 每次编码及 upsert 的子块数量，默认 16 |
| `CHILD_CHUNK_SIZE`、`CHILD_CHUNK_OVERLAP` | 子块字符上限及正文重叠字符数；长度上限包含文档标题前缀 |
| `PARENT_MAX_CHARS` | 父块最大字符数，超长小节继续按标点切分 |
| `FAQ_THRESHOLD` | FAQ 稠密余弦相似度阈值，默认 0.9；实测误命中 0.852 / 0.806 / 0.885，最高 0.885；阶段 4 用测试集校准 |
| `FAQ_CACHE_TTL_S` | FAQ Redis 精确缓存 TTL，默认 86400 秒（24 小时） |
| `REFUSE_THRESHOLD` | 归一化 rerank 最高分低于此值时建议拒答，默认 0.5，无检索结果也拒答；历史 parent 分数为 RAG 最低 0.894、拒答最高 0.261。child 输入与 fp16 会改变分数分布，必须用 check_retrieval 重新校准，不能直接沿用历史依据 |
| `RERANK_ENABLED`、`RERANK_CANDIDATES`、`RERANK_TOP_K` | 检索重排开关、去重后候选上限（10）、结果数量（5）；`retrieve` 的显式 `top_k` 优先。候选从 20 降为 10 是性能起点，先按剖析工具结果再微调并复核 Hit@1 |
| `RERANK_MAX_LENGTH` | 送入 `FlagReranker.compute_score` 的 token 上限，默认 512 |
| `RERANK_BATCH_SIZE` | 重排批大小，默认 16；先按文本长度排序分批，得分还原为原始候选顺序 |
| `RERANK_MAX_CHARS` | 仅 parent 模式下的重排父块字符上限，默认 600；0 不截断。child 模式不使用此限制；生成始终使用完整父块 |
| `RERANK_TEXT_MODE` | child / parent，默认 child；child 重排已带标题的最佳命中子块（snippet），parent 重排父块正文 |
| `RERANK_FP16` | 默认 false，仅在 mps 上允许半精度；cpu 始终 fp32。启用前对比耗时、Top1 与分数差异，应用中的加载/推理错误仍会正常上抛 |
| `SPECULATIVE_RETRIEVAL` | 默认 true；FAQ 未命中且 history 为空时并行路由和原问题检索重排；false 或多轮 history 时等路由改写后检索 |
| `LLM_TIMEOUT_S`、`REQUEST_TIMEOUT_S` | 单次模型调用和整请求超时，默认 20 / 40 秒；排队等待计入整请求超时 |
| `LLM_MAX_CONCURRENCY` | 全局模型调用并发上限，默认 4，超出时排队 |
| `LLM_MAX_RETRIES`、`LLM_RETRY_BACKOFF_S` | 默认重试 1 次（总共至多 2 次尝试），首次退避 0.5 秒，后续按 2 倍递增；可设重试次数 0 关闭。重试与退避始终持有同一并发名额，均受 REQUEST_TIMEOUT_S 约束 |
| `WARMUP_ON_START` | 默认 true，在 lifespan 启动时预热 FAQ、BM25、问句向量、本地模型和 Milvus；false 延迟初始化 |
| `ALLOW_TRACE` | 默认 false；请求 debug=true 且此配置为 true 才返回内部耗时与分数；演示环境在 `.env` 设 true |
| `TRUST_PROXY_HEADERS` | 默认 false；启用才信任 X-Forwarded-For 首个地址；受控 Caddy 反代部署时开启并确保反代覆盖伪造头 |
| `RATE_LIMIT_PER_MIN`、`RATE_LIMIT_PER_DAY` | IP 与 session_id 分别限制，默认每分钟 10、每日 200 |
| `HEALTH_TIMEOUT_S` | 每项 TCP 探测的总超时秒数，连接与关闭共用；已连通后关闭失败或超时仍为 `up` |
| `LOG_LEVEL` | 应用日志级别，默认 INFO（支持 DEBUG / INFO / WARNING / ERROR / CRITICAL） |
| `LOG_DIR` | 日志目录，默认启动工作目录下的 logs；从 backend 启动为 backend/logs |
| `LOG_BACKUP_DAYS` | 午夜滚动日志保留份数，默认 14；0 不自动删除历史文件 |
| `LOG_TO_CONSOLE` | 默认 true，同时输出控制台日志 |
| `CHAT_LOG_ENABLED` | 默认 true，在响应后台任务中向 MySQL 写入对话原文；false 不写入也不启动补列 |
| `CORS_ORIGINS` | 允许跨域的来源，格式为 JSON 字符串数组 |
| `DEBUG` | 默认关闭 `/docs` 和 `/openapi.json`；请求体 `debug` 与 `ALLOW_TRACE` 共同控制 trace |

## 接口与错误

- `GET /health`：返回 `mysql`、`redis`、`milvus` 的 `up/down`，仅检测 TCP；返回 `llm` 的 `configured/unconfigured`，只检查 key 是否非空。依赖断开仍返回 HTTP 200，不代表认证或协议正常。此接口不发起 LLM 请求。
- `POST /api/v1/chat`：接受 `session_id`、1–500 字的 `message`、可选 `debug`。返回 UUID `request_id`、原 `session_id`、真实 `answer`、`route=cache|faq|rag|general|fallback`、`strategy`、带编号的 `sources`、`is_mock=false`。RAG 与检索不足的拒答策略为 direct，敏感意图拒答和其他路由为 null。请求 `debug=true` 且 `ALLOW_TRACE=true` 才附带 trace，否则为 `null`。纯空白消息也会被拒绝。
- 响应头 `X-Request-ID` 与响应体对应。请求参数校验失败返回 HTTP 400 和 `{"code":40001,"message":"请求参数非法","request_id":"..."}`；`AppError` 转换成同样结构的错误体。404、405 分别返回 `40401`、`40501`，其他 HTTP 客户端错误返回 `40000`，均保留原 HTTP 状态；未捕获异常返回 `50001`，仅记录清洗后的异常类型与堆栈位置，DEBUG 下也不回显可能包含原文的异常。

| 错误码 | 含义 | HTTP 状态 |
| --- | --- | --- |
| 40000 | 其他 HTTP 客户端错误 | 原 HTTP 4xx 状态 |
| 40001 | 请求参数校验失败 | 400 |
| 40401 | 路由或资源不存在 | 404 |
| 40501 | 请求方法不允许 | 405 |
| 42901 | 限流 | 429 |
| 50001 | 内部错误 | 500 |
| 50201 | LLM 调用失败 | 502 |
| 50301 | Milvus / MySQL 不可用 | 503 |
| 50401 | 超时 | 504 |

入口按 IP 和 session_id 独立累计，任一维度超限返回 HTTP 429 / 42901「提问有点快，休息一分钟再问吧」。IP 默认取 request.client.host。Redis 使用 INCR + EXPIRE 事务，不可用时静默退化为进程内计数（各进程独立，重启清空）。固定分钟窗口和 UTC 自然日窗口，超限请求也计数，每日超限需等次日。

模型上游 429 返回 42901「请求太频繁，请稍后再试」；超时返回 50401；其他网络/HTTP 错误及坏响应返回 50201，不回显密钥、用户原文或提示词。MySQL / Milvus 不可用返回 50301「服务暂时不可用，请稍后再试」；其他异常保持上抛，由 API 返回统一 50001。Redis FAQ 缓存故障仍跳过。

LLM 客户端默认对 ConnectError、连接重置/中断类网络错误、HTTP 5xx 和 429 额外尝试一次；不重试超时、其余 4xx、重定向或响应解析错误。退避默认 0.5 秒，次数增大时按指数递增。一次调用的全部尝试和退避占用同一并发名额；每次传输仍受 LLM_TIMEOUT_S 限制，整请求由 REQUEST_TIMEOUT_S 限制，取消会中断退避。每次重试记录 `llm_retry` WARNING，仅包含 category、HTTP 状态、重试序号、脱敏后的 host/model；最终失败沿用现有错误映射与诊断。

## 阶段 1：样例数据与入库

`data/` 为虚构店铺「栖木」的虚构样例：18 份 Markdown 文档、30 条 FAQ、24 条评测问题。知识主题包括售后、配送、支付与发票、会员与优惠，以及四款商品。听岚 S1 单次续航 8 小时、含盒 32 小时；听岚 S2 关闭主动降噪时为 10/40 小时，开启后为 6/24 小时。数据不对应真实品牌或真实服务承诺。

评测集包含 FAQ 6 条、RAG 8 条、general 5 条、fallback 5 条。阶段 2D 在原 20 题后新增两道提示词注入/泄露提示词请求（fallback）和两道店铺无关的代码/天气请求（general）。`expected_doc` 在 RAG 行中是文档文件名，其他行是 `null`；FAQ 与知识文档事实保持不变。

在 `backend/` 执行以下命令；相对数据路径先按当前目录查找，再回退到仓库根目录，因此 `data/docs` 不需要改成 `../data/docs`：

```bash
python -m ingest.run --dir data/docs --dry-run
```

dry-run 先通过 `app.config.get_settings()` 加载配置，再只读文档与切块，不导入模型或 Milvus SDK，不连接任何服务。这样可使用与实际入库一致的 `.env` 切块参数。MD/TXT 的读取与切块核心仅需标准库；实际读取 PDF 需要 PyMuPDF，读取 DOCX 需要 python-docx，缺包会给出说明。扫描 PDF 不执行 OCR。默认输出 JSON 统计 `documents`（文档数）、`parent_chunks`（父块数）、`child_chunks`（子块数）、`elapsed_s`（耗时秒数）及 `dry_run`。

准备好本地模型文件、已安装的编码/存储依赖和正在运行的 Milvus 后，实际入库命令为：

```bash
python -m ingest.run --dir data/docs
python -m ingest.load_faq data/faq.csv
```

文档入库、dry-run 与 FAQ 导入 CLI 均默认使用 `app.config.get_settings()`，读取仓库根目录 `.env`；进程环境变量优先。`MYSQL_*`（含密码）、`MILVUS_URI`、`BGE_M3_PATH` 与切块参数无需再次手动导出。两个 `main(argv, *, settings=None)` 也支持测试显式注入 `Settings(_env_file=None, ...)`，注入后不调用配置加载器、不读取 `.env`。纯标准库测试可注入具备相同字段的配置替身。编码器与存储类不自行读取配置，由入库入口传入已加载的模型路径、批大小和服务地址。

默认模型路径是仓库内的 `models/bge-m3`。编码器在第一次编码时才加载本地模型，同时强制底层模型库离线运行；有可用 MPS 时显式指定 `devices="mps"`，否则指定 `devices="cpu"`，两者均使用 `use_fp16=False`。一次 encode 同时请求 `return_dense=True`、`return_sparse=True`，不加载重排模型。

稀疏部分取 FlagEmbedding 的 `lexical_weights`。其原生键是 tokenizer 词表 ID 的十进制字符串，如 `"42"`，用 `int` 恢复原 ID；若适配器返回实际 token 文本，则使用同一模型的 `tokenizer.convert_tokens_to_ids` 映射，未知 token 明确报错。不会将真实编码器的 token 文本哈希成假 ID，也不调用会把 ID 反向解码为文本的 `convert_id_to_token`。写入 pymilvus 的格式为 `{整数词表ID: 浮点权重}`。

父块按 Markdown 的 `##` 二级标题整节划分，文首 `#` 标题及介绍并入首节，`###` 及更深标题留在节内，代码围栏中的标题不作为边界；没有二级标题时保留整篇，再按上限处理。其他格式按空行段落划分；父块超过 1200 字再切。子块默认最多 300 字（包含标题），正文重叠 50 字，优先在中文句尾切分，必要时按字符硬切。切分使用标准库实现，未在模块导入时加载 langchain 或 torch。分类按 `after_sales_`、`logistics_`、`payment_`、`product_`、`promotion_` 文件名前缀推断，也可由 loader 元数据覆盖；无匹配前缀默认 `product`。

样例自查发现原分节只有 110–205 字，300 字子块预算不会强行拆短内容。现已把相关细则归入整节并扩写使用、材料核对和处理说明，保留原事实。默认参数下的真实统计如下，长度单位为 Unicode 字符（含 Markdown 标记）；父块去重统计，子块长度包含文档标题前缀，均不同于 Milvus 的 UTF-8 字节限制：

| 样例版本 | 父块数 | 子块数 | 父块最小/中位/最大 | 子块最小/中位/最大 |
| --- | --- | --- | --- | --- |
| 修订前 | 72 | 72 | 110 / 135 / 205 | 125 / 150 / 223 |
| 阶段 1 修订后 | 36 | 75 | 422 / 464 / 516 | 93 / 276 / 299 |
| 补充划痕退货条件后（当前） | 37 | 76 | 196 / 464 / 516 | 93 / 276.5 / 299 |

当前 18 份文档共 37 个父块、76 个子块，按默认父块 1200、子块 300、重叠 50 实测。其中 36 个父块位于 400–1000 字并产生 2–3 个子块；新增屏幕划痕与外观瑕疵小节为 196 字，保留为一个完整子块。父块提供完整节的上下文，子块用于检索。

文档 ID 来自源文件相对路径的稳定摘要；`chunk_id={doc_id}#{parent_idx}#{child_idx}`，每个子块携带父块原文、标题、来源及分类。Milvus 集合为 `kb_chunks`，dense 使用 HNSW＋COSINE，sparse 使用 SPARSE_INVERTED_INDEX＋IP。主键 upsert 保证同一输入重复入库不增加行；已有同名集合须具备兼容 schema，不会自动迁移。文档缩短或删除后，不会自动清除旧子块，需要另行清理或重建集合。文本写入前按 UTF-8 字节校验 Milvus VARCHAR 限制。

FAQ 导入先校验 CSV 并按 question 去重，再通过 MySQL 命名锁串行化本导入器的更新/插入，整批提交或回滚，兼容现有无 question 唯一索引的表。重复导入会更新答案和分类，不新增相同问题；该锁不约束其他写入端，也不清理导入前已经存在的重复行，不自动修改表结构。

新增离线单元测试也可只使用标准库执行（pytest 同样会收集这些测试）：

```bash
python -m unittest discover -s tests -p 'test_ingest*.py' -v
```

测试使用 FakeEmbedder、假的模型接口及可注入 Milvus/MySQL 客户端；不加载真实模型、不连接数据库，也不读取 `.env`。聊天接口已通过阶段 2B 串联这些服务。

## 阶段 2A：独立 FAQ 与检索服务

本阶段提供可注入依赖的同步 `FaqService.faq_lookup(question)` 和 `Retriever.retrieve(queries, rerank_query, top_k=5)`。`contracts.py` 中相应契约为同步签名；阶段 2B 的异步 API 在线程池中运行这些同步数据库及模型操作。

FAQ 启动时通过 `FaqRepo.load_enabled()` 以参数化 SQL 读取 MySQL 中 `enabled=1` 的问答，用 jieba 和 rank-bm25 构建内存索引，并一次性预计算 FAQ 问句的稠密向量。`FaqService.reload()` 完整重建索引与向量后替换快照并清理 FAQ 缓存；单独的 `Bm25Index.reload()` 仅刷新 BM25 索引。查询先查 Redis，再对 BM25 Top5 问句计算稠密余弦分数，最高分达到当前阈值则返回 `FaqHit` 并写缓存。缓存保存 score，读取时若 score 小于当前阈值则视为 miss 并尽力删除；无 score 的旧格式、坏 JSON 或非法字段也视为 miss。阈值从 0.8 升到 0.9 后旧误命中无需等 24h 自动失效。缓存 key 为 `faq:cache:{md5(normalize(question))}`：全角 ASCII/空格转半角、小写、去首尾空白与标点，保留内部空白和标点。Redis 不可用时只记录固定 warning 并跳过，序列化等程序错误继续上抛。reload 与清理命令只扫描 `faq:cache:*`，保留限流计数。

`FaqHit` 序列化字段包含 `faq_id`、`question`、`answer`、`score`、`category`、`source`（`cache` 或 `faq`）；兼容旧 `id` 输入与读取。缓存通过 `model_dump_json()` / `model_validate_json()` 保存和读取完整字段，保留 `category`，读取后仅把 `source` 标记为 `cache`。`hit.top_score` 等于命中分数；`service.top_score` 为当前调用上下文的最高分，未达阈值时仍可用于 trace，无候选时为 `None`。依赖与阈值均通过构造函数注入，不在服务内部读取 `.env`。

检索对每个查询调用 Milvus `hybrid_search`：dense Top20（COSINE）与 sparse Top20（IP，整数词表 ID 字典），`RRFRanker(k=60)` 融合，最多返回 40 个子块。`retrieve_candidates(queries)` 合并并按 `parent_id` 去重，保留最高 RRF 分的子块作为 snippet；`rerank_candidates(rerank_query, candidates, top_k)` 按 RRF 分取默认 10 个候选重排，`retrieve()` 依次组合两步。重排默认使用原始问题和已带文档标题的最佳命中子块（RERANK_TEXT_MODE=child）；parent 模式才使用父块前 600 字（0 不截断），两种模式的 token 上限均为 512；按文本长度分批，以 `torch.inference_mode` 推理并恢复候选的分数顺序。分数仍为归一化 rerank 分；关闭重排时不裁候选，保留 RRF 分与排序。最终按显式 `top_k` 截断。`RetrievedDoc` 的完整 `content`、snippet、来源不会因重排截断被修改。

真实验证前，请自行准备已声明的依赖、本地模型和完成入库的数据库。在 `backend/` 执行：

```bash
python -m eval.search_demo "怎么申请退货？"
python -m eval.faq_demo "怎么申请退货？"
python -m eval.check_retrieval
python -m eval.check_retrieval --rerank-text-mode child --candidates 10
python -m eval.check_retrieval --rerank-text-mode parent --candidates 10
python -m eval.profile_rerank "怎么申请退货？"
```

两个演示脚本在运行时调用 `get_settings()`，从任意启动目录解析本地模型路径。search demo 分别展示关闭/开启重排的 Top5（固定对照两条路径，覆盖 `RERANK_ENABLED`，每条显示标题、score、snippet 前 60 字）；faq demo 显示结果、来源与最高分。重排器优先 MPS，否则 CPU，默认 fp32；RERANK_FP16=true 仅在 MPS 上启用 fp16，CPU 始终 fp32；模型仅延迟加载本地目录并强制离线。脚本仅供手动真实环境验证，单元测试只注入假对象。

`eval.profile_rerank "问题"` 使用运行配置，对同一 RRF 候选池比较候选 20/10/6 × 父块字符上限 0/600/300 × batch 8/16/32，共 27 组。此网格固定使用 parent/fp32，以便和历史剖析对照。每组跑两次，丢弃首次作为预热，只报告第二次。表格包含实际设备/dtype、候选上限与实际数量、截断后问题+父块字符长度的最小/中位/最大、tokenize/前向/总耗时，以及 Top1 文档是否与「候选20、不做字符截断、batch8」一致。前向用模型 hooks 并同步 MPS 计时；总耗时还包含排序、设备传输等开销。token 上限仍取 RERANK_MAX_LENGTH，工具不比较不同 token 上限。

网格后新增 fp32/fp16 对比：取 RERANK_CANDIDATES 指定的同一批候选，按当前 RERANK_TEXT_MODE、RERANK_MAX_CHARS、RERANK_BATCH_SIZE 准备完全相同的文本，分别执行两次并丢弃首次；输出两种精度的前向耗时、Top1 是否一致、分数平均绝对差和最大差。CPU 会明确跳过 fp16；MPS fp16 加载或推理失败时仅显示异常类型和跳过提示，继续保留 fp32 结果。此对比不改变应用默认精度，也不自动修改配置。先根据剖析结果微调，再用 `eval.check_retrieval` 与完整 `eval.run_eval` 确认 Hit@1、引用和路由；单道题 Top1 一致不能证明测试集质量不变。

`eval.check_retrieval` 在运行时调用 `get_settings()`，默认读取 `data/eval.jsonl`（可用 `--eval-file` 替换）。省略 `--rerank-text-mode` 时依次对比 child、parent；指定 child 或 parent 时只测该模式，`--candidates N` 可覆盖候选数量（必须大于 0）。两种模式复用同一精度模型并先预热一次，均固定开启 rerank。每题打印 expected_doc 的 Top5 命中、排名、归一化分和单次重排耗时 rerank_ms（不含召回），分别汇总 Hit@1/Hit@5；fallback 打印最高分、拒答阈值比较与重排耗时。替身若只提供旧 retrieve 接口则无法拆分计时，显示 `-`。此脚本不执行 LLM 路由或生成。REFUSE_THRESHOLD 默认仍是 0.5，但输入从 parent 改成 child 后分数分布会变化，需按新的 RAG 与拒答样本重新校准；启用 fp16 后也应复核。

本次新增二级小节改变了退货文档的父块编号。真实验证前应清理旧知识集合或重建 `kb_chunks` 后重新入库，再运行评测，避免旧子块残留；本次没有操作真实数据库或模型。

## 阶段 2B：完整聊天链路与评测

`create_app(settings, services=假服务)` 支持离线注入，不传 services 时使用真实装配。全进程复用本地 embedder/reranker，锁保护首次加载和模型调用；同步 FAQ、数据库和检索在线程池执行。关闭 WARMUP_ON_START 后，首次请求延迟初始化 FAQ 和模型。预热失败会阻止应用就绪，须先准备本地依赖。同步工作交给线程后无法强制中断，但请求到时会返回超时，关闭服务时等待线程释放连接。

缓存/FAQ 命中直接返回 FAQ 来源；未命中仅调用一次 router，分为 general、knowledge、sensitive，优先级为 sensitive > knowledge > general。路由 JSON 非法时降级为 knowledge/direct/原问题。配送范围、能否寄往某地等所有店铺具体问题均归 knowledge，问法奇怪或答案可能是不支持也要检索。默认同时启动 router 与原问题的召回+线程池重排；knowledge 且 history 为空时直接使用投机结果，不再用 rewritten_query 重新检索。关闭 SPECULATIVE_RETRIEVAL 或 history 非空时保持顺序路径：改写问题检索、原问题重排。无资料或最高分严格低于 REFUSE_THRESHOLD 时固定拒答，不调用生成模型，等于阈值时允许生成。

索要他人隐私、要求医疗/疗效/法律结论或承诺、要求泄露提示词或忽略规则等归 sensitive；直接返回 fallback、sources=[]、strategy=null，固定回答「抱歉，这类问题我无法回答。涉及他人隐私、医疗效果或超出店铺规则的内容，建议联系人工客服。」，不调用生成。general/sensitive 会取消等待或忽略已启动的投机任务，其异常仅记固定 DEBUG，不影响响应，也不把投机 trace 混入正常阶段。同步模型线程无法强制停止，可能继续占用模型锁直到完成；关闭投机配置可以避免这部分额外工作。general 仅用于问候、自我介绍、闲聊或店铺无关的日常话题；通用提示词要求对具体店铺事实不凭记忆回答，并对代码、作业、天气等请求礼貌说明专注店铺咨询，不展开。

RAG 关键路径由 router + retrieve/rerank + generate 缩短为 max(router, retrieve/rerank) + generate；是否达到约 10 秒需实测。路由提示词已精简，ROUTER_MAX_TOKENS 默认 64；可通过 ROUTER_MODEL 换用更快模型，并用 `eval.run_eval --output ...` 对比分类准确率、错误和耗时，特别检查 JSON 长度不足导致的降级。

hyde/subquery/stepback 暂时降级 direct。生成使用父块，清除越界引用，只返回实际引用的来源；无有效引用时列出全部资料。提示词在 `backend/app/llm/prompts/*.md`，资料与用户输入转义后放入明确分隔块；路由示例不照抄评测题，实际模型分类效果仍需开发者用更新后的 24 题验证。

在 `backend/` 手动运行（真实装配，运行时调用 get_settings）：

```bash
python -m eval.chat_demo "签收后发现屏幕有划痕可以退吗？"
python -m eval.run_eval
python -m eval.run_eval --eval-file data/eval.jsonl
python -m eval.run_eval --output logs/eval-report.json
python -m eval.llm_ping
```

真实调用 LLM 服务 需要在 `.env` 配置 `LLM_API_KEY`、`LLM_BASE_URL`、`ROUTER_MODEL`、`GENERATE_MODEL`，由开发者在本机运行验证。本次实现不联网、不读取 `.env`，未验证 LLM 服务 或真实数据库/本地模型。

支持任意 OpenAI 兼容接口，默认地址为阿里云国内站（可通过 `LLM_BASE_URL` 改成其他兼容服务）；模型名以所用 LLM 服务实际支持的为准。使用第三方中转服务时，API key 与对话内容会经过该服务，上线前需评估其可靠性与数据处理条款。

演示与评测请求均带 debug=true；API 与 chat_demo 返回的 trace 仍由 ALLOW_TRACE 控制。run_eval 单独收集内部 trace，无论 ALLOW_TRACE 为何都统计耗时，报告 trace_enabled=true 和 api_trace_allowed（配置原值），不改变 API 对外开关。trace 记录 cache(hit)、faq(hit/top_score)、router(intent)、retrieve(candidates)、rerank(kept)、generate 和整个 pipeline 的 total；使用投机结果时 retrieve/rerank 标记 speculative=true。并行阶段独立计时，不能相加当作总耗时；total_ms 从评测调用的实际起止计算，包含失败请求。

run_eval 终端打印总题数、路由准确率、RAG sources 命中率、各阶段均值与 p50/p90、总耗时 p50/p90、错误分布（例如 `50201 ×14`）和路由错误题目（问题、期望、实际路由）。`--output 路径` 保存完整 JSON，results 保留每一道题，包含 answer、sources（doc_id/title/score）、total_ms、stage_ms、error_code（成功为 null）、is_error、判定及引用命中；失败行的答案与来源为空，保留出错前已完成阶段的耗时。stage_mean_ms、stage_p50_ms、stage_p90_ms 仅统计实际执行的阶段，包含失败前已完成的阶段；total_p50_ms、total_p90_ms 包含所有请求，空样本为 null，分位数采用排序后线性插值；completed 只统计成功完成的题目。错误分布也存入 error_distribution。不设硬门槛。expected_route=cache 接受 cache 或首次运行的 faq；错误计入总数并视为路由不正确，RAG 命中率的分母包含失败的 RAG 题。资料按入库稳定 doc_id 匹配。脚本不清理 Redis，重复评测可能把 expected_route=faq 变成 cache，需结合缓存状态解读。

llm_ping 不依赖数据库或本地模型，只对 ROUTER_MODEL、GENERATE_MODEL 各发送一次「回复ok」请求，max_tokens 固定为 64。它通过 get_settings 加载运行配置，打印目标 host、模型名、key 是否配置及长度；成功时打印耗时与回复前 20 字，失败时打印脱敏诊断，任一失败以非零退出。LLM 的用户错误文案保持通用；应用 WARNING 另记 category（timeout/http_status/network/bad_response/truncated/unconfigured）、HTTP 状态、异常类型、供应商 error.code/error.message（各最多 200 字）、host、model。密钥、已知消息和扩展参数值的回显会被替换，不记录请求体、任意响应 body 或 URL 路径/查询参数。

推理模型可将 reasoning_content/reasoning 与最终 content 分开。客户端只返回最终 content：字符串直接使用，分段列表只拼接 text/output_text 文本段，忽略推理段和图片段。bad_response/truncated 诊断增加 response_structure：choices_count、finish_reason、content_type、content_empty、has_reasoning_content、has_reasoning，以及 usage 中的 prompt/completion/reasoning token 数；不记录任何最终或推理内容文本，缺失的计数为 null，未知 finish_reason 仅记 unknown。reasoning token 数支持 completion_tokens_details.reasoning_tokens 或 usage.reasoning_tokens。

空最终内容且 finish_reason=length 时返回 HTTP 502 / 50201，用户文案仍通用，诊断 category=truncated、reason 为「输出被长度限制截断，可调大 max_tokens 或关闭思考模式」。content 非空时仍返回最终文本，不会用推理内容代替答案；非空但不完整的路由 JSON 仍按原规则降级 knowledge。推理是否占用了输出额度须用供应商返回的 usage 与 finish_reason 确认，不能仅根据 HTTP 200 或耗时推断。

例如服务支持时，可设置 `LLM_EXTRA_BODY='{"thinking":{"type":"disabled"}}'` 关闭思考模式。该配置用于关闭推理模型的思考模式或传入供应商特有参数，具体参数名以供应商文档为准。只记录 extra_body_keys 顶层键名，不记录值；model/messages/temperature/max_tokens 及路由 JSON 格式等核心调用参数优先于同名扩展参数。使用 GLM 等模型时，应先由开发者在本机运行 llm_ping 验证供应商参数与输出结构，再运行完整评测。本次未调用真实服务。

## 阶段 2C：缓存维护、应用日志与对话记录

默认 FAQ_THRESHOLD 已升至 0.9，依据为真实评测中误命中最高分 0.885；这仍是暂定值，阶段 4 用测试集校准。已有 `.env` 的显式 0.8 会覆盖代码默认值，开发者需要自行改为 0.9。在 `backend/` 可执行：

```bash
python -m ingest.clear_cache
python -m ingest.migrate_db
python -m eval.chat_logs_view --route rag --session demo --since 2h --limit 20
python -m eval.chat_logs_view --since 1d
python -m eval.chat_logs_view --full 123
python -m ingest.purge_chat_logs --days 30
python -m ingest.purge_chat_logs --days 30 --yes
```

clear_cache 用 SCAN 分批删除 FAQ key，输出实际删除数，Redis 不可用则非零退出。migrate_db 用 information_schema 检查后补充 sources_json JSON NULL、error_code INT NULL，并用命名锁避免并发迁移；MySQL 8.0 不使用 ADD COLUMN IF NOT EXISTS。新库 init.sql 已含两列；已有数据库应先执行 migrate_db。应用开启启动预热且 CHAT_LOG_ENABLED=true 时也补列一次，失败只记 warning，不阻止其他预热。其他模型/FAQ 基础设施的启动失败仍会阻止就绪。

create_app 初始化统一日志：默认 `backend/logs/app.log`（从 backend 启动），UTC 午夜滚动，默认保留 14 份，目录已被 Git 忽略。格式包含时间、级别、logger 名、request_id 和消息；request_id 用 ContextVar 隔离并发请求。request_done 记录 method、path、status、ms，不记录查询参数、请求/响应体或 IP。chat_done 记录 session_id、route、strategy、total_ms、各阶段 ms 与命中文档数，即使对外 trace=null 仍在内部记录耗时。应用日志不保存问题、答案、API key 或完整提示词；异常只记录类型与位置，不记录异常消息、源代码或局部变量。第三方 SDK 调试日志被关闭。

pytest 的 autouse fixture 将 LOG_DIR 指向每个测试的 tmp_path，并关闭该测试创建的文件处理器；不会写入仓库真实运行的 logs/app.log。日志隐私断言只检查应用日志文件及 app.* logger，不把测试客户端等第三方捕获记录当作应用输出。httpx、httpx2、httpcore 均设为 WARNING，兼容两种客户端包名，避免出站 INFO 请求行进入应用日志。

**对话原文会保存在 MySQL 的 chat_logs 中。C 端上线前请在界面告知用户，并设定保留期限。** CHAT_LOG_ENABLED 默认 true，成功响应和 AppError 响应生成后通过后台任务在线程池中写入，不阻塞事件循环；任何写入失败只记录固定 warning，不改变用户回答。AppError 的 answer 为空字符串、route=error、error_code 为业务错误码。引用列表保存到 sources_json，user_id 暂为 NULL。后台任务不保证进程异常退出时可靠送达，需要可靠审计时应另设持久队列。

chat_logs_view 是本机管理工具，默认显示时间(UTC)、session、route、耗时、问题与答案前 40 字，--full ID 才显示完整记录；筛选与清理均绑定 SQL 参数。purge_chat_logs 默认仅统计过期行，--yes 才删除；不会自动设置数据库保留期限，请按选定期限定期执行。评测 JSON 文件和管理工具终端也可能含问题与答案原文，分享前需自行检查。

## 目录结构

```text
.
├── docker-compose.yml
├── .gitignore
├── README.md
├── data/
│   ├── docs/                # 18 份虚构样例 Markdown 文档
│   ├── faq.csv              # 30 条 FAQ
│   └── eval.jsonl           # 24 条评测标注
└── backend/
    ├── pyproject.toml
    ├── app/
    │   ├── main.py            # 入口、路由、健康检查、异常转换
    │   ├── config.py          # 环境配置
    │   ├── schemas.py         # API 模型
    │   ├── contracts.py       # 业务契约、错误码
    │   ├── pipeline.py        # 异步聊天编排、超时和来源
    │   ├── services.py        # 全局模型单例、服务生命周期
    │   ├── logging_config.py  # 日志配置、request_id、异常清洗
    │   ├── chat_log.py        # 对话写入与幂等补列
    │   ├── rate_limit.py      # Redis / 进程内限流
    │   ├── tracing.py         # 请求隔离的阶段计时
    │   ├── faq/
    │   │   ├── __init__.py
    │   │   ├── repo.py       # 可注入 MySQL 连接的 FAQ 仓库
    │   │   ├── cache.py      # Redis 精确缓存、规范化与失败降级
    │   │   ├── bm25.py       # jieba 分词、内存召回与 reload
    │   │   └── service.py    # 缓存 → BM25 → 稠密相似度
    │   ├── retrieval/
    │   │   ├── __init__.py
    │   │   ├── embedder.py  # 本地 BGE-M3 与 FakeEmbedder
    │   │   ├── store.py     # 集合创建与幂等 upsert
    │   │   ├── reranker.py  # 本地 BGE 重排与 FakeReranker
    │   │   └── retriever.py # 多查询混合检索、父块去重与重排
    │   └── llm/               # client、router、strategies、generator、prompts/*.md
    ├── ingest/
    │   ├── __init__.py
    │   ├── loaders.py       # MD/TXT/PDF/DOCX
    │   ├── splitter.py      # 父子块与稳定标识
    │   ├── run.py           # 文档入库及 dry-run
    │   ├── load_faq.py      # CSV 校验与 MySQL 导入
    │   ├── clear_cache.py   # SCAN 分批清理 FAQ 缓存
    │   ├── migrate_db.py    # 已有 chat_logs 表补列
    │   └── purge_chat_logs.py # 默认只统计的对话清理工具
    ├── eval/
    │   ├── __init__.py
    │   ├── search_demo.py   # 手动对照 rerank 开关的 Top5
    │   ├── faq_demo.py      # 手动查看 FAQ 结果、来源与分数
    │   ├── check_retrieval.py # RAG 命中率与 fallback 阈值评测
    │   ├── chat_demo.py     # 完整链路 JSON 演示
    │   ├── run_eval.py      # 路由、引用、耗时汇总与 JSON 文件
    │   ├── metrics.py       # 延迟分位数的线性插值
    │   ├── profile_rerank.py # 重排 tokenize/前向剖析与 Top1 对照
    │   ├── llm_ping.py      # 无数据库依赖的最小 LLM 服务诊断
    │   └── chat_logs_view.py # 本机查看对话记录
    ├── milvus/embedEtcd.yaml
    ├── sql/init.sql
    └── tests/
        ├── conftest.py
        ├── test_api.py
        ├── test_health.py
        ├── test_cli_config.py
        ├── test_ingest_data.py
        ├── test_ingest_faq.py
        ├── test_ingest_loaders.py
        ├── test_ingest_retrieval.py
        ├── test_ingest_run.py
        ├── test_ingest_splitter.py
        ├── faq_fakes.py
        ├── test_faq_index.py
        ├── test_faq_service.py
        ├── test_online_retrieval.py
        ├── test_reranker.py
        ├── test_online_imports.py
        ├── test_eval_demos.py
        ├── test_check_retrieval.py
        ├── chat_fakes.py
        ├── test_llm.py
        ├── test_llm_retries.py
        ├── test_llm_helpers.py
        ├── test_pipeline.py
        ├── test_speculative_pipeline.py
        ├── test_profile_rerank.py
        ├── test_rate_limit.py
        ├── test_chat_eval.py
        ├── test_services.py
        ├── test_clear_cache.py
        ├── test_chat_log_storage.py
        ├── test_logging_helpers.py
        ├── test_app_observability.py
        └── fixtures/
            ├── after_sales_policy.md
            └── logistics_note.txt
```

`volumes/`、`models/`、`.env`、`.venv/` 和 Python 缓存已忽略。缓存、FAQ、混合检索、重排、LLM 和限流已接入聊天接口；开启 `CHAT_LOG_ENABLED` 时聊天记录异步写入 MySQL。测试不需要启动依赖，也不会建立外部网络连接。

## 待验收

- 阶段 2H：默认 child 重排、可选 MPS fp16、同文本精度对比、检索双模式/候选数参数与独立重排计时、LLM 有限重试及 600 token/150 字生成约束已实现。指定 pytest 仍因 uv 用户缓存权限未启动，工作区缓存的离线替代运行又因 Python 3.9.6 不满足项目 3.11 要求失败；完整 pytest 未验证。110 项标准库离线测试、语法与差异检查通过；真实 child/parent Hit@1、拒答阈值、MPS fp16 稳定性和端到端耗时须在本机重新评测。
- 阶段 2G：重排配置、分批与顺序还原、两阶段检索、投机并行、剖析工具和 p50/p90 汇总已实现。指定 pytest 仍因 uv 用户缓存权限未启动；改为工作区缓存并禁止下载后，可用 Python 3.9.6 不满足项目 3.11 要求，完整 pytest 未验证。105 项标准库离线测试通过，语法与差异检查通过。真实设备的 tokenize/前向耗时、约 10 秒目标和 Hit@1 是否保持，须在本机运行 profile_rerank、check_retrieval 与 run_eval 复核。
- 阶段 2F：配置统一为 LLM_API_KEY / LLM_BASE_URL，保留旧名兼容与安全弃用警告。日志脱敏用例改为仅统计诊断 WARNING，显式启用 INFO 验证不会误计；测试前清除应用配置环境变量并隔离日志目录。指定 pytest 命令仍受 uv 缓存权限限制，离线指定系统 Python 后因 3.9.6 不满足项目 3.11 要求失败，完整 pytest 未执行；97 项标准库离线测试、语法检查与 git diff --check 通过。新旧名环境读取和 API 集成 pytest 仍须在本机完整依赖环境验证。
- 阶段 2E：已加入无正文文本的响应结构诊断、truncated 原因、分段最终内容解析、LLM_EXTRA_BODY 和可配置 token 上限。当前指定 pytest 命令仍被 uv 缓存权限与可用 Python 3.9.6 不符合项目 3.11 要求所阻，原 269 项及新增 pytest 未执行；95 项标准库离线测试、78 个 Python 文件语法检查和 git diff --check 通过。真实 LLM 服务的推理 token 与输出截断原因须由开发者本机运行 llm_ping 确认，不能据本次假响应测试认定。
- 阶段 2D：已加入三类意图、敏感请求固定拒答、24 题数据和独立于 ALLOW_TRACE 的内部评测耗时。指定 pytest 命令仍被 uv 缓存权限阻止，离线指定可用系统 Python 后因 3.9.6 不满足项目 3.11 要求失败；未安装依赖，现有 254 项与新增 pytest 未复核。87 项标准库离线测试、75 个 Python 文件语法检查与 git diff --check 通过；24 题真实 LLM 服务结果待开发者本机验证。
- 阶段 2C 修复：指定的 `cd backend && uv run --no-sync pytest` 仍因 uv 用户缓存权限失败；仓库缓存加禁止下载后，仅能找到不满足项目 3.11 要求的 Python 3.9.6。未安装依赖，完整 pytest（用户主目录报告的 236 项及本次新增测试）未执行。当前可运行的 86 项标准库离线测试通过，74 个 Python 文件语法检查和 git diff --check 通过。真实 LLM 服务诊断与 API 集成测试需在本机 Python 3.11 环境验证。
- 阶段 2B：指定 pytest 命令被 uv 用户缓存权限阻止；仓库内缓存和禁止下载后，可用系统 Python 3.9.6 不满足项目 3.11 要求，且没有 pytest、Pydantic、FastAPI、httpx。未联网安装，完整 pytest 未执行，原有 102 项及新增聊天测试的全量结果尚未验证。当前可执行的 57 项标准库测试通过（51 项回归与 6 项新增 LLM 辅助逻辑测试）；60 个 Python 文件语法检查及 git diff --check 通过。真实 LLM 服务、数据库和模型由开发者本机验收。
- 划痕退货与阈值修订：默认 dry-run 实测为 18 文档、37 父块、76 子块，48 项标准库 unittest 通过（含新增 3 项假检索器汇总测试）。完整 pytest 仍因 uv 缓存/托管 Python 目录权限受限未启动，原 93 项与新增测试的全量结果尚未复核；真实模型分数需清理旧索引并重新入库后验证。
- 阶段 2A：指定的 `cd backend && uv run --no-sync pytest` 因 uv 用户缓存权限受限未能启动；改用工作区缓存并强制离线后，又因托管 Python 目录权限受限而失败。系统 Python 为 3.9.6，缺少 pytest、Pydantic 等基础包，本次未安装依赖。实际通过的是 45 项标准库 unittest（原有入库 35 项、FAQ 索引/缓存降级 7 项、重排器 3 项），全部 Python 文件语法检查与 `git diff --check` 通过。FAQ 服务、检索、完整应用导入及 demo 的新增测试尚未执行；这不是完整 pytest 通过。
- 用户已在主目录 Python 3.11 环境通过修订前的 50 项 pytest；当前受限执行环境可直接使用的 Python 为 3.9.6，本次 35 项入库离线单元测试通过。完整 pytest 的 uv 启动因用户缓存及托管 Python 目录权限受限而未完成，未将这些离线测试视为完整 pytest 通过；未执行 Compose 配置校验或容器启动。
- 固定镜像标签在 `linux/arm64` 与 `linux/amd64` 下的 manifest、实际启动和 MySQL 首次初始化均未联网验证。
- 嵌入式 etcd 方案已按官方写法配置，需在本机 `docker compose up -d` 后，通过后端 `http://127.0.0.1:8000/health` 的 Milvus 状态与 `http://127.0.0.1:9091/healthz` 验证；本次未启动容器。
- 真实 BGE-M3 权重编码、PDF/DOCX 文件解析、Milvus schema/索引写入及 MySQL 导入事务尚未集成验收；测试验证的是离线替身和纯文本数据。
- 阶段 2A 的真实 MySQL 读取、Redis 缓存、jieba/rank-bm25、Milvus 混合检索和本地 reranker 尚未集成验收；两个 demo 留给真实环境手动执行。
