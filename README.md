# 电商 RAG 智能客服

面向 C 端顾客的 Python 3.11 / FastAPI / Pydantic v2 后端脚手架，依赖由 uv 管理。规划链路为缓存 → FAQ → LLM 路由 → RAG；当前聊天接口仅返回固定占位回答，不访问数据库、不加载模型、不调用 LLM。

## 本地启动

前置条件：uv、Python 3.11、Docker 和 Compose v2（M1 开发机使用支持 Apple Silicon 的 Docker Desktop）。后端直接在本机运行。

1. 在仓库根目录创建 `.env`，使用下方模板，替换本地数据库的占位密码。Qwen key 可留空，聊天占位接口照常工作。当前交付环境禁止写入 `.env.*`，因此未能生成 `.env.example`，完整示例保留在本文中。
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

模型、文档及 FAQ 依赖仅在 `pyproject.toml` 声明，本次未安装。已有基础测试环境可用 `uv run --no-sync pytest` 运行全部测试，不会同步新增依赖；应用与测试导入不要求安装模型、数据库、Redis、BM25、分词或文档解析 SDK。

Compose 包含 Milvus Standalone 2.5.10、MySQL 8.0、Redis 7.4。Milvus 使用官方 standalone_embed 方案，内置 etcd 并使用本地存储，不再依赖外部存储服务；`backend/milvus/embedEtcd.yaml` 只读挂载为 `/milvus/configs/embedEtcd.yaml`，etcd 数据与 Milvus 数据统一保存在 `./volumes/milvus/`。全部服务使用 `${DOCKER_PLATFORM:-linux/arm64}`：M1 默认使用 `linux/arm64`，x86 VPS 在根目录 `.env` 中设置 `DOCKER_PLATFORM=linux/amd64`。镜像架构支持仍需按下方「待验收」核实。所有发布端口只绑定 `127.0.0.1`，内置 etcd 的 2379 不发布到宿主机。数据保存在 `./volumes/`。MySQL 仅在数据目录为空的首次启动时自动执行 `backend/sql/init.sql`，创建 `faq_pairs` 和 `chat_logs`；后续修改 SQL 不会自动迁移已有数据库。

## 环境变量

配置统一由 `backend/app/config.py` 读取仓库根目录 `.env`，进程环境变量优先；从 `backend/` 启动同样有效。导入 `app.main` 不创建应用、不读取 `.env`；Uvicorn 通过 `--factory` 调用 `create_app()` 时才加载配置。测试显式调用 `create_app(settings)`，并以 `Settings(_env_file=None, ...)` 禁止读取环境文件。Compose 从根目录 `.env` 读取凭据及平台配置。以下全部为示例或明显占位值，没有真实密钥：

```dotenv
DOCKER_PLATFORM=linux/arm64
QWEN_API_KEY=
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ROUTER_MODEL=qwen-turbo
GENERATE_MODEL=qwen-plus
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
FAQ_THRESHOLD=0.8
FAQ_CACHE_TTL_S=86400
RERANK_ENABLED=true
RERANK_CANDIDATES=20
RERANK_TOP_K=5
REFUSE_THRESHOLD=0.5
LLM_TIMEOUT_S=20
REQUEST_TIMEOUT_S=40
RATE_LIMIT_PER_MIN=10
RATE_LIMIT_PER_DAY=200
HEALTH_TIMEOUT_S=1
CORS_ORIGINS=["http://localhost:3000","http://127.0.0.1:3000"]
DEBUG=false
```

| 参数 | 用途 |
| --- | --- |
| `DOCKER_PLATFORM` | 仅供 Compose 使用；M1 为 `linux/arm64`，x86 VPS 为 `linux/amd64` |
| `QWEN_API_KEY`、`QWEN_BASE_URL` | LLM 凭据和兼容接口地址；当前只检查 key 是否非空 |
| `ROUTER_MODEL`、`GENERATE_MODEL` | 预留的路由、生成模型名称 |
| `MYSQL_*` | 本机数据库地址、库名、用户及密码；root 密码供容器初始化 |
| `REDIS_URL`、`MILVUS_URI` | 本机依赖服务地址 |
| `MILVUS_TIMEOUT_S` | Milvus 检索客户端建连及每次 `hybrid_search` 的超时秒数，默认 10；须大于 0，运行入口加载配置后注入检索器 |
| `BGE_M3_PATH` | 已准备好的本地 BGE-M3 目录；不存在或文件不完整时失败，不自动下载 |
| `RERANKER_PATH` | 本地 BAAI/bge-reranker-v2-m3 目录；首次重排才加载，不自动下载 |
| `EMBED_BATCH_SIZE` | 每次编码及 upsert 的子块数量，默认 16 |
| `CHILD_CHUNK_SIZE`、`CHILD_CHUNK_OVERLAP` | 子块字符上限及正文重叠字符数；长度上限包含文档标题前缀 |
| `PARENT_MAX_CHARS` | 父块最大字符数，超长小节继续按标点切分 |
| `FAQ_THRESHOLD` | FAQ 稠密余弦相似度阈值，默认 0.8 |
| `FAQ_CACHE_TTL_S` | FAQ Redis 精确缓存 TTL，默认 86400 秒（24 小时） |
| `REFUSE_THRESHOLD` | 归一化 rerank 最高分低于此值时建议拒答，默认 0.5，无检索结果也拒答；实测 RAG 最低 0.894、拒答类最高 0.261，0.5 位于两者之间；样本小，阶段 4 用 eval.jsonl 校准 |
| `RERANK_ENABLED`、`RERANK_CANDIDATES`、`RERANK_TOP_K` | 检索重排开关、去重后候选上限（20）、供调用方使用的结果数量（5）；`retrieve` 的显式 `top_k` 优先 |
| `LLM_TIMEOUT_S`、`REQUEST_TIMEOUT_S` | 预留的超时秒数，当前占位链路未执行超时控制 |
| `RATE_LIMIT_PER_MIN`、`RATE_LIMIT_PER_DAY` | 预留的限流配置，当前未执行限流 |
| `HEALTH_TIMEOUT_S` | 每项 TCP 探测的总超时秒数，连接与关闭共用；已连通后关闭失败或超时仍为 `up` |
| `CORS_ORIGINS` | 允许跨域的来源，格式为 JSON 字符串数组 |
| `DEBUG` | 默认关闭 `/docs` 和 `/openapi.json`；请求体 `debug` 独立控制占位 trace |

## 接口与错误

- `GET /health`：返回 `mysql`、`redis`、`milvus` 的 `up/down`，仅检测 TCP；返回 `llm` 的 `configured/unconfigured`，只检查 key 是否非空。依赖断开仍返回 HTTP 200，不代表认证或协议正常。此接口不发起 LLM 请求。
- `POST /api/v1/chat`：接受 `session_id`、1–500 字的 `message`、可选 `debug`。返回 UUID `request_id`、原 `session_id`、固定 `answer`、`route=general`、`strategy=null`、`sources=[]`、`is_mock=true`。请求 `debug=true` 时附带 trace，否则为 `null`。纯空白消息也会被拒绝。
- 响应头 `X-Request-ID` 与响应体对应。请求参数校验失败返回 HTTP 400 和 `{"code":40001,"message":"请求参数非法","request_id":"..."}`；`AppError` 转换成同样结构的错误体。404、405 分别返回 `40401`、`40501`，其他 HTTP 客户端错误返回 `40000`，均保留原 HTTP 状态；未捕获异常默认返回 `50001`，不暴露内部错误细节。`DEBUG=true` 时框架会展示未捕获异常的调试信息，仅供本地开发。

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

限流、LLM、依赖不可用及超时的业务错误码供后续接入，当前占位链路不会主动调用上述依赖。

## 阶段 1：样例数据与入库

`data/` 为虚构店铺「栖木」的虚构样例：18 份 Markdown 文档、30 条 FAQ、20 条评测问题。知识主题包括售后、配送、支付与发票、会员与优惠，以及四款商品。听岚 S1 单次续航 8 小时、含盒 32 小时；听岚 S2 关闭主动降噪时为 10/40 小时，开启后为 6/24 小时。数据不对应真实品牌或真实服务承诺。

评测集包含 FAQ 6 条、RAG 8 条、闲聊 3 条、拒答 3 条。`expected_doc` 在 RAG 行中是文档文件名，其他行是 `null`。评测只是标注数据，本阶段不执行路由、检索、生成或拒答逻辑。

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

测试使用 FakeEmbedder、假的模型接口及可注入 Milvus/MySQL 客户端；不加载真实模型、不连接数据库，也不读取 `.env`。原聊天接口仍返回占位结果。

## 阶段 2A：独立 FAQ 与检索服务

本阶段提供可注入依赖的同步 `FaqService.faq_lookup(question)` 和 `Retriever.retrieve(queries, rerank_query, top_k=5)`，不串联聊天 pipeline，不调用 LLM。`contracts.py` 中相应契约改为同步签名；后续异步 API 接入时应在线程池中运行这些同步数据库及模型操作。

FAQ 启动时通过 `FaqRepo.load_enabled()` 以参数化 SQL 读取 MySQL 中 `enabled=1` 的问答，用 jieba 和 rank-bm25 构建内存索引，并一次性预计算 FAQ 问句的稠密向量。`FaqService.reload()` 完整重建索引与向量后替换快照；单独的 `Bm25Index.reload()` 仅刷新 BM25 索引。查询先查 Redis，再对 BM25 Top5 问句计算稠密余弦分数，最高分达到阈值则返回 `FaqHit` 并写缓存。缓存 key 为 `faq:cache:{md5(normalize(question))}`：全角 ASCII/空格转半角、小写、去首尾空白与标点，保留内部空白和标点。Redis 包导入失败、连接/超时、`RedisError` 或 `OSError` 只记录 warning 并跳过；坏 JSON 或字段校验失败视为 miss 并记 warning。序列化 bug、`TypeError` 等程序错误继续上抛。FAQ 数据更新后，旧缓存可能继续生效至 TTL，应按需手动失效缓存。

`FaqHit` 序列化字段包含 `faq_id`、`question`、`answer`、`score`、`category`、`source`（`cache` 或 `faq`）；兼容旧 `id` 输入与读取。缓存通过 `model_dump_json()` / `model_validate_json()` 保存和读取完整字段，保留 `category`，读取后仅把 `source` 标记为 `cache`。`hit.top_score` 等于命中分数；`service.top_score` 为当前调用上下文的最高分，未达阈值时仍可用于 trace，无候选时为 `None`。依赖与阈值均通过构造函数注入，不在服务内部读取 `.env`。

检索对每个查询调用 Milvus `hybrid_search`：dense Top20（COSINE）与 sparse Top20（IP，整数词表 ID 字典），`RRFRanker(k=60)` 融合，最多返回 40 个子块。多查询结果合并后按 `parent_id` 去重，保留最高 RRF 分的子块作为 snippet，再按 RRF 分取默认 20 个候选重排。重排使用原始 `rerank_query` 和父块正文，返回分数为归一化 rerank 分；关闭时保留 RRF 分与排序。最终按显式 `top_k` 截断。`RetrievedDoc` 包含 `parent_id`、`doc_id`、`title`、`source`、父块 `content`、命中子块 `snippet` 和 `score`。

真实验证前，请自行准备已声明的依赖、本地模型和完成入库的数据库。在 `backend/` 执行：

```bash
python -m eval.search_demo "怎么申请退货？"
python -m eval.faq_demo "怎么申请退货？"
python -m eval.check_retrieval
```

两个演示脚本在运行时调用 `get_settings()`，从任意启动目录解析本地模型路径。search demo 分别展示关闭/开启重排的 Top5（固定对照两条路径，覆盖 `RERANK_ENABLED`，每条显示标题、score、snippet 前 60 字）；faq demo 显示结果、来源与最高分。重排器优先 MPS，否则 CPU，均关闭 fp16；模型仅延迟加载本地目录并强制离线。脚本仅供手动真实环境验证，单元测试只注入假对象。

`eval.check_retrieval` 同样在运行时调用 `get_settings()`，默认读取 `data/eval.jsonl`（可用 `--eval-file` 替换）。它固定开启 rerank，逐条打印 RAG 问题的 expected_doc 是否命中 Top5、首次出现的排名与归一化分，并汇总 Hit@5、Hit@1（分母仅包含 RAG 问题）。对 fallback 问题，它打印最高分与 `REFUSE_THRESHOLD` 的比较；低于阈值或无检索结果时报告会被拒答，等于阈值时不拒答。此脚本只检查检索与阈值，不执行完整路由或 LLM 拒答。默认阈值 0.5 的依据：实测 RAG 最低 0.894、拒答类最高 0.261，0.5 位于两者之间；样本小，该阈值在阶段 4 用 eval.jsonl 校准。

本次新增二级小节改变了退货文档的父块编号。真实验证前应清理旧知识集合或重建 `kb_chunks` 后重新入库，再运行评测，避免旧子块残留；本次没有操作真实数据库或模型。

## 目录结构

```text
.
├── docker-compose.yml
├── .gitignore
├── README.md
├── data/
│   ├── docs/                # 18 份虚构样例 Markdown 文档
│   ├── faq.csv              # 30 条 FAQ
│   └── eval.jsonl           # 20 条评测标注
└── backend/
    ├── pyproject.toml
    ├── app/
    │   ├── main.py            # 入口、路由、健康检查、异常转换
    │   ├── config.py          # 环境配置
    │   ├── schemas.py         # API 模型
    │   ├── contracts.py       # 业务模型、占位函数、错误码
    │   ├── pipeline.py        # 固定占位回答
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
    │   └── llm/__init__.py
    ├── ingest/
    │   ├── __init__.py
    │   ├── loaders.py       # MD/TXT/PDF/DOCX
    │   ├── splitter.py      # 父子块与稳定标识
    │   ├── run.py           # 文档入库及 dry-run
    │   └── load_faq.py      # CSV 校验与 MySQL 导入
    ├── eval/
    │   ├── __init__.py
    │   ├── search_demo.py   # 手动对照 rerank 开关的 Top5
    │   ├── faq_demo.py      # 手动查看 FAQ 结果、来源与分数
    │   └── check_retrieval.py # RAG 命中率与 fallback 阈值评测
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
        └── fixtures/
            ├── after_sales_policy.md
            └── logistics_note.txt
```

`volumes/`、`models/`、`.env`、`.venv/` 和 Python 缓存已忽略。缓存、FAQ、混合检索与重排已有独立实现，尚未接入聊天接口；路由、聊天日志持久化与 LLM 生成留待后续阶段。测试不需要启动依赖，也不会建立外部网络连接。

## 待验收

- 划痕退货与阈值修订：默认 dry-run 实测为 18 文档、37 父块、76 子块，48 项标准库 unittest 通过（含新增 3 项假检索器汇总测试）。完整 pytest 仍因 uv 缓存/托管 Python 目录权限受限未启动，原 93 项与新增测试的全量结果尚未复核；真实模型分数需清理旧索引并重新入库后验证。
- 阶段 2A：指定的 `cd backend && uv run --no-sync pytest` 因 uv 用户缓存权限受限未能启动；改用工作区缓存并强制离线后，又因托管 Python 目录权限受限而失败。系统 Python 为 3.9.6，缺少 pytest、Pydantic 等基础包，本次未安装依赖。实际通过的是 45 项标准库 unittest（原有入库 35 项、FAQ 索引/缓存降级 7 项、重排器 3 项），全部 Python 文件语法检查与 `git diff --check` 通过。FAQ 服务、检索、完整应用导入及 demo 的新增测试尚未执行；这不是完整 pytest 通过。
- 用户已在主目录 Python 3.11 环境通过修订前的 50 项 pytest；当前受限执行环境可直接使用的 Python 为 3.9.6，本次 35 项入库离线单元测试通过。完整 pytest 的 uv 启动因用户缓存及托管 Python 目录权限受限而未完成，未将这些离线测试视为完整 pytest 通过；未执行 Compose 配置校验或容器启动。
- 固定镜像标签在 `linux/arm64` 与 `linux/amd64` 下的 manifest、实际启动和 MySQL 首次初始化均未联网验证。
- 嵌入式 etcd 方案已按官方写法配置，需在本机 `docker compose up -d` 后，通过后端 `http://127.0.0.1:8000/health` 的 Milvus 状态与 `http://127.0.0.1:9091/healthz` 验证；本次未启动容器。
- 真实 BGE-M3 权重编码、PDF/DOCX 文件解析、Milvus schema/索引写入及 MySQL 导入事务尚未集成验收；测试验证的是离线替身和纯文本数据。
- 阶段 2A 的真实 MySQL 读取、Redis 缓存、jieba/rank-bm25、Milvus 混合检索和本地 reranker 尚未集成验收；两个 demo 留给真实环境手动执行。
