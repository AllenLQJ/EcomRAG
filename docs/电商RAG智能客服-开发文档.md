# 灵码电商 RAG 智能客服｜开发文档

## 1. 项目概述

一个人开发、维护的访客客服应用，面向虚构店铺「灵码」的公开政策与商品咨询。当前已具备 FAQ、混合检索、重排、带引用生成、拒答、SSE 流式回答、多轮上下文、浏览器历史对话与手机适配；数据与回答不代表真实商家的服务承诺。「栖光」是台灯型号名称的一部分。

后端使用 Python 3.11、FastAPI、Pydantic v2、httpx，依赖由 uv 管理；LLM 客户端直接使用 httpx 的 OpenAI 兼容协议，切块使用自有 `ingest/splitter.py`，不通过 LangChain 编排。前端是 React 18、TypeScript 5、Vite 5、Tailwind CSS 3.4、react-markdown 9。依赖服务为 Milvus Standalone 2.5.10（内嵌 etcd、本地存储）、MySQL 8.0、Redis 7.4，无独立 MinIO 服务。

开发与演示采用本机 M1 / 16 GB，公网入口采用 Cloudflare 固定隧道，后端只监听 `127.0.0.1:8010`，前端开发端口为 `3000`。启动步骤见[根 README](../README.md)，前端操作见[前端 README](../frontend/README.md)。本文描述代码能力与配置约定，真实环境效果需按第 7 节验证。

## 2. 总体架构与在线链路

### 2.1 单轮请求

```text
浏览器 POST /api/v1/chat 或 /api/v1/chat/stream
  → 请求校验、request_id、整请求预算
  → IP 与 session 分别限流
  → Redis FAQ 精确缓存 ─命中→ cache
  → FAQ：MySQL 内存快照 → jieba/BM25 Top5 → bge-m3 余弦 ≥ 0.9 ─命中→ faq、写缓存
  → LLM 路由：intent + strategy + rewritten_query
      general   → 通用对话生成 → general
      sensitive → 固定敏感拒答 → fallback
      knowledge → Milvus 混合检索 → 父块去重 → 重排 → 拒答判断
                  → 上下文过滤 → 带 [n] 引用生成 → rag / NO_ANSWER 转 fallback
```

FAQ 与缓存只存 FAQ 答案，不缓存 RAG 或闲聊。缓存键为 `faq:cache:` 加规范化问句的 MD5；规范化转换全角 ASCII/空格、转小写、去两端空白及标点，不删除句中全部标点。TTL 默认 86400 秒；坏数据、低于当前 FAQ 阈值的缓存视为未命中。Redis 故障时跳过缓存，限流退化为进程内计数。FAQ 启动预热时从 MySQL 载入启用项并预计算问句向量，关闭预热时延迟初始化；更新 FAQ 后应重启后端重建内存快照。

路由意图优先级为 sensitive > knowledge > general。配送范围、付款、商品、售后等店铺具体问题走 knowledge，即使目的地特殊或资料可能不支持，也先检索。路由正常一次调用；JSON 提取或字段校验失败时再调用一次，仍失败则可疑输入走 sensitive，其余走 knowledge/direct/原问。网络故障由 LLM 客户端处理，不等同于路由解析重试。`hyde/subquery/stepback` 仅保留枚举，当前查询构建统一按 direct 处理。

默认 `SPECULATIVE_RETRIEVAL=true`：空 history、FAQ 未命中时，并行执行路由与**原问题**的召回/重排；knowledge 复用这次结果，不再按改写问题重新召回。general/sensitive 会取消等待或丢弃投机结果，其 trace 不进入响应；已经运行的同步模型线程不能强制停止，仍占用执行名额和模型锁。关闭该开关时先路由，再用改写问题召回、原问题重排。

### 2.2 检索、重排与拒答

bge-m3 输出 1024 维稠密向量与词表 ID 稀疏向量。Milvus 集合 `kb_chunks` 的稠密索引为 HNSW/COSINE（M=16、efConstruction=200，查询 ef=64），稀疏索引为 SPARSE_INVERTED_INDEX/IP。每个查询稠密、稀疏各 Top20，经 RRF k=60 融合最多返回 40 个子块，再按 `parent_id` 去重，保留融合分最高的子块作为 snippet。

默认取前 10 个父块候选交给 `bge-reranker-v2-m3`，使用 `normalize=True` 的分数，按分排序保留 Top5。`RERANK_TEXT_MODE=child` 使用已带标题的最佳子块；parent 模式使用父块正文，默认截取前 600 字符（0 表示不截断）。默认 max_length=512、batch_size=16、fp16=false，按文本长度分批再还原评分顺序。MPS 可选 fp16，CPU 始终 fp32；生成始终读取完整父块。

原问题重排最高分低于 0.2 时，若改写规范化后不同且非空、原问非可疑输入、重排已开启且有结果，就复用候选做一次「改写问题补充重排」；**没有文本相似度门槛，也不追加召回**。规范化仅保留 casefold 后的字母数字；按父块合并两次真正的重排分并取较高值，重新取 TopK。投机路径可复用完整候选池，顺序路径复用已返回的 TopK；不得将 RRF 分混入归一化重排分。

无检索结果始终拒答；开启重排时，最终最高分**严格小于** `REFUSE_THRESHOLD=0.2` 才拒答，等于阈值可生成。关闭重排时不拿 RRF 分与拒答阈值比较，但生成器仍可因资料不足拒答。低分拒答不调用生成模型，返回固定文案、`route=fallback`、空 sources、`strategy=direct`；敏感路由直接拒答，strategy 为 null。

拒答判断后才应用 `RAG_CONTEXT_MIN_SCORE=0.1`：重排开启时保留最高分资料，其余需达到 0.1；重排关闭时不过滤 RRF 分。保留资料从 1 编号，生成器、引用校验与响应使用同一编号。回答只保留正文实际有效引用的 sources，编号不再次压缩；无有效引用时保留最高分来源一条。摘要取子块前 60 字符。

提示词要求只据资料回答，通常不超过 150 字、最多 5 条，每个事实附 `[n]`；完全无法回答只输出内部标记 `NO_ANSWER`，部分可答则保留已知事实并说明缺失内容。pipeline 清除大小写、空白、全角变体的标记；只有标记或清理后仅剩标点、格式、引用时转为固定拒答，sources 为空。流式缓冲跨增量的标记前缀，结束时残缺真前缀也不发给顾客。引用校验支持 `[1]`、`【1】`、`［1］` 与全角数字，越界编号删除，代码块及行内代码不参与引用匹配。

### 2.3 多轮上下文

客户端提交 history，服务端不根据 session 自动回填历史，也不保存 Redis 会话状态。history 省略为 `[]`，显式 null 非法；每项必须恰有 `role`、`content`，role 只允许 user/assistant，content 为字符串，可为空，不要求角色交替。

先校验**全部**条目的结构，再保留最近 6 条消息（不是 6 轮），每条取前 1000 个 Unicode 字符；总长超过 4000 时，从最早开始整条丢弃。被裁掉的旧消息若结构非法，仍返回 40001。前端采用更小的发送预算：最近 3 个无错误的完整问答对，每条最多 500 个 Unicode 码点，assistant 先移除 `[n]`。

裁剪后 history 非空时：跳过缓存读取、写入及首次 FAQ 初始化清缓存；先路由，knowledge 才用 `rewritten_query` 查 FAQ，未命中再以改写问题检索与重排。投机检索关闭，补充重排也不使用有省略指代的原问。general 带历史闲聊，sensitive 直接拒答。

历史、问题、资料通过转义后的分隔块传给模型；历史中的 assistant 角色仍是不可信数据。路由只在追问、省略、指代时补齐对象，完整问题不受旧主题影响；历史中的敏感内容不应污染当前正常问题，当前借历史要求忽略规则则仍属 sensitive。历史答案不作为政策依据，也不沿用旧引用编号；无法确定型号或对象时应澄清。这些是提示词约束，不能代替真实模型验收。

### 2.4 超时、并发与排队

路由、生成、限流、模型排队、解析重试及网络退避共享 `REQUEST_TIMEOUT_S=40` 的截止时间。`LLM_TIMEOUT_S=12` 控制单次实际传输，信号量等待不计入这 12 秒。流式首内容前同样为 12 秒，之后相邻 content 间空闲上限为 20 秒；reasoning 或供应商心跳不续期，内容已开始转发后不重试。

网络错误与超时共用 `LLM_NETWORK_RETRIES=3`（首次之外的重试数）；退避从 0.5 秒指数递增并加少量抖动。HTTP 5xx 独立默认重试一次；429 独立默认重试三次，退避从 1 秒指数递增，与有效 Retry-After 秒数取较大值，单次封顶 8 秒。仅 429 退避释放信号量、重新排队；网络/5xx 退避继续持有名额，非流式超时重试另建临时连接池。其他 4xx、重定向、坏响应不自动重试。

重试前剩余预算须**大于**「退避 + min(LLM_TIMEOUT_S, LLM_MIN_ATTEMPT_S)」，默认至少为本次尝试预留 5 秒；不足则按最后错误返回，实际总截止时间耗尽返回 50401。旧 `LLM_TIMEOUT_RETRIES` 仍可读取，但不再限制超时重试。

LLM 并发默认 50，信号量按事件循环及相同上限共享，连接池上限与其一致；远程重排并发默认也为 50，keepalive 默认 60 秒。供应商账号额度独立生效；多 worker 各有独立名额，需按账号总额度折算，增加 LLM 并发不会增加本地重排吞吐。

同步 FAQ、编码和检索/重排使用独立模型线程池：默认 50 个执行名额、50 个额外 FIFO 等待名额，等待最多 20 秒。容量满返回 50302，等待超时返回 50401，仍受总预算约束。取消等待项立即回收容量；已执行任务直到真正结束才回收。全进程模型单例由锁保护，提高 workers 不会自动让模型并行。限流使用独立 50 线程、总在飞 50 的池，满载立即返回 50302；关闭时线程池收尾最多等待 5 秒。

以上依据：`backend/app/pipeline.py`、`faq/`、`retrieval/`、`llm/`、`services.py`、`executor.py`、`budget.py`。

## 3. 接口、错误与可观测性

### 3.1 JSON 接口

`POST /api/v1/chat` 示例（history 不重复包含当前问题）：

```json
{
  "session_id": "client-generated-uuid",
  "message": "那台灯呢",
  "history": [
    {"role": "user", "content": "耳机保修多久？栖光 L1 台灯我也想了解。"},
    {"role": "assistant", "content": "耳机适用 12 个月有限保修。"}
  ],
  "debug": false
}
```

`session_id` 为 1–128 字符非空白字符串，后端不要求 UUID，前端生成 UUID；message 为 1–500 字符且非空白。响应字段：request_id、session_id、answer、route、strategy、sources、is_mock、trace。route 为 cache/faq/rag/general/fallback；当前策略为 direct 或 null，真实后端 is_mock=false。source 含 index、doc_id、title、snippet、score；FAQ/cache 的 doc_id 为 `faq:<id>`，general/fallback 无来源。

`GET /health` 返回 mysql、redis、milvus 的 TCP 连通状态 up/down，llm 为 configured/unconfigured（仅检查 key 是否非空，不调用模型）。它不证明数据库表、知识集合或模型权限已可用。`DEBUG=false` 默认关闭 `/docs` 和 `/openapi.json`。

### 3.2 SSE 接口

`POST /api/v1/chat/stream` 使用相同请求体；校验失败仍返回 HTTP 400 JSON。建立流后 HTTP 200，每个事件用 `event: 名称`、`data: JSON` 和一个空行分隔，终态只有 done 或 error，不单独发送 sources 事件。

| 事件 | data 与行为 |
| --- | --- |
| meta | `{"request_id":"UUID","session_id":"会话ID"}`，在限流、模型加载和排队前发送 |
| status | `{"stage":"faq"}`，stage 为 faq/routing/retrieving/generating，按实际路径发出 |
| delta | `{"text":"文本增量"}`，仅最终 content，不转发 reasoning |
| done | 完整 ChatResponse，含最终 answer、sources、route、strategy、trace 等 |
| error | `{"code":50401,"message":"请求超时，请稍后再试","request_id":"UUID"}`；流内不能再改变 HTTP 状态 |

顺序为 meta → 可选 status/delta → done 或 error。等待期间每 10 秒发送 `: ping` 注释和空行，心跳不是业务事件。缓存、FAQ、直接拒答或纯 NO_ANSWER 拒答发送单个完整 delta，再 done；生成内容已转发后若失败，发送 error。客户端以 done.answer 替换暂态文本，最终 sources 才确定引用。

响应要求 `Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`、`Content-Encoding: identity`；代理关闭压缩、缓存、缓冲。客户端断开时取消上游流与未开始的模型等待；已运行同步线程自然退出后才释放名额。生产者异常且无终态时补发 50001 error。

### 3.3 错误码

JSON 错误结构为 `{"code":数字,"message":"可读信息","request_id":"UUID"}`，响应头包含 X-Request-ID。SSE 建立后的错误放入 error 事件，HTTP 状态仍为 200。

| 业务码 | JSON HTTP 状态 | 含义 |
| --- | --- | --- |
| 40000 | 其他 4xx | 其他 HTTP 请求错误 |
| 40001 | 400 | 参数非法，含消息、会话标识和 history 校验失败 |
| 40401 | 404 | 地址不存在 |
| 40501 | 405 | 请求方法不允许 |
| 42901 | 429 | 本服务分钟频率限流：「提问有点快，休息一分钟再问吧」；远程重排上游 429 沿用此业务码但 HTTP 为 502 |
| 42902 | 429 | 本服务每日频率限流：「今天提问次数已达上限，请明天再试」 |
| 50001 | 500 | 内部错误 |
| 50201 | 502 | LLM 未配置、网络失败、非 429 HTTP 失败、坏响应或输出异常 |
| 50301 | 503 | MySQL/Milvus 等依赖不可用或执行器已关闭 |
| 50302 | 503 | 本地执行/等待容量满载或上游模型 429 重试耗尽：「服务有点忙，请稍后再试」 |
| 50401 | 504 | 单次调用、模型排队或整请求超时 |

### 3.4 trace、日志与数据维护

trace 仅在请求 `debug=true` 且 `ALLOW_TRACE=true` 时返回，否则为 null。记录 cache、faq、router、retrieve、rerank、generate、total 的耗时、命中/分数、意图、候选/保留数量；投机结果带 speculative=true，补充重排 detail 为「改写问题补充重排」并记录合并后 top_score。并行阶段耗时不可相加作为总耗时。

应用日志写 `LOG_DIR/app.log`，按 UTC 日轮转，默认保留 14 份备份，可同时输出控制台；记录请求编号、路由、耗时和脱敏诊断，不记录问题、答案、历史、提示词、推理正文或 key。LLM 日志提供安全的 category、host、model、状态码、响应结构、token 统计等；供应商 1210 有固定思考参数建议，400 不重试。

对话原文另存 MySQL `chat_logs`：当前问题/答案、来源 JSON、错误码、裁剪后 history_count、路由、策略和延迟；不另存历史正文，user_id 写 null。独立有界写入队列默认容量 1000；满载或写入失败丢弃并记录固定警告，不重放提交结果不确定的记录，不阻断回答。流式取消时独立 shield 写入部分答案/error 记录并继续传播取消。日志不是可靠消息队列。

已有库用 `ingest.migrate_db` 幂等补 sources_json、error_code、history_count；新库由 `backend/sql/init.sql` 建表。开启预热且启用对话记录时也会尝试补列。对话原文查看、保留与删除应由自己管理；`purge_chat_logs` 默认只统计，显式 `--yes` 才删除。

依据：`backend/app/main.py`、`schemas.py`、`history.py`、`streaming.py`、`contracts.py`、`chat_log.py`、`logging_config.py`、`llm/diagnostics.py`、`backend/sql/init.sql`。

## 4. 模型与完整配置

### 4.1 智谱部署配置

当前使用智谱 `glm-5.3-flash`，路由和生成均显式设置该模型。它始终思考，需 `thinking.type=enabled`，建议 effort=low；路由 2048、生成/通用对话 4096 的 max_tokens 同时覆盖思考与最终内容配额。不是要求用满配额，实际耗时仍受超时限制。

```dotenv
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ROUTER_MODEL=glm-5.3-flash
GENERATE_MODEL=glm-5.3-flash
ROUTER_MAX_TOKENS=2048
GENERATE_MAX_TOKENS=4096
LLM_EXTRA_BODY='{"thinking":{"type":"enabled","effort":"low"}}'
LLM_MAX_CONCURRENCY=50
```

需另提供 LLM_API_KEY。以上是部署覆盖值，**不是 config.py 的全部默认值**。代码保留阿里云兼容地址、qwen-turbo/qwen-plus 作为通用默认配置；客户端不按模型自动补思考参数。`LLM_EXTRA_BODY` 合并进请求，核心 model/messages/temperature/max_tokens/stream 和路由 response_format 优先。

客户端只返回最终 content，忽略 reasoning；非流式空 content 且 finish_reason=length 时为 50201，日志区分是否思考耗尽配额，提示提高 token 上限或降低受支持的思考强度。流式 length 也作为异常处理。诊断命令 `uv run python -m eval.llm_ping` 分别探测两个模型角色，输出成功标志、耗时、finish_reason、reasoning_tokens 等，不打印回复或推理正文。

### 4.2 全部后端配置项与真实默认值

下表逐项对应 `backend/app/config.py:Settings`，包括兼容项；秒数单位为秒。进程环境优先于仓库根目录环境文件，配置名不区分大小写，未知项忽略；本文核对未读取任何 `.env*`。`get_settings()` 缓存结果，修改配置后需重启进程。列表用 JSON 数组，LLM_EXTRA_BODY 用合法 JSON 对象，空字符串表示 `{}`。

| 环境变量 | 代码默认值 | 含义 |
| --- | --- | --- |
| LLM_API_KEY | 空字符串 | OpenAI 兼容接口凭据；未配置时调用模型返回 50201 |
| LLM_BASE_URL | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 客户端追加 `/chat/completions`，智谱需显式覆盖 |
| QWEN_API_KEY | null | 旧 key 别名；新名非空时优先，否则可回退旧名 |
| QWEN_BASE_URL | null | 旧地址别名；新名非空时优先，否则可回退旧名 |
| ROUTER_MODEL | `qwen-turbo` | 路由模型名 |
| GENERATE_MODEL | `qwen-plus` | RAG 与通用对话生成模型名 |
| ROUTER_MAX_TOKENS | 2048 | 路由输出 token 上限，含推理配额 |
| GENERATE_MAX_TOKENS | 4096 | RAG/通用对话输出 token 上限，含推理配额 |
| LLM_EXTRA_BODY | `{}` | 供应商扩展参数；非法 JSON、非对象或非有限数值拒绝加载 |
| MYSQL_HOST | `127.0.0.1` | MySQL 主机 |
| MYSQL_PORT | 3306 | MySQL 端口，1–65535 |
| MYSQL_DATABASE | `ecomrag` | 数据库名 |
| MYSQL_USER | `ecomrag` | 应用数据库用户 |
| MYSQL_PASSWORD | 空字符串 | 应用数据库密码；Compose 要求提供非空值 |
| MYSQL_ROOT_PASSWORD | 空字符串 | MySQL 容器初始化 root 密码，Compose 要求提供非空值 |
| REDIS_URL | `redis://127.0.0.1:6379/0` | FAQ 缓存与限流 Redis 地址 |
| MILVUS_URI | `http://127.0.0.1:19530` | Milvus 地址 |
| MILVUS_TIMEOUT_S | 10 | 建连及 hybrid_search/upsert 超时 |
| BGE_M3_PATH | `models/bge-m3` | 本地编码模型路径，不自动下载 |
| RERANKER_PATH | `models/bge-reranker-v2-m3` | 本地重排模型路径，不自动下载 |
| EMBED_BATCH_SIZE | 16 | 编码和入库批大小 |
| CHILD_CHUNK_SIZE | 300 | 含标题前缀的子块字符上限 |
| CHILD_CHUNK_OVERLAP | 50 | 子块正文重叠字符数，非负且须小于扣除标题后的正文容量 |
| PARENT_MAX_CHARS | 1200 | 父块字符上限 |
| FAQ_THRESHOLD | 0.9 | BM25 候选中最佳 bge-m3 稠密余弦命中阈值 |
| FAQ_CACHE_TTL_S | 86400 | FAQ 精确缓存有效秒数 |
| RERANK_ENABLED | true | 开启重排及相应分数过滤 |
| RERANK_CANDIDATES | 10 | 父块去重后的重排候选上限 |
| RERANK_MAX_LENGTH | 512 | 重排 tokenizer 的 token 上限 |
| RERANK_BATCH_SIZE | 16 | 重排批大小 |
| RERANK_MAX_CHARS | 600 | 仅 parent 模式字符截断上限；0 不截断 |
| RERANK_TEXT_MODE | `child` | child 使用最佳子块，parent 使用父块 |
| RERANK_FP16 | false | 仅 MPS 支持 fp16；CPU 使用 fp32 |
| RERANK_TOP_K | 5 | 默认返回父块数；retrieve 显式 top_k 优先 |
| SPECULATIVE_RETRIEVAL | true | 空 history 时并行路由与原问检索/重排 |
| REFUSE_THRESHOLD | 0.2 | 重排最高分严格低于此值时拒答 |
| RAG_CONTEXT_MIN_SCORE | 0.1 | 生成上下文过滤阈值；始终保留最高分，仅开启重排时生效 |
| LLM_TIMEOUT_S | 12 | 单次传输/流式首内容超时，不含信号量排队 |
| LLM_MIN_ATTEMPT_S | 5 | 重试预留尝试秒数，与 LLM_TIMEOUT_S 取小 |
| LLM_STREAM_IDLE_TIMEOUT_S | 20 | 首内容后相邻 content 的空闲超时 |
| LLM_MAX_CONCURRENCY | 50 | LLM HTTP 并发及连接池上限；供应商账号配额独立生效 |
| LLM_KEEPALIVE_S | 60 | 空闲 TLS 连接保留秒数；0 禁用空闲复用 |
| LLM_NETWORK_RETRIES | 3 | 网络错误与超时共用的额外重试次数 |
| LLM_MAX_RETRIES | 1 | 仅 HTTP 5xx 的额外重试次数 |
| LLM_RATE_LIMIT_RETRIES | 3 | 上游 HTTP 429 的额外重试次数 |
| LLM_TIMEOUT_RETRIES | 1 | 已弃用，仅接收旧配置，不限制超时重试 |
| LLM_RETRY_BACKOFF_S | 0.5 | 网络/超时及 5xx 指数退避基数；429 另从 1 秒开始 |
| WARMUP_ON_START | true | 启动时预热 FAQ、编码/重排、连接 Milvus；启用记录时尝试补列 |
| ALLOW_TRACE | false | 必须同时请求 debug=true 才返回 trace |
| TRUST_PROXY_HEADERS | false | 是否允许从可信 socket 对端读取转发 IP 头 |
| CLIENT_IP_HEADER | `x-forwarded-for` | 可选 x-forwarded-for / cf-connecting-ip |
| TRUSTED_PROXIES | `["127.0.0.1","::1"]` | 可信代理 IP/CIDR 白名单；空数组不信任任何代理 |
| REQUEST_TIMEOUT_S | 40 | 整请求预算，包含限流、排队、路由与生成 |
| MODEL_IO_WORKERS | 50 | 同步模型工作线程/执行名额 |
| MODEL_IO_QUEUE_SIZE | 50 | 执行名额外的等待容量，可为 0 |
| MODEL_IO_QUEUE_TIMEOUT_S | 20 | 模型等待队列超时 |
| MODEL_IO_MAX_IN_FLIGHT | 1 | 已弃用兼容项，不参与模型容量计算，显式配置会警告 |
| RATE_LIMIT_IO_WORKERS | 50 | 独立限流线程数 |
| RATE_LIMIT_IO_MAX_IN_FLIGHT | 50 | 限流池总在飞容量，满载立即拒绝 |
| RATE_LIMIT_IP_ENABLED | true | 是否启用额外 IP 桶；无法取得真实访客 IP 的本机隧道脚本临时关闭此项，仍保留 session 桶 |
| RATE_LIMIT_PER_MIN | 50 | 每 IP（开启时）、每 session 各自每固定分钟窗口次数 |
| RATE_LIMIT_PER_DAY | 200 | 每 IP、每 session 各自每 UTC 自然日窗口次数 |
| HEALTH_TIMEOUT_S | 1 | 每个依赖的 TCP 连通检查超时 |
| LOG_LEVEL | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| LOG_DIR | `logs` | 应用日志目录，相对启动工作目录 |
| LOG_BACKUP_DAYS | 14 | 按日轮转的备份数量；0 不限制备份数量 |
| LOG_TO_CONSOLE | true | 同时写控制台日志 |
| CHAT_LOG_ENABLED | true | 启用 MySQL 对话记录及相应启动补列 |
| CORS_ORIGINS | `["http://localhost:3000","http://127.0.0.1:3000"]` | 允许的浏览器源；生产跨域访问需显式设置 |
| DEBUG | false | FastAPI 调试与 OpenAPI/docs 开关，独立于 ALLOW_TRACE |

数值上限/秒数除明确允许 0 的项外须为正；阈值范围 0–1，重试次数非负。Settings 还要求 overlap < child_size − 1，切块时再校验实际标题预算。旧 QWEN_* 使用或冲突会发固定警告，不输出值；新值为空白视为未提供，空地址且无有效旧名时恢复默认地址。

`DOCKER_PLATFORM` **不是 Settings 项**：仅 `docker-compose.yml` 使用，默认 `linux/arm64`，x86 主机需 `linux/amd64`。Compose 只有三个依赖容器，所有发布端口绑定回环地址，数据写 `volumes/`；MySQL 仅在空数据目录首次启动执行初始化 SQL。后端和前端不在 Compose 服务中。

## 5. 数据、入库与仓库结构

### 5.1 现有数据

`data/docs/` 有 19 份 Markdown，清单如下；默认父块 1200、子块 300、重叠 50 时为 41 个父块、79 个子块（`backend/tests/test_ingest_run.py` 固定该基准）。

| 文件 | 内容 |
| --- | --- |
| after_sales_application.md | 退货申请流程与材料 |
| after_sales_exchange.md | 换货规则与办理方式 |
| after_sales_refunds.md | 退款时效与原路退回 |
| after_sales_returns.md | 退货条件 |
| after_sales_warranty.md | 保修与日常维护 |
| logistics_delivery.md | 配送范围与预计时效 |
| logistics_shipping.md | 运费与合单规则 |
| logistics_tracking.md | 物流查询与破损少件 |
| payment_invoices.md | 电子发票申请说明 |
| payment_methods.md | 支付与账单核对 |
| payment_order_changes.md | 订单取消与地址修改 |
| product_catalog.md | 在售商品总览，汇集下列四款商品，不推定店铺仅售这四款 |
| product_muche_c1.md | 木澈 C1 保温杯 |
| product_qiguang_l1.md | 栖光 L1 桌面台灯 |
| product_tinglan_s1.md | 听岚 S1 无线耳机 |
| product_tinglan_s2.md | 听岚 S2 无线耳机 |
| promotion_campaigns.md | 活动价与价格说明 |
| promotion_coupons.md | 优惠券使用与返还 |
| promotion_membership.md | 会员与积分规则 |

`data/faq.csv` 有 37 条数据（不含表头，37 个不同问题），表头固定为 question,answer,category；分类为 after_sales/logistics/payment/product/promotion。导入时同问题保留 CSV 最后一条，数据库按 question 更新或插入，不删除文件中已移除的问题。所有 FAQ 答案应与知识文档一致。

| 评测文件 | 题数 | 用途与期望路由 |
| --- | --- | --- |
| data/eval.jsonl | 24 | 标准链路回归：6 FAQ、8 RAG、5 general、5 fallback，RAG 指定参考文档 |
| data/eval_colloquial.jsonl | 34 | 口语与短问法：25 answer、6 fallback、3 general，关注误拒答/误答 |
| data/eval_multiturn.jsonl | 9 | 追问改写、历史隔离和注入：7 条接受 faq/rag、1 fallback、1 general |

### 5.2 入库与维护命令

以下命令均在 `backend/` 执行，供自己在已准备好依赖的环境操作。CLI 默认会加载配置，`--dry-run` 也会加载配置，但不加载模型、不连接服务。

```bash
uv run python -m ingest.run --dir ../data/docs --dry-run
uv run python -m ingest.run --dir ../data/docs
uv run python -m ingest.load_faq ../data/faq.csv
uv run python -m ingest.migrate_db
uv run python -m ingest.clear_cache
uv run python -m ingest.purge_chat_logs --days 30
# 确认统计结果后才用 --yes 真正删除过期对话
uv run python -m ingest.purge_chat_logs --days 30 --yes
```

路径先按当前工作目录查找，再回退仓库根目录，所以在 backend 下 `data/docs`、`../data/docs` 均可解析。模型应事先准备在仓库 `models/`，运行时强制离线加载，不自动下载。

loader 支持 MD/TXT/PDF/DOCX，扫描 PDF 不做 OCR；PDF/DOCX 解析器延迟导入。Markdown 按二级标题整节切父块，介绍并入首节，超长再按字符/标点切分；子块前加文档标题。doc_id 来自源相对路径 SHA256 的前 24 位，parent_id/chunk_id 追加块序号，Milvus 按稳定 chunk_id upsert。重复导入相同内容不会增行，但**不会清理删除或切分变化留下的旧块**；变更切块或移除文档时需另行清理/重建集合，再入库验证。

资料标题与介绍精简后，默认子块由 81 变为 79，父块仍为 41。已有库需要先按这 19 份资料的 doc_id 清理旧块，避免遗留旧标题与旧正文。在 `backend/` 下执行以下清理与导入命令；完成后重启后端以重建内存 FAQ 索引。

```bash
cd backend
uv run python - <<'PY'
import json
from app.config import get_settings
from ingest.loaders import load_directory
from pymilvus import MilvusClient

settings = get_settings()
client = MilvusClient(uri=settings.milvus_uri, timeout=settings.milvus_timeout_s)
try:
    if client.has_collection(collection_name="kb_chunks"):
        doc_ids = [doc.doc_id for doc in load_directory("../data/docs")]
        client.delete(collection_name="kb_chunks", filter="doc_id in " + json.dumps(doc_ids),
                      timeout=settings.milvus_timeout_s)
        client.flush(collection_name="kb_chunks", timeout=settings.milvus_timeout_s)
finally:
    client.close()
PY
uv run python -m ingest.run --dir ../data/docs
uv run python -m ingest.load_faq ../data/faq.csv
uv run python -m ingest.clear_cache
```

FAQ 导入后重启后端更新内存索引。`clear_cache` 仅 SCAN/分批删除 `faq:cache:*`，保留限流计数，不使用 FLUSHDB。`purge_chat_logs --days N` 默认只统计过期原文，--yes 才删除；此策略不会自动定时执行。查看原文可用 `uv run python -m eval.chat_logs_view --since 1d --limit 20`，完整记录用 `--full ID`，输出不写应用日志。

### 5.3 目录与模块边界

```text
├── docker-compose.yml        # 仅 Milvus / MySQL / Redis
├── data/                     # docs/、faq.csv、三个 eval*.jsonl
├── models/                   # 自行准备的本地模型目录
├── backend/
│   ├── app/
│   │   ├── main.py           # 应用工厂、接口、错误、健康检查
│   │   ├── config.py、schemas.py、contracts.py
│   │   ├── pipeline.py、history.py、streaming.py
│   │   ├── services.py、executor.py、budget.py、rate_limit.py
│   │   ├── chat_log.py、logging_config.py、tracing.py
│   │   ├── faq/              # MySQL、BM25、缓存、匹配
│   │   ├── retrieval/        # 编码、存储、检索、重排、改写补救
│   │   └── llm/              # 客户端、路由、生成、流式、诊断、prompts/
│   ├── ingest/               # 入库、迁移、缓存/日志维护
│   ├── eval/                 # 端到端、检索、重排剖析与诊断
│   ├── tests/
│   └── sql/init.sql
├── frontend/                 # 见第 6 节
└── docs/
```

`Services` 装配共享模型、客户端与执行器，`pipeline.py` 负责链路，`contracts.py` 定义数据契约与业务错误；提示词在 `llm/prompts/*.md`。模块导入不自动加载模型或连接数据库，应用工厂及真实 CLI 才加载运行配置。

## 6. 前端与本地联调

### 6.1 技术栈与目录

React 18 + TypeScript + Vite 5 + Tailwind CSS 3.4，react-markdown 渲染回答；Vitest 2、Testing Library、jsdom 用于测试。Node.js 要求 ≥18.18。字体使用系统栈，无运行时字体或图片 CDN 依赖。

```text
frontend/
├── package.json、vite.config.ts、tsconfig.json、tailwind.config.ts
├── env.sample                # 参考模板，Vite 不会自动加载此文件
└── src/
    ├── App.tsx、main.tsx、index.css
    ├── api/                  # 类型、校验、错误映射、fetch/SSE 解析
    ├── components/           # 消息、输入、引用、来源、trace、历史列表
    ├── hooks/                # useChat、滚动与响应式断点
    ├── lib/                  # 配置、引用、格式、history、storage、会话时间
    ├── mocks/                # 固定关键词 fixtures
    └── test/                 # 全局设置及测试工具；测试文件与模块就近放置
```

### 6.2 全部前端配置

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| VITE_API_BASE | 空字符串 | 浏览器 API origin，空值同源；自动追加 `/api/v1/chat[/stream]`，不要重复写路径 |
| VITE_PROXY_TARGET | `http://127.0.0.1:8010` | 仅开发服务器 `/api` 代理目标，不进入浏览器配置 |
| VITE_HUMAN_SUPPORT_URL | 空字符串 | 完整 http(s) 人工客服链接，无效或为空时隐藏入口 |
| VITE_STORE_NAME | `灵码` | 店铺名、欢迎语与网页标题，空白回退灵码 |
| VITE_MOCK | `0` | 严格为 1 才使用本地预设响应，不请求后端 |
| VITE_STREAM | `1` | 默认 SSE，严格为 0 才只走 JSON |
| VITE_SHOW_TRACE_TOGGLE | `0` | 严格为 1 时公开显示「查看链路」入口 |

Vite 客户端变量在构建时注入，变更需重启开发服务器或重建；VITE_* 不能包含凭据。依据 `frontend/src/lib/config.ts`、`storage.ts`、`api/client.ts`、`components/`、`vite.config.ts`、`env.sample`。

### 6.3 流式、引用与错误

默认用 fetch + ReadableStream POST SSE；解析支持 UTF-8 跨块、CRLF、多行 data 与注释。首个合法业务事件前超时 60 秒，之后每次合法业务事件重置 30 秒空闲计时；未知事件及注释心跳不续期。首合法事件前发生网络错误、HTTP 非成功（含 404/405）、非 SSE、空流或解析异常时，自动回退一次 JSON；**代码的兜底范围不限于 404/405**。已收到 meta/status/delta/done/error 后任何失败都显示错误并允许手动重试，主动取消和超时不自动回退。

done 前 `[n]` 为普通文字，done 后用最终 answer/sources 启用引用、来源、路由与耗时；来源默认展开，点击引用会展开、滚动并聚焦对应卡片，卡片不显示 doc_id。摘要只在展示前移除行首 Markdown 标记及重复的开头标题、合并空白和省略号；空正文显示独立兜底，不改接口或历史数据。Markdown 不启用 raw HTML，模型图片只显示替代文字。错误气泡保留业务码和 requestId 数据，但不展示编号或后端内部错误文本。

手机宽度 <641px 的欢迎标题与说明仅在当前会话无消息时显示，第一条消息发出即隐藏，新建空会话后恢复；桌面行为不变。输入区下方仅保留字数，页头保留业务操作入口。应用与品牌预览页均使用 `/favicon.svg?v=2` 的墨印图标和 `#16171A` 主题色。

Enter 发送、Shift+Enter 换行，IME 组合期不发送；请求中锁定发送与重试，新对话/切换会话可取消旧请求。请求 epoch 隔离迟到事件；上滑阅读时不强制滚到底部。生成中消息区 aria-busy，终态再播报完整回答或错误，避免逐字重复播报。

### 6.4 历史列表与链路开关

当前会话 UUID 存 `localStorage['ecomrag.session_id']`，历史存 `localStorage['lingma.conversations.v1']`，每项含 id/title/createdAt/updatedAt/messages；标题取首问前 20 个 Unicode 码点。最多 30 个会话，每个最多最近 100 条已完成消息（50 轮，已有成功会话可包含错误回合）；按 updatedAt 保留最新项。首轮失败不创建列表项，首次成功回答或拒答后才进入列表。

旧 `sessionStorage['ecomrag.messages']` 的完整回合会迁移并删除旧键，未完成末尾 user 不保存。存储异常退回内存，刷新可能丢失；同源标签页共享 localStorage，但没有跨标签页事件合并或跨设备同步。桌面 ≥1024px 的侧栏默认 260px，可拖拽调整至 200–420px；分隔线支持左右方向键每次 16px、Home 最小值、End/双击默认值，宽度保存到版本化 localStorage，较窄屏为历史抽屉；列表分今天/昨天/近 7 天/更早，删除与清空需行内确认。切换、新对话、删除当前会话、清空时中止相关请求。

`VITE_SHOW_TRACE_TOGGLE=1`、URL `?debug=1` 或 `localStorage['ecomrag.debug']='1'` 任一项才显示链路入口。入口可见不等于开启，开关状态存 `sessionStorage['ecomrag.trace']`，默认关闭；开启才请求 debug=true，关闭立即隐藏已有面板。后端仍须 ALLOW_TRACE=true；前端隐藏不是权限边界。真实阶段含 cache/faq/router/retrieve/rerank/generate/total，改写补充重排有 detail，未知字段仍可展示。

### 6.5 启动与构建

后端在 `backend/` 使用 `uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010 --no-proxy-headers --reload`；前端在 `frontend/` 使用 `npm install`、`npm run dev`，固定端口 3000 且 strictPort=true。默认代理仅转 `/api`，不代理 `/health`；浏览器可直接访问后端健康检查地址。

`VITE_MOCK=1 npm run dev` 为离线模拟界面，Mock 流式按 Unicode 字符发送增量，含阶段事件，is_mock=true；history 可传入但不驱动真实推理。`npm run build` 先类型检查再生成 dist，`npm run preview` 只预览静态产物，不提供开发 API 代理；生产需同源反代 `/api` 或构建时指定 API origin 并匹配后端 CORS。

## 7. 评测、测试与验收

### 7.1 日常与完整检查规则

平时只跑单元测试与类型检查，不调用真实模型或服务：

```bash
cd backend
uv run pytest -q
# 另一个终端在 frontend/
npm test
npm run typecheck
```

测试数量以实际运行输出为准：后端 `cd backend && uv run pytest -q`，前端 `cd frontend && npm test`（最近一次单元测试：后端 883 项、前端 181 项全部通过，随后续改动会变化）。

测试使用假服务、MockTransport、内存数据库替身/模拟客户端和 ReadableStream。后端测试配置显式禁用环境文件并隔离配置环境变量，API 默认注入假记录器；不把单元测试当作真实模型质量验证。

大提交或合并 main 前才做完整检查：上述单元测试，加三个端到端评测集、检索评测与并发压测，前端另做生产构建。以下真实评测会访问运行依赖与 LLM，供本机手动执行，此次未运行：

```bash
# backend/
uv run python -m eval.run_eval --eval-file data/eval.jsonl --output logs/eval-standard.json
uv run python -m eval.run_eval --eval-file data/eval_colloquial.jsonl --output logs/eval-colloquial.json
uv run python -m eval.run_eval --eval-file data/eval_multiturn.jsonl --output logs/eval-multiturn.json
uv run python -m eval.check_retrieval
# 只测在线默认输入模式时也可显式指定
uv run python -m eval.check_retrieval --rerank-text-mode child --candidates 10
# frontend/
npm run build
```

### 7.2 指标解释

`eval.run_eval` 直接调用真实 Services/pipeline，逐题串行，不通过 HTTP，因此**不覆盖入口限流与 SSE 传输**。expected_route=answer 接受 faq/cache/rag，列表接受任一列出项，旧 cache 期望同时接受首次 faq；严格 faq 期望不自动接受 cache。脚本不自动清缓存，比较严格单轮路由前应清缓存并固定预热策略。

| 指标 | 含义 |
| --- | --- |
| route_accuracy | 正确路由题数 / 全部题数，错误题算不正确 |
| rag_source_hit_rate | 期望允许 rag 且提供 expected_doc 的题中，实际 rag 响应的过滤后 sources 命中参考文档的比例；失败行仍入分母，无参考文档跳过 |
| false_refusals | 期望回答且不允许 fallback，却返回 fallback 的题目；调用错误不冒充误拒答 |
| false_answers | 仅期望 fallback，却返回任意非 fallback 路由的题目，含 general |
| completed / errors | 成功完成数、错误清单及按业务码汇总的 error_distribution |
| stage_mean_ms / stage_p50_ms / stage_p90_ms | 实际执行阶段的耗时统计，含失败前完成阶段；同阶段多次执行先相加 |
| total_p50_ms / total_p90_ms | 所有题目的实际总耗时，含失败；排序后线性插值，空样本为 null |

run_eval 内部 trace 不受 ALLOW_TRACE 影响，不会改变 API 开关；JSON results 含问题、答案、最终引用编号/来源、耗时与错误，评测导出可能包含原文，应自行保管。引用命中不等于回答事实完全正确，仍需人工检查。

`eval.check_retrieval` 只处理 expected_route 为字符串 rag/fallback 的题，固定开启重排，不调用 LLM 路由、FAQ 或生成；默认依次对比 child/parent，参数可选单模式与候选数。Hit@1/Hit@5 为参考文件在前 1/5 个检索父块中的命中比例，fallback 报最高分是否低于拒答阈值，rerank_ms 不含召回。可在题目附 rewritten_query 复现补充重排；缺省用原问，不自动调用模型改写。它跳过 answer，且当前将路由值直接用于集合成员判断，列表期望会触发 TypeError；不要把多轮集直接传入，也不能用口语集替代标准集解释 Hit 指标。此处仅记录脚本限制，未改代码。

性能剖析命令：`uv run python -m eval.profile_rerank "签收后发现屏幕有划痕可以退吗？"`。工具复用召回候选，对 parent/fp32 的候选数/截断/批大小做网格对比，并按当前文本模式比较 MPS fp16；每组运行两次丢弃首次，输出 tokenize/forward/total、Top1 与分数差。单题 Top1 一致不能证明整体质量不变，模式、精度或候选数改变后应重跑检索和端到端评测。

### 7.3 并发与端到端验收

推荐使用独立工具 `python -m eval.load_test`。在已激活项目 Python 环境、服务与依赖就绪的 `backend/` 目录执行；以下命令会访问真实服务与模型，日常离线测试不运行：

```bash
python -m eval.load_test --concurrency 10 --mode json
python -m eval.load_test --concurrency 20 --mode stream --debug
python -m eval.load_test --concurrency 20 --requests 100 --mode both --ramp 30 --output logs/load-test.json
```

| 参数 | 默认值与行为 |
| --- | --- |
| `--base-url` | `http://127.0.0.1:8010`；仅允许 http/https，无 URL 凭据、查询串或 fragment；不跟随重定向、不使用环境代理 |
| `--concurrency N` / `--requests M` | 默认 N=10、M=N；上限分别为 200、5000。M>N 时由 N 个 worker 持续发送，每请求使用独立 `loadtest-` session_id |
| `--mode json\|stream\|both` | 默认 json；分别访问 `/api/v1/chat` 与 `/api/v1/chat/stream`；both 交替均分，奇数时 json 多 1 个 |
| `--questions` | 默认仓库 `data/eval_colloquial.jsonl`，提取 question 去重并循环；不存在或无有效问题时用内置 10 条店铺问题 |
| `--history-ratio` / `--seed` | 默认 0 / 0；按比例选取请求附带固定的两条历史，数量四舍五入，seed 固定选择结果；session_id 仍每次独立 |
| `--debug` | 请求带 debug:true；服务端需开启 ALLOW_TRACE 才能获得 trace，否则显示无 trace 数据 |
| `--timeout` / `--ramp` | 默认 90 秒 / 0 秒；timeout 限制整个单请求（含 SSE）；ramp 将 M 个请求的计划启动时间线性分布于指定秒数内，受 worker 容量约束时延后 |
| `--output` | 可选 JSON 报告路径，自动创建父目录；不读取或写入 `.env*`，也不加载应用配置 |
| `--i-know-this-is-not-localhost` | 非 `127.0.0.1/localhost` 主机必须显式确认，运行时仍提醒非本机目标 |

工具在发送压测请求前先检查 `/health` 并显示 MySQL、Redis、Milvus、LLM 状态；HTTP 失败、响应无效或依赖未就绪时退出码非 0。health 不提供服务端限流配置，因此启动时提示「若 RATE_LIMIT_PER_MIN / RATE_LIMIT_IP_ENABLED 未放开，高并发会触发 42901/42902」。如需测容量，请自行在本机测试配置中调整限额并重启，测试结束恢复；工具不修改配置。统计将这两种业务码单列为「被本服务限流」，不计入后端故障/其他失败，仍计入失败总数。

文本表和 JSON 报告包含完成/成功/失败数、HTTP 状态和业务错误码分布（含 50302、50401、42901、42902、50201）、总墙钟时间、成功吞吐与成功请求 min/p50/p90/p95/p99/max。墙钟时间不含 health，包含 ramp 和排队；延迟从实际发送起算，不含 ramp 等待。SSE 以 done/error 判断完成，HTTP 200 不代表成功；缺少终止事件算协议失败，首字耗时取首个非空 delta，完整回答耗时取 done，二者仅汇总成功流式请求。分位数排序后线性插值，空样本为 null。debug 额外统计 router/rerank/generate/faq/retrieve 均值与 p90（同请求同阶段先相加，只统计观察到的阶段）、route 分布及 rerank provider 分布。存在失败时工具返回非 0。

每请求明细只保存序号、模式、是否带历史、问题前 20 字（常见凭据格式脱敏）、状态、路由、耗时、错误码、允许的阶段/provider 指标及答案长度；不保留完整问题、答案、历史、服务端错误原文或 trace 自由文本。不将真实密钥放入问题。`backend/tests/test_load_test.py` 通过 MockTransport 覆盖 JSON/SSE、跨块解析、统计、边界与输出隐私，不调用真实服务。

FAQ 缓存只影响 FAQ，知识库问题不会因重复发送而缓存。真实域名还应验证 meta 及时到达、delta 逐条到达、心跳、取消和错误，不能只看最终完整答案。

验收包括：依赖健康与实际入库可检索；FAQ 首次命中/重复缓存；知识回答事实与引用一致；资料不足、隐私/注入走拒答；多轮追问不串答；Redis 故障仍可回答；非法输入、限流、繁忙和超时有可读提示；手机输入/键盘、历史切换/删除、刷新恢复和链路开关正常。真实供应商、浏览器、读屏与隧道必须实测，源码或单元测试不能证明已通过。

## 8. 部署、防滥用与已知限制

### 8.1 本机与 Cloudflare 固定隧道

部署链路为：固定域名 → Cloudflare named tunnel → 本机 Web 服务（托管 frontend/dist 并反代 /api）→ `127.0.0.1:8010`。临时隧道不支持本项目所需 SSE 流式，采用固定隧道（依据既有 `docs/演示准备.md` 的部署约定，未在此次联网复测）。仓库没有提供 cloudflared ingress、Nginx 站点或一键部署脚本，下列片段是按接口要求给出的部署示例。

后端启动：

```bash
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010 --no-proxy-headers
```

前端 `npm run build` 后由 Web 服务托管 dist，固定隧道指向该服务的本地地址；只公开 API 时可直接指向 8010。不要用 Vite 开发代理替代生产反代。Compose 各依赖只绑定回环端口；当前 Compose 没有 backend、Caddy 或 Nginx 服务，也未配置 restart 策略，运行维护需自行安排。

Nginx 的 API 示例（入口须只接受受信隧道流量）：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8010;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
    proxy_buffering off;
    proxy_cache off;
    gzip off;
    proxy_read_timeout 90s;
}
```

对 `/api/*` 禁用边缘缓存、压缩与缓冲，保留 text/event-stream、no-cache、X-Accel-Buffering 和 identity 头；后端 10 秒注释心跳维持传输，不延长 40 秒应用预算。后端 DEBUG=false、ALLOW_TRACE=false；仅演示时同时开放 trace 与前端入口，长期公开运行应恢复关闭。跨域访问时 CORS_ORIGINS 仅配置实际前端源。

### 8.2 真实 IP 与限流

Cloudflare 固定隧道配置 `TRUST_PROXY_HEADERS=true`、`CLIENT_IP_HEADER=cf-connecting-ip`，TRUSTED_PROXIES 包含实际本地代理 socket 地址，默认 `["127.0.0.1","::1"]`。**只有后端仅监听 127.0.0.1 且入口受可信代理保护时才开启**；中间代理须保留 Cloudflare 验证后的头且不允许绕过入口伪造。`--no-proxy-headers` 禁止 Uvicorn 提前改写 socket 对端，由应用统一校验白名单。

只有开关开启且 socket 对端位于白名单才读选定头；X-Forwarded-For 只取首个字段，CF-Connecting-IP 必须单个合法 IP。缺失、空白、非法头或不可信对端回退 socket 地址，不改读另一种头；白名单支持 IP/IPv4/IPv6 CIDR，空数组不信任任何代理，非法网段拒绝加载配置。

限流默认按 IP 和 session 各自累计固定分钟与 UTC 自然日窗口，默认 50/分钟、200/日；分钟超限为 42901，日超限为 42902，日限额优先提示，超限请求也计数。`RATE_LIMIT_IP_ENABLED=false` 只停用 IP 桶，session 桶继续生效。本机 FRP/Caddy 尚未透传可信访客 IP，`deploy/start-local.sh` 临时关闭共享 IP 桶，避免压测占用其他手机的额度；接通真实 IP 后应恢复双重限流。Redis 使用 INCR+EXPIRE 事务；故障后本进程保留已有本地计数继续限制。回退计数最多 10000 个窗口 key，按过期与 LRU 淘汰，重启清空；多 worker 各自计数，整体限额可能放大，高基数也可能提前淘汰旧计数。session 可自行更换，仅按 session 限流不能代替按真实来源防刷。

### 8.3 访客边界与功能范围

当前无注册登录、JWT、`get_current_user()` 或管理员 API；session_id 是会话标识，不是身份凭证。请求模型没有可信 user_id 字段，chat_logs 的 user_id 仅预留并写 null。应用只回答公开知识，未接入个人订单、物流账户或真实人工工单；人工客服仅为配置的 http(s) 链接。

输入校验、敏感路由、资料/历史隔离及生成拒答共同减少编造与注入风险，不保证覆盖所有攻击。应用日志脱敏不等于不保存原文，chat_logs 与评测导出应自行设定访问、备份和清理策略。

SSE、手机适配、多轮和浏览器历史已实现。HyDE/子问题/stepback 只是枚举及 direct 降级；反馈接口、知识库后台、跨设备同步、语义缓存、图片问答等不属于当前可用能力。扩展时先更新契约和测试，再接入真实身份/数据服务，不以不存在的扩展点作为已交付功能。

### 8.4 已知运行限制

- 单机重排受模型全局锁保护，既有演示记录的知识库吞吐约每分钟 20 个；提高 LLM 并发不能解除该瓶颈，也不是对任意机器的性能保证。
- 海外访问智谱可能有跨境网络抖动；12 秒单次超时、最多三次网络/超时重试仍受 40 秒总预算限制，不能保证每次都有三次重试机会。
- 16 GB 机器内存紧张或 swap 较高时，既有记录中重排会从约 2.5 秒恶化到 20 秒以上；演示时关闭无关大应用、保持接电与不休眠，先预热并留出内存。以上性能观察来自 `docs/演示准备.md` 的实测记录。
- 降低候选数或关闭重排可减少工作量，但会改变质量与拒答语义，须重新评测；不能直接沿用 parent/fp16 的历史分数分布解释 child/fp32 阈值。
- 知识覆盖仅为现有资料，NO_ANSWER 是第二道资料不足兜底；不把商品总览或历史回答当作完整店铺事实。

## 9. 变更与决策记录

- **拒答阈值 0.5 → 0.2**：既有 child 模式调参记录中应答口语题可落在 0.20–0.44，0.5 会误拒，拒答样本约 0.088，另有 0.1396 的样本已被 sensitive 路由拒绝；代码与 `backend/tests/test_pipeline.py` 固定验证这些分数及 0.2 边界，并保留 NO_ANSWER 兜底，原始真实评测报告未随仓库提供。
- **改写补充重排取消相似度门槛**：少量增字也可能改变相关分；`retrieval/rewrite.py` 只排除规范化相同/空改写等条件，`test_rewrite_rerank.py` 用「支持哪些付款方式」与「店铺支持哪些付款方式」的 0.083→0.688 回归用例验证该规则，测试分数是替身输入。
- **采用固定隧道**：`docs/演示准备.md` 明确临时隧道不支持流式；`app/streaming.py` 与 `frontend/src/api/client.ts` 依赖持续 SSE 事件，因此采用 named tunnel 并关闭代理缓冲，实际域名仍需端到端验收。
- **切换 glm-5.3-flash 与 enabled/low 思考**：按当前部署约定显式覆盖地址与两个模型角色，保留 OpenAI 兼容客户端；`llm/diagnostics.py` 和 `test_llm_reasoning.py` 对 1210 给出固定 enabled/low 建议，代码不自动切换供应商或修改配置。
- **并发容量统一为 50**：LLM、远程重排、模型工作线程及等待队列、限流检查线程及总在飞容量均默认 50；分钟频率额度提高为 50，日额度保留 200。该调整扩大接纳容量，供应商配额及本地模型锁仍独立生效，不据此宣称单机模型吞吐提升。
