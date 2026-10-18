# 电商 RAG 智能客服｜开发文档

## 1. 项目概述

本项目是面向 C 端顾客的电商智能客服：用「缓存 → FAQ → LLM 路由 → RAG」四级链路回答问题，知识库由离线脚本灌入 Milvus。总工期约 1.5 天，按关键路径串行推进：先把后端链路跑通，再做前端与部署。

**一句话目标**：交付一个可演示、可解释的电商客服。同一个问题能看到它走了「缓存 / FAQ / RAG / 通用对话 / 拒答」哪条路、每段耗时多少、答案引用了哪份文档。

**范围原则**

- P0 只做能跑通演示链路的部分，其余功能进入第 6 节扩展路线，P0 验收通过后再按优先级追加。
- 所有数值参数都走 `.env` / `config.py`，不写死在代码里。
- 不伪造数据和指标：样例数据是虚构店铺，界面上标注「样例」；Mock 数据必须带 `is_mock`。
- 关键路径自己写，不阻塞主链路的模块（前端骨架、评测脚本、pytest）可交给 AI 编码助手。

**已定的主要选型**

| 项 | 选择 | 说明 |
| --- | --- | --- |
| LLM | 智谱 glm-5.3-flash | OpenAI 兼容协议，可换厂商；始终思考，推荐 low 强度 |
| 结构化存储 | MySQL 8 | 存 FAQ 与对话日志 |
| 检索 | Milvus 混合检索 + bge-m3 + Reranker（默认开启） | `RERANK_ENABLED` 可关 |
| 前端 | React + Vite + Tailwind 独立界面 | 视觉按 UI 设计稿实现 |
| 开发与演示机 | 本机 M1，16 GB 内存 | 全部依赖用 arm64 镜像 |
| 公网 | 海外 VPS（无需备案） | 见第 8 节 |
| 样例数据 | 虚构店铺：19 份文档、37 条 FAQ、24 条测试题 | 政策与文档一致；见[联调修复与验收](联调修复与验收.md) |

**标记约定**：【已确认】= 已拍板；【建议】= 默认做法，无异议即生效。

## 2. 技术架构与选型

在线请求按「缓存 → FAQ → LLM 路由 → RAG」逐级下沉，越往下越慢越贵；离线知识库由脚本单独灌入 Milvus，不在请求链路上。

```
前端 · React 单页            聊天、路由标签、引用来源、trace
   ↓
FastAPI · pipeline.py        校验、限流、request_id、编排
   ↓
① Redis 精确缓存  ──命中──→  直接返回            route = cache
   ↓ 未命中
② FAQ 匹配 MySQL→BM25 Top5→余弦 ≥ 0.8
                  ──命中──→  写缓存并返回        route = faq
   ↓ 未命中
③ LLM 路由（一次调用）─通用─→ 通用对话           route = general
   ↓ 知识类
混合检索 + 父块去重（稠密+稀疏各 Top20，RRF → Reranker Top5）  ←──→  Milvus kb_chunks
   ↓
带引用生成（只依据上下文作答，标注 [n]）──低分──→ 拒答，建议转人工  route = fallback

离线知识库（脚本入库）：知识文档(MD/TXT/PDF/DOCX) → 父子切块(父≤1200字,子300字) → bge-m3 编码(稠密+稀疏) → Milvus kb_chunks
```

图形版见 [diagrams/架构图.svg](diagrams/架构图.svg)。

除了缓存和 FAQ 命中，每个请求至少调一次 LLM；RAG 路径调两次（路由 + 生成）。

所有数值参数都走 `.env` / `config.py`，不写死在代码里。

| 项 | 默认值 | 状态 | 备注 |
| --- | --- | --- | --- |
| 后端 | Python 3.11 + FastAPI + Pydantic v2 + LangChain | 已确认 | 用 uv 锁定依赖；LangChain 只用切块与 LLM 客户端 |
| 前端 | React + TS + Vite + Tailwind | 已确认 | 独立界面；视觉按 UI 设计稿的 token 实现 |
| 向量库 | Milvus Standalone 2.4+（docker compose） | 已确认 | 稀疏向量与混合检索在 Standalone 上更稳；M1 用 arm64 镜像 |
| Embedding | BAAI/bge-m3，稠密 1024 维 + 稀疏 | 已确认 | 本机用 MPS，VPS 用 CPU；开工前下载约 2.3 GB |
| Reranker | BAAI/bge-reranker-v2-m3 | 已确认 | `RERANK_ENABLED=true`；候选 10 → 保留 5 |
| LLM | 智谱 glm-5.3-flash，OpenAI 兼容接口 | 已确认 | 路由和生成均显式配置该模型；开启思考，推荐 low；其他兼容模型按其文档配置 |
| 结构化存储 | MySQL 8（docker，`faq_pairs`、`chat_logs`） | 已确认 | `faq/repo.py` 封装访问 |
| 缓存 / 限流 | Redis 7，FAQ 答案 TTL 24h | 建议 | 连不上时跳过缓存，限流退化为进程内计数；已知限制：本地 LRU 淘汰会丢失对应计数 |
| BM25 | rank_bm25 + jieba | 建议 | 启动时从 MySQL 加载到内存 |
| FAQ 命中阈值 | bge-m3 余弦 ≥ 0.9 | 已确认 | 联调后用测试集复核 |
| 父块 / 子块 | 父块按标题切、上限 1200 字；子块 300 字、重叠 50 | 建议 | 子块前拼文档标题提升召回 |
| 混合检索 | 稠密 Top20 + 稀疏 Top20，RRF k=60 | 建议 | 去重后交给 Reranker |
| 拒答阈值 | rerank 最高分 < 0.2 走 fallback | 已实现 | 低分且改写规范化后不同、非空时追加一次重排；默认阈值依据 child 模式实测调整 |
| 超时 | 单次 LLM 12s，整个请求 40s，前端 60s | 已实现 | 超时默认新连接重试一次；最终超时 HTTP 504 / 50401 |

当前使用智谱 `glm-5.3-flash`。以下配置需由开发者在本机设置；代码保留原通用兼容地址和模型名的默认值，不会自动切换供应商：

```dotenv
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
ROUTER_MODEL=glm-5.3-flash
GENERATE_MODEL=glm-5.3-flash
ROUTER_MAX_TOKENS=2048
GENERATE_MAX_TOKENS=4096
LLM_EXTRA_BODY='{"thinking":{"type":"enabled","effort":"low"}}'
LLM_MAX_CONCURRENCY=2
```

该推理模型始终思考，不支持关闭思考，需开启并选择强度（推荐 `low`，也支持 `high`、`max`）。思考 token 计入 max_tokens；默认 `ROUTER_MAX_TOKENS=2048`、`GENERATE_MAX_TOKENS=4096` 为思考与最终答案共用配额，生成配置也用于通用对话。max_tokens 只是上限，不要求用满；单次实际 HTTP 调用耗时由 `LLM_TIMEOUT_S` 兜底，整请求仍受 `REQUEST_TIMEOUT_S` 约束。提示词仍约束简短回答。用户此前实测路由 1024、生成 2048 的配额下 8 个真实问题均成功，路由 2.3–6 秒、生成 2–7 秒；这些数据对应原配额，新的 2048/4096 配额仍需在本机回归。其他 OpenAI 兼容模型继续可用；非思考模型可省略 `LLM_EXTRA_BODY` 或按其文档设置，客户端不自动推测供应商参数。

在 backend 目录执行 `python -m eval.llm_ping`，两个角色分别读取上述线上配额，复用相同 LLM_EXTRA_BODY、超时、并发及重试配置；探测文本仍是简单的「回复ok」，不等同于完整路由/生成质量评测。每项输出 max_tokens、finish_reason、reasoning_tokens、耗时 ms，缺失字段为 null；成功仅附回复长度，失败附脱敏诊断，不打印回复或思考原文。元数据随每次调用返回，避免并发共享最后一次响应造成串扰。

当 finish_reason=length、最终 content 为空但有 reasoning_content/reasoning 时，仍返回通用 HTTP 502 / 50201，诊断 reason 为「思考消耗了全部输出配额，请调大 ROUTER_MAX_TOKENS/GENERATE_MAX_TOKENS，或在模型支持时降低思考强度」。没有推理字段时只提示提高配额；不将思考文本作为答案。上游 error_code=1210 时，诊断提示「该模型不支持关闭思考，请把 LLM_EXTRA_BODY 改为 {"thinking":{"type":"enabled","effort":"low"}}」；这是固定配置示例，不是请求参数值的回显。400 不自动重试，日志不记录问题、答案、推理原文或 key。

LLM HTTP 并发默认 `LLM_MAX_CONCURRENCY=2`，路由和生成共享同一信号量；应与模型服务账号的并发上限一致，多 worker 部署时按 worker 数折算。例如账号总额度为 2，两个 worker 各设为 1；worker 数超过账号并发额度时应减少 worker 或另行使用跨进程协调，进程内信号量不能限制所有 worker 的总并发。等待信号量不计入 `LLM_TIMEOUT_S` 的单次尝试超时，但受 `REQUEST_TIMEOUT_S=40` 整请求预算约束。

连接池显式设置 `max_keepalive_connections=max_connections=LLM_MAX_CONCURRENCY`，新增 `LLM_KEEPALIVE_S=60` 控制空闲 TLS 连接保留秒数（设为 0 可禁用空闲复用）。同一客户端跨请求复用连接。httpx 传输支持 `socket_options` 时同时启用 `SO_KEEPALIVE`，旧版本不支持时跳过；TCP 探测间隔沿用操作系统默认值。底层传输不做隐藏重试，所有重试由本客户端计数。

新增 `LLM_NETWORK_RETRIES=3`，表示首次尝试之外最多重试 3 次；网络错误和超时共用此计数。覆盖 ConnectError、ConnectTimeout、ReadError、RemoteProtocolError、其他 httpx 超时与单次调用超时，也保留连接重置类错误的重试。默认从 `LLM_RETRY_BACKOFF_S=0.5` 秒开始按 0.5、1、2 秒指数退避，每次增加 0 到 min(0.1 秒, 基础退避的 10%) 的抖动。网络/超时退避持有并发名额；超时后仍使用临时独立连接池，避免影响其他请求，结束时关闭临时池。

每次重试前检查整请求预算：剩余时间须大于「退避时间 + min(`LLM_TIMEOUT_S`, `LLM_MIN_ATTEMPT_S`)」，`LLM_MIN_ATTEMPT_S` 默认 5 秒（正常调用 2–5 秒即可完成）；不满足时直接按现有错误返回。重试那一次仍受整请求预算约束，不会延长总耗时上限。实测：6 路并发时一次请求在第 30.7 秒遇到网络错误，旧规则要求剩余 12 秒而放弃重试，改为 5 秒后可再试一次。

`LLM_MAX_RETRIES=1` 保留兼容，现仅控制 5xx 的重试次数，继续使用原指数退避且不加抖动；`LLM_TIMEOUT_RETRIES=1` 仅接受旧配置，已弃用且不再限制超时重试，请改用 `LLM_NETWORK_RETRIES`。上游 429 保持 `LLM_RATE_LIMIT_RETRIES=3` 的独立计数，默认退避 1、2、4 秒；有效的 `Retry-After` 秒数与退避取更大值，单次等待最多 8 秒。无效、负数或日期格式的头忽略。429 退避前关闭响应并释放名额，下次尝试重新排队。三类重试互不消耗或重置计数。

所有尝试、排队和退避共享 `REQUEST_TIMEOUT_S=40` 的整请求预算，包括路由与生成两阶段。重试前必须至少剩余「本次退避 + `LLM_TIMEOUT_S`」的完整尝试时间，否则立即按最后一次错误返回（网络/5xx 为 HTTP 502 / 50201，超时为 HTTP 504 / 50401，429 为 HTTP 503 / 50302），不再等待或发请求；实际总截止时间触发仍返回 HTTP 504 / 50401。

| 限流/忙碌来源 | 业务码 | HTTP 状态与文案 |
| --- | --- | --- |
| 本服务自身的 IP/session 限流 | 42901 | 429，提问有点快，休息一分钟再问吧 |
| 上游 HTTP 429 重试耗尽 | 50302 | 503，服务有点忙，请稍后再试 |
| 同步 IO 执行与等待容量已满 | 50302 | 503，服务有点忙，请稍后再试 |

上游限流日志仍保留 `category=http_status`、`http_status=429`、脱敏 `error_code`（例如智谱的 1302）与重试序号，不记录供应商自由文本中的业务原文或 key。API 沿用 AppError 的统一错误结构，不把上游账号限流显示为本服务用户提问过快。

`REFUSE_THRESHOLD` 默认由 0.5 调整为 0.2，依据 child 模式实测：应答类口语问题的低分约为 0.20–0.44，原阈值会误拒；应拒类最高约为 0.088。现有 eval 拒答类最高为 0.1396，且该题已被路由判为 sensitive。结合敏感路由和生成器 `NO_ANSWER` 兜底，采用 0.2 作为默认值；最高分严格低于阈值才拒答，等于 0.2 允许进入生成。显式环境配置仍优先于默认值；切换重排模式或精度后应重新评测。

低分改写补充重排不再使用文本相似度门槛：对原问与 rewritten_query 去标点、空格并 casefold 后，只要不同且非空，就复用原候选追加一次重排，并按父块保留两次评分的较高值。即使只增加「店铺」两字也会触发；用户实测「支持哪些付款方式」从 0.083 提升到改写后的 0.688，文本相似度不足以判断评分是否会改善。规范化后完全相同、改写为空、重排关闭、原问已达阈值、可疑输入或无结果时仍跳过。在线 pipeline 与离线 `eval.check_retrieval` 共用 `needs_rewrite_rerank`；离线题目可附 rewritten_query 复现，未提供时默认原问、不增加重排或 LLM 调用。

同步 IO 使用独立线程池。模型侧 `MODEL_IO_WORKERS=1`，新增 `MODEL_IO_QUEUE_SIZE=8`（除执行名额外的等待容量，可设为 0）、`MODEL_IO_QUEUE_TIMEOUT_S=20`（等待执行名额的秒数，必须大于 0）。默认同时接纳 1 个执行任务与 8 个等待任务，达到 workers + queue_size 才立即返回 HTTP 503 / 50302。等待队列是异步 FIFO，任务取得执行名额后才提交线程池，不阻塞事件循环、不向底层无界队列堆积任务。队列等待超时返回 HTTP 504 / 50401；整个请求仍受 `REQUEST_TIMEOUT_S=40` 限制。超时或取消的等待项立即移除、释放容量，其函数不再提交；已执行的同步任务不能强制停止，继续占执行名额直到真正结束。被丢弃的投机检索因此可能仍在运行，但下一请求可进入等待队列。

`MODEL_IO_MAX_IN_FLIGHT` 仅保留为兼容旧配置的正整数，显式配置时记录固定弃用警告，不再参与模型容量计算；旧值 1 不会禁用新队列，也不再要求其小于等于 workers。应改用 workers 和 queue_size 两项。限流侧仍独立使用 `RATE_LIMIT_IO_WORKERS=2`、`RATE_LIMIT_IO_MAX_IN_FLIGHT=16`，保持原有满载立即拒绝、已提交任务取消后等底层退出才释放容量的语义。模型全局锁仍保证串行调用，提高 worker 数并不自动提高模型吞吐。线程池收尾最多等待 5 秒；关闭时唤醒并拒绝尚未提交的模型等待项，超时则记固定警告，放弃未完成线程及迟到清理。

生成前上下文过滤由新增 `RAG_CONTEXT_MIN_SCORE=0.1` 控制（范围 0–1）：仅在重排开启时，保留最高分文档，并保留其他分数大于等于该值的文档；不改变 `REFUSE_THRESHOLD=0.2` 的拒答判定。关闭重排时不按分过滤 RRF 结果。过滤后按保留顺序从 1 重新编号，生成器的资料、有效引用和响应 sources 使用同一编号；之后仍按正文实际引用过滤来源，无有效引用时只保留最高分一条。

生成器资料完全无法回答时只输出内部标记 `NO_ANSWER`；可部分回答时引用已知部分，并说明其余资料未提及。pipeline 对大小写、空白、外围标点容错：只有标记时返回现有 REFUSAL、route=fallback、sources=[]，仅记录 `reason=generator_no_answer`。标记夹在正常回答中时也会被移除；移除后只剩空白、标点、格式符或引用时同样拒答，标记不会作为生成答案发给用户。应用日志不记录原问题、答案、提示词或 key。

新增 `data/eval_colloquial.jsonl` 共 34 题（25 条应答、6 条拒答/敏感、3 条闲聊）。在 backend 目录执行 `python -m eval.run_eval --eval-file data/eval_colloquial.jsonl --output logs/eval-colloquial.json`。`expected_route="answer"` 接受 faq/cache/rag，列表接受列出的任一路由；旧 cache 期望仍接受首次命中的 faq。`expected_doc` 可为空或省略，此时跳过引用命中统计；带参考文档且允许 rag 的失败行仍计入引用分母。终端和 JSON 分别提供「误拒答（应答却 fallback）」的 false_refusals 与「误答（应拒却回答）」的 false_answers 清单；错误单独汇总，不冒充误拒答。仅期望 fallback 却返回任何非拒答路由（包括 general）计为误答；列表允许 fallback 时不算误拒答。评测终端/文件按要求包含评测题和回答，不进入应用日志。需在本机运行原 24 题、口语 34 题及并发压测，核验实际质量、排队耗时、溢出 50302 和超时 50401；本次未运行真实依赖或完整 pytest。

`data/docs/product_catalog.md` 为「灵码在售商品总览｜虚构样例」，仅摘录听岚 S1/S2 无线耳机、栖光 L1 桌面台灯、木澈 C1 保温杯的现有详情，不推定店铺只销售这四款。首个 `##` 小节覆盖「你这里有什么商品」「你们卖什么」并完整列出四款商品，随后按品类介绍定位和参数。默认切块参数（父块 1200、子块 300、重叠 50，子块包含标题）下，总览为 4 个父块、5 个子块；整个 docs 为 19 份文档、41 个父块、81 个子块。FAQ 共 37 条，新增两条商品范围咨询与一条耳机咨询，答案摘自总览并保留「虚构样例：」前缀；口语集追加两条商品总览问题，期望 `answer`。

更新后在 backend 目录重新执行 `python -m ingest.run --dir ../data/docs` 与 `python -m ingest.load_faq ../data/faq.csv`，完成知识文档与 FAQ 入库后重启后端以重建内存 FAQ 索引；如存在旧回答缓存，可执行 `python -m ingest.clear_cache` 清理本项目缓存。这些入库命令会在本机加载配置并访问本地模型/服务；本次仅修改代码和数据，未执行真实入库。先用 `python -m pytest tests/test_llm*.py tests/test_settings.py tests/test_ingest*.py tests/test_eval_colloquial.py tests/test_cli_config.py` 运行相关单元测试。

## 3. MVP 功能与接口

P0 的判定标准只有一条：能用 curl 跑通「提问 → 缓存 / FAQ / 路由 / RAG → 带引用的回答」的真实链路。前端放在这条链路跑通之后，因为它不在关键路径上。

**P0 — 必须完成**

- `GET /health`（mysql / redis / milvus / llm 四项状态）与 `POST /api/v1/chat`（非流式）
- C 端防滥用：按 IP 与 session 限流、提示词护栏、生产环境不返回 trace（见第 5 节）
- Redis 精确缓存：只缓存 FAQ 命中结果，TTL 24h；Redis 连不上时自动跳过
- FAQ：MySQL `faq_pairs` 启动时载入内存 → BM25 召回 Top5 → bge-m3 余弦 ≥ 0.9 判命中
- LLM 路由（一次调用，结构化 JSON）：输出 `intent` 与 `rewritten_query`；解析失败默认 knowledge
- RAG：direct 检索 → Milvus 混合检索（稠密 + 稀疏各 Top20，RRF k=60）→ 按 `parent_id` 去重 → Reranker 重排 → Top5 父块 → 带 [n] 引用生成
- RAG 来源仅保留回答正文有效引用的编号，编号不重排；没有有效引用时仅保留最高分的一条。chat_log 与评测均使用过滤后的来源，评测导出保留原引用编号。超时、改写重排及本机验收步骤见[联调修复与验收](联调修复与验收.md)。
- 拒答兜底：检索最高分低于阈值或上下文为空时，明确说「未找到」并建议转人工
- 响应带 trace：每段的 stage、ms、hit / score
- 离线入库脚本：MD / TXT / PDF / DOCX → 父子切块 → bge-m3 → Milvus upsert，可重复执行
- 前端单页：输入、回答、加载 / 错误 / 空状态、路由标签、可展开引用、可折叠 trace、快捷问题
- `docker compose up` 启动 Milvus（etcd + minio）、MySQL 与 Redis；README、`.env.example`

**接口契约**

- 请求体：`session_id`（前端生成的 UUID）、`message`（1–500 字）、`debug`（为 true 时返回 trace）
- 响应体：`request_id`、`session_id`、`answer`、`route`、`strategy`、`sources`、`is_mock`、`trace`
- `route` 枚举：cache | faq | rag | general | fallback；P0 的 `strategy` 只会是 `direct` 或 `null`，枚举保留 direct | hyde | subquery | stepback 四个值以便扩展
- `sources`：rag 时非空，`answer` 中的 [n] 对应 `index`；faq / cache 时给出命中的 FAQ 条目；general / fallback 为空数组
- 错误结构：HTTP 状态码 + `{"code", "message", "request_id"}`；错误码 40001 参数非法、42901 仅本服务自身 IP/session 限流（HTTP 429）、50001 内部错误、50201 LLM 调用失败、50301 Milvus / MySQL 不可用、50302 同步 IO 线程池满载或上游模型持续限流（HTTP 503，服务有点忙，请稍后再试）、50401 超时
- 可选字段 `user_id`（仅 chat_logs，可为空，请求体不加）：P0 不使用，为第 5 节的身份对接预留

**模块边界**：`contracts.py` 定义模块间的函数签名（`retrieve` / `route` / `generate_answer` / `chat_general` / `faq_lookup` / `run_chat`），只有 `pipeline.py` 组装它们。签名的作用是给 AI 编码助手划清改动范围，并便于以后新增策略时不动 `pipeline.py`。模块内部异常一律包成 `AppError(code, message)`，由全局处理器转成统一错误结构。

**仓库结构**

```
ecom-rag/
├── docker-compose.yml        # milvus(etcd+minio) / mysql / redis
├── .env.example
├── data/
│   ├── docs/                 # 知识文档样例
│   ├── faq.csv               # FAQ 样例
│   └── eval.jsonl            # 测试问题 + 期望路由 + 期望文档
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI 入口、异常处理、CORS、限流
│   │   ├── config.py · schemas.py · contracts.py
│   │   ├── pipeline.py       # 编排：cache→faq→router→rag/general
│   │   ├── faq/              # cache.py bm25.py repo.py
│   │   ├── retrieval/        # embedder.py store.py reranker.py retriever.py
│   │   └── llm/              # client.py router.py strategies.py generator.py prompts/
│   ├── ingest/               # loaders.py splitter.py run.py load_faq.py
│   ├── eval/run_eval.py      # 跑测试集，输出路由 / 命中报告
│   └── tests/
├── frontend/                 # Vite 工程
└── docs/
```

提示词放 `llm/prompts/*.md`，不硬编码在 Python 里；每个公开函数至少一个 pytest，依赖外部服务的测试标 `@pytest.mark.integration`。

## 4. 开发排期

总工期约 1.5 天，按关键路径串行：阶段 1–2 只做后端链路，前端放到阶段 3，阶段 4 做校准、评测、部署和彩排。阶段之间用门槛判断是否可以继续。

| 阶段 | 用时 | 内容 | 门槛 |
| --- | --- | --- | --- |
| 准备 | 开工前 | compose 起通、模型、key、数据 | G0 环境就绪：compose 全绿，模型与 key 到位 |
| 阶段 1 | 约 3.5 小时 | 脚手架、LLM 调通、入库脚本 | M1 数据入库：样例文档已入库，LLM 可调通 |
| 阶段 2 | 约 4.5 小时 | 检索、路由、生成、FAQ、缓存、串联 | **M2 链路跑通（唯一硬门槛）**：curl 跑通演示 1–5 |
| 阶段 3 | 约 3 小时 | 前端单页、联调、限流 | — |
| 阶段 4 | 约 3 小时 | 校准、评测、部署、冻结与彩排 | M3 验收：验收清单全勾，可演示 |

图形版见 [diagrams/开发排期图.svg](diagrams/开发排期图.svg)。

关键路径：环境 → 入库 → retrieve() → 带引用生成 → pipeline 串联 → M2；前端在 M2 之后。前端不在关键路径上：做不完仍可用 `/docs` 或 curl 演示主链路，反之不行。

**准备（开工前，不占总工期）**

- [ ] 建仓库与脚手架：`schemas.py`、`contracts.py`、`config.py`、`CLAUDE.md`
- [ ] 本机 `docker compose up` 起通 Milvus、MySQL、Redis（arm64 镜像）
- [ ] 下载 bge-m3 与 bge-reranker-v2-m3（共约 4.5 GB）
- [ ] 配置智谱 glm-5.3-flash 账号凭据，确认额度、有效期与并发上限，验证 enabled/low 思考参数；备一家兼容服务
- [ ] 样例数据：19 份文档、37 条 FAQ、24 条测试题（先定演示脚本的问题，再反向准备数据）
- [ ] 买好海外 VPS 并试跑一次 compose

**阶段 1 · 脚手架与入库（约 3.5 小时）→ M1**

1. FastAPI 骨架、统一错误结构、`/health`；LLM 客户端调通一次（约 1 小时）
2. `ingest/`：loader → 父子切块 → bge-m3 → Milvus upsert，全部样例文档入库并打印统计（约 2.5 小时）

**阶段 2 · 检索、生成与串联（约 4.5 小时）→ M2**

1. `retrieve()`：混合检索 + 父块去重 + Reranker；用 3 个问题自测召回（约 1.5 小时）
2. `route()`、`generate_answer()`（带 [n]）、`chat_general()`、拒答兜底（约 1.5 小时）
3. FAQ（MySQL + BM25 + 余弦阈值）、Redis 缓存、`pipeline.py` 串联与 trace（约 1.5 小时）

**阶段 3 · 前端与联调（约 3 小时）**

1. 前端单页：聊天、路由标签、引用展开、trace 折叠、快捷问题、三种状态
2. 联调真实 `/api/v1/chat`；加 IP / session 限流

**阶段 4 · 校准、评测与部署（约 3 小时）→ M3**

1. `eval/run_eval.py` 跑 24 条测试题，校准 FAQ 与拒答阈值
2. 部署到海外 VPS（第 8 节）
3. 代码冻结，只修 bug；补 README「已知限制」；按演示脚本完整彩排一遍

**AI 编码助手的用法**：关键路径自己写，可交给助手并行写的是不阻塞 M2 的模块：前端骨架（用响应示例 JSON 做 fixture）、`ingest/loaders.py`、`eval/run_eval.py`、pytest。每个任务只给一个目录，交付时附运行命令与测试结果。

**降级预案**

- M1 未达成：缩小语料到 10 份 MD，先放弃 PDF / DOCX loader
- M2 未达成：继续修后端；前端降为一个输入框 + 纯文本结果，扩展功能全部取消
- 冒烟门槛后只修 bug，不加新功能

## 5. 前端是否做登录（面向 C 端）

**结论：本期不做登录，采用「访客模式 + 防滥用」；以后也不建议自建注册登录，而是复用商城已有的登录态。** C 端客服通常嵌在商城 App 或网页里，顾客已在商城登录；再让他们为客服单独注册一次，只会拦住咨询。

**为什么本期不做**

- 咨询不需要身份：本期知识库（售后政策、配送运费、商品参数、优惠规则）都是公开信息，对所有顾客回答相同。售前咨询本来就大量来自未登录访客。
- 成本高：自建 C 端账号（注册、手机号验证码或密码、找回、JWT、路由守卫）至少 0.5–1 天，还要接短信服务并处理个人信息合规，1.5 天窗口放不下。
- 不在关键路径上：登录不影响「入库 → 检索 → 生成」链路和演示看点。
- 会话已有替代：前端生成匿名 `session_id`（UUID）存在 localStorage，多轮会话与日志按它关联。

**不登录也要做的防护（P0，合计约 1 小时）**

C 端入口对所有人开放，风险是被刷来消耗 LLM 额度，或被诱导说出店铺不该说的话。以下措施替代登录承担这部分作用：

- 限流：按 IP 与 `session_id` 各自限流（建议每分钟 10 次、每日 200 次，用 Redis 计数），超限返回 429 + 友好提示
- 输入校验：`message` 1–500 字；请求体不接受任何身份字段的取值
- 提示词护栏：只回答本店范围内的问题；不承诺退款金额、赔偿等政策外内容；不暴露系统提示词
- 不采集个人信息：访客模式下不索要手机号、地址、订单号；日志只记问题与回答
- 生产部署时 `debug=true` 与 trace 只对演示环境开放，不向 C 端返回内部耗时和分数

**什么时候要加身份，加哪种**

| 触发条件 | 推荐方案 | 估时 |
| --- | --- | --- |
| 顾客要查「我的订单 / 物流 / 退款进度」 | 对接商城登录态：商城前端把已有的用户令牌传给客服，后端校验后取 `user_id`；未登录时只给通用答复并引导去商城登录 | 取决于商城接口，约 0.5–1 天 |
| 独立部署、没有商城账号体系 | 仍用访客模式；个人数据查询改为「订单号 + 下单手机后 4 位」校验 | 2–3 小时 |
| 做知识库 / FAQ 管理后台 | 这是内部员工入口，与 C 端分开：只给 `/api/v1/admin/*` 加管理员登录（单账号 + JWT） | 2–3 小时 |

**本期预留的扩展点（合计约 15 分钟）**

- `chat_logs` 预留可空的 `user_id`；请求体**不**加 `user_id`，身份以后只能从校验过的令牌里取，避免冒充他人查订单
- `main.py` 留一个鉴权依赖 `get_current_user()`，当前恒返回「访客」；以后只替换它的实现
- 管理类接口统一放在 `/api/v1/admin/` 前缀下，方便整体加锁

## 6. 可扩展功能路线

扩展功能只在 P0 验收清单全部通过后开工，分三档：A 档可在跑通后的缓冲时间内完成，B 档是演示后 1–2 周的迭代，C 档是长期方向。每一项都对应一个已预留的扩展点，不需要改动 `pipeline.py` 的主流程。

| 档位 | 功能 | 对 C 端的价值 | 估时 | 接入点 / 前置 |
| --- | --- | --- | --- | --- |
| A | SSE 流式输出 `POST /api/v1/chat/stream` | 首字从 4–5s 降到 1s 内，体感提升最大 | 2h | `generate_answer` 增加流式版本；事件依次为 meta、sources、delta、done |
| A | 移动端适配 | C 端流量主要来自手机 | 1h | Tailwind 响应式；输入框吸底、引用改抽屉 |
| A | 多轮会话 + 追问改写 | 支持「那它支持快充吗」这类追问 | 2h | Redis `session:{id}` 存最近 10 轮；`route()` 已接收 history |
| A | 回答反馈（有用 / 没用） | 收集差评问题，驱动补知识库 | 1h | `chat_logs` 加 `feedback` 字段 + `POST /api/v1/feedback` |
| B | 转人工：留言工单 | fallback 不再是死胡同，顾客可留下问题 | 0.5d | fallback 路由的回答附「转人工」按钮；先写表，不接真实客服系统 |
| B | HyDE / 子问题 / 回溯三种策略 | 答对「A 和 B 哪个续航长」等对比类问题 | 1d | `route()` 输出 `strategy`，`build_queries()` 分支；枚举已预留 |
| B | 订单 / 物流查询 | 电商客服最高频的个人问题 | 1d（Mock） | 新增 route `order`；先 Mock 并标 `is_mock`，真实对接须先完成第 5 节的身份对接 |
| B | FAQ / 知识库管理后台 | 运营自己维护问答，不用改文件重新入库 | 1–2d | `/api/v1/admin/*` + 管理员登录；上传文件触发入库 |
| B | 语义缓存 | 同义问法也能毫秒级返回，省 LLM 费用 | 0.5d | 复用 `embed()`；只缓存 FAQ 类答案 |
| B | 内容安全审核 | 公开上线的合规要求 | 0.5d | 输入 / 输出各加一道审核（云厂商接口或敏感词表） |
| C | 导购推荐 | 从答疑延伸到促转化：按需求推荐商品并附链接 | 3–5d | 商品参数结构化入库 + `category` 过滤 |
| C | 高频问答自动挖掘 | 从日志自动生成 FAQ 候选 | 2–3d | 依赖 `chat_logs` 积累和反馈数据 |
| C | 图片提问（拍照问商品 / 破损） | 售后场景常见 | 3d+ | 多模态 LLM；上传存储与隐私处理 |
| C | 多渠道接入（小程序 / 公众号 / 商城内嵌） | 覆盖顾客实际所在的渠道 | 每渠道 2–3d | API 已与前端解耦，仅新增适配层 |
| C | 运营监控大盘 | 看解决率、转人工率、各路由占比 | 2d | 基于 `chat_logs` 与反馈 |

Bert 意图分类、PPT / 代码入库、本地部署 LLM 不在上表中：它们对 C 端客服的收益低于上表任一项，暂不考虑。

## 7. 风险、验收与已定决策

最大的风险是某一步卡住后没有人可以分担，所以每个风险都配一个超时即降级的规则。

**风险与应对**

| 风险 | 影响 | 应对 / 降级 |
| --- | --- | --- |
| 某一步卡住超过 45 分钟 | 关键路径整体后移 | 立即走降级方案，把问题记入 README「已知限制」，继续下一步 |
| LLM 账号额度用尽、过期或限流 | 演示中断 | 准备阶段在控制台确认额度与并发限制；开发期多用缓存；备用兼容服务须同时核对地址、模型名、思考参数与配额 |
| 本机内存压力（Milvus + MySQL + Redis + bge-m3 + Reranker） | 卡顿或容器被杀 | 机器为 16 GB，正常可运行；模型使用全局单例，给容器设内存上限，卡顿时先关 Reranker |
| Milvus（etcd + minio）compose 起不来 | 检索全断 | 准备阶段先实测；实在不行改用 Milvus Lite 只跑稠密检索，`retrieve()` 接口不变 |
| 模型权重共约 4.5 GB，下载慢 | 开工即卡住 | 提前下载到本机，VPS 上单独下载一次；配 HF 镜像 |
| VPS 无 GPU，Reranker 慢 | 公网版单次 +1–2s | 候选默认已是 10，可再下调；模型全局单例预热 |
| 串行 LLM 调用延迟 6s+ | 演示体验差 | 路由用快速小模型；前端显示阶段进度 |
| FAQ 误命中 | 答非所问 | 默认阈值 0.9；联调后用 24 条测试题复核 |
| LLM 编造政策（面向 C 端会变成客诉） | 可信度与纠纷 | 仅据上下文作答并标 [n]；低分走 fallback；提示词禁止承诺政策外的赔付 |
| 评委和几十人同时访问 | 智谱账号限流或额度耗尽、模型推理排队，延迟明显变长或报错 | 演示前预热缓存；后端限制并发并排队；提前压测；VPS 作为主入口，本机备用（见第 8 节） |

**演示脚本**

| # | 提问 | 期望路由 | 看点 |
| --- | --- | --- | --- |
| 1 | 怎么申请退货？ | faq | BM25 + 阈值命中，答案来自 FAQ 表 |
| 2 | 再问一次「怎么申请退货」 | cache | trace 中缓存命中，毫秒级 |
| 3 | 签收后发现屏幕有划痕可以退吗？ | rag / direct | 引用售后政策原文，可展开来源 |
| 4 | 你好，你能帮我做什么？ | general | 不检索，直接对话 |
| 5 | 你们支持寄到火星吗？ | fallback | 明确说未找到、建议转人工，不编造 |

**P0 验收清单**

- [ ] 新机器按 README 执行 `docker compose up` + 入库 + 启动前后端，15 分钟内可用
- [ ] `/health` 四项依赖全绿
- [ ] 演示脚本 1–5 路由与期望一致
- [ ] rag 回答均带 [n] 引用，引用文本可在原文件中找到
- [ ] Redis 停掉后请求仍正常（跳过缓存）
- [ ] 空消息、超长消息、LLM key 错误、触发限流时，前端有可读提示
- [ ] `eval/run_eval.py` 在 24 条测试集上输出路由准确率、过滤后 RAG 来源命中率、各阶段平均耗时（如实记录，不设硬门槛）；检索 Hit@5 由 `eval/check_retrieval.py` 核验
- [ ] 核心函数有 pytest 且通过；界面上的样例数据已标注

**已定决策**

| 决策 | 结论 |
| --- | --- |
| LLM 厂商 | 智谱 glm-5.3-flash，enabled/low 思考；保留其他 OpenAI 兼容模型支持 |
| 开发与演示机 | 本机 M1，16 GB 内存 |
| 公网部署 | 海外 VPS（无需备案） |
| 结构化存储 | MySQL 8 |
| 前端形态 | 独立界面，不嵌入商城 |
| 样例数据 | 虚构店铺 |
| UI 风格 | 高级简约（「墨与瓷」），按 UI 设计稿 |
| FAQ 命中阈值 | 0.8 |
| Reranker | 默认开启 |
| 扩展功能 | P0 验收后再定 |

## 8. 公网部署（海外 VPS）

公网版与本机版用同一套 compose，只多一个 Caddy 对外；放在 M2 之后的阶段 4，不影响本机演示。

1. 机器：4 核 8 GB 起（40 GB+ 硬盘），选离你近的海外节点；不需要 GPU
2. 域名解析到 VPS，Caddy 自动申请 HTTPS 证书
3. compose 增加 `backend`（模型目录挂载）、`caddy`（托管 `vite build` 产物 + 反代 `/api`）；只有 Caddy 暴露 80 / 443
4. Milvus、MySQL、Redis、etcd、minio 一律不映射公网端口
5. 生产 `.env`：`DEBUG=false`、关闭 `/docs`、不返回 trace、CORS 只允许自己的域名；Caddy 拒绝 `/api/v1/admin/*`
6. 开启第 5 节的限流；在模型服务控制台确认账号额度、并发限制与用量上限
7. 运维：容器 `restart: unless-stopped`，用 `/health` 做存活检查，每天备份 MySQL

**多人同时访问（评委与几十人在线）**

几十人同时在线时，真正同时处理的请求通常只有几个；瓶颈不在隧道，而在模型账号限流、推理排队和本机内存。以下措施都要在演示前做完：

- 入口：评委链接以 VPS 固定域名为主，本机用隧道做备用；不要把会变动的临时隧道链接发给评委。
- 并发上限：每个 worker 默认同时最多发起 2 个 LLM HTTP 调用，其余异步排队；应与模型服务账号的并发上限一致，多 worker 时按 worker 数折算，不能让每个 worker 各自占满账号额度。具体上限以账号限制和压测结果为准。
- 预热缓存：演示前把演示脚本里的问题和常见问题各问一遍，使 FAQ 与缓存命中，命中的请求不调 LLM。
- 压测：在阶段 4 用 20–30 个并发请求跑一遍，记录延迟与报错，再定并发上限；写一个简单的压测脚本。
- 额度：在智谱控制台确认额度、有效期与并发上限；备用兼容服务同时核对地址、模型名、思考参数和 token 配额，运行 llm_ping 后再切换。
- 本机备用：演示期间接电源，运行 `caffeinate -dims` 防止休眠；不要合盖。
- 降级：VPS 慢时先关 Reranker 或下调候选数（默认已是 10）；仍不够时让前端提示稍后再试。


## 流式回答与公网代理

`POST /api/v1/chat/stream` 接受与 `POST /api/v1/chat` 相同的 `{session_id,message,debug}`。原非流式接口及 JSON 错误行为保持不变。流式成功建立后使用 HTTP 200、`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`、`Content-Encoding: identity`，没有压缩或完整响应缓冲。参数校验失败仍是原 HTTP 400 JSON 错误；流开始后的业务失败通过 `error` 事件返回，不能再改变 HTTP 状态。

每个事件使用 `event: 名称` 与 `data: JSON`，后面一个空行。顺序为 meta → status（阶段切换）→ delta（零至多条）→ done 或 error；终态只有一个：

| 事件 | data |
| --- | --- |
| `meta` | `{"request_id":"UUID","session_id":"原会话ID"}`，限流或排队前尽早发送 |
| `status` | `{"stage":"faq"}`，stage 为 faq / routing / retrieving / generating |
| `delta` | `{"text":"文本增量"}`，只含 content，不转发 reasoning_content |
| `done` | 完整 ChatResponse，含 request_id、session_id、answer、route、strategy、sources、is_mock、trace |
| `error` | `{"code":50401,"message":"请求超时，请稍后再试","request_id":"UUID"}` |

等待事件期间每 10 秒发送 `: ping` 注释行和空行；心跳不算 delta，也不增加对话内容。error 沿用 42901（本服务限流）、50302（容量/上游繁忙）、50401（超时）、50201（LLM 失败）、50301（依赖不可用）等原错误码与文案。缓存、FAQ、敏感/低分拒答发送单个 delta 后 done；生成确认只有 NO_ANSWER（或流结束时仅有其残缺真前缀）时，先发送单个 delta，内容为现有拒答文案，再发送 route=fallback 的 done；只拼接 delta 的客户端也能得到完整拒答。NO_ANSWER 匹配先做全角转半角归一化，流式与非流式一致，正文的其它全角字形与标点保持原样。可能被拆开的标记前缀先缓冲，流结束仍为真前缀时丢弃；夹在正文中的完整标记也在转发前移除。

前端以 done.answer 为最终结果，覆盖暂态文本；done 前 `[n]` 按普通文字显示，done 后按 sources 启用引用、来源卡片、路由标签与客户端发送至 done 的耗时。sources 仍只保留有效被引用资料，无有效引用时保留最高分一条；trace 仅请求 debug=true 且 ALLOW_TRACE=true 返回。正常结束记录最终问答；业务失败记录 error 行。客户端断开时取消仍向上传播，并以 shield 独立写入一条 route=error 的记录，answer 为已生成的部分文本或空，写入失败仅记录固定警告，不能吞掉或替代取消。produce 异常退出且尚未入队终态时，consumer 补发 code=50001、message=内部错误的 error 事件；在线连接上的流始终以 done 或 error 结束。原文只进入 chat_log，应用日志仅记路由、阶段、耗时和脱敏诊断。

路由和生成共用 REQUEST_TIMEOUT_S=40 的截止时间、模型线程池等待队列与 LLM 并发信号量。首内容 token 前沿用 LLM_TIMEOUT_S=12；新增 `LLM_STREAM_IDLE_TIMEOUT_S=20` 控制相邻 content 增量的空闲秒数，reasoning/供应商心跳不延长该时间。首内容前网络、超时、429、5xx 按现有各自计数重试并检查剩余预算；捕获 TimeoutError 后还检查整请求 deadline 与 timeout scope，预算已到期立即返回 50401，绝不计算退避或开始重试；已经转发内容后不重试，发送 error 并结束。断开会取消上游 HTTP 流、释放信号量并撤销未开始的模型线程池等待；已经运行的同步模型线程仍须等其自然结束才能释放执行名额。

前端 `VITE_STREAM=1` 为默认值，`=0` 只用原 JSON 接口。使用 fetch + ReadableStream 解析 SSE，支持 UTF-8 跨块、CRLF、多行 data 与注释。首个事件前保持 60 秒 AbortController 超时，收到事件后每次事件重置 30 秒空闲超时；注释和未知事件不作为业务事件续期，未知事件也不禁用兜底。error 的 request_id 可省略，前端保留原 code，缺省时沿用已收到 meta 的 request_id。reader 的 cancel/releaseLock 清理失败不会覆盖结果、错误或阻断兜底。首事件前网络错误、404/405 或非 SSE Content-Type 自动回退一次非流式请求；一旦收到合法的 meta/status/delta/done/error 事件，任何后续错误/提前 EOF 都显示错误气泡并可重试，避免自动重复回答。超时与主动取消不触发兜底。VITE_MOCK=1 同样模拟 status，并按 Unicode 字符每 30–60ms 发送增量，不访问后端。

新增 `CLIENT_IP_HEADER=x-forwarded-for`（默认）或 `cf-connecting-ip`。必须同时满足 `TRUST_PROXY_HEADERS=true` 和 socket 对端 request.client.host 位于 `TRUSTED_PROXIES` 才读取所选头；X-Forwarded-For 取首个字段并校验地址格式，CF-Connecting-IP 必须是单个 IP。缺失、空白、非法地址回退 request.client.host，不再改读其他请求头。**只有后端仅监听 127.0.0.1 且前面有可信代理时才能开启**，代理必须覆盖所选头；公网可直连后端时保持 false。用 Cloudflare 固定隧道时可配置 CLIENT_IP_HEADER=cf-connecting-ip，确保中间反向代理保留 Cloudflare 验证后的该头，禁止客户端绕过可信入口；建议用 `--no-proxy-headers` 禁用 Uvicorn 的自动头重写，由应用根据原始 socket 对端执行白名单校验，避免服务启动参数绕过本配置。

新增 `TRUSTED_PROXIES`，默认 `["127.0.0.1", "::1"]`，可配置 IP 或 IPv4/IPv6 CIDR，环境变量使用 JSON 数组，如 `TRUSTED_PROXIES='["127.0.0.1","::1","10.8.0.0/24"]'`。空列表表示不信任任何代理；非法地址或网段在配置加载时拒绝。不要把公网直连者纳入白名单；后端部署仍只监听 `127.0.0.1`，例如 `uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010 --no-proxy-headers`。未知/不可信对端即使携带转发头也只使用 socket 地址。

前端消息区使用 `aria-busy=true` 抑制生成中的增量播报，`aria-relevant` 仅为 additions；阶段提示不再使用独立 status live region。done/error 后新增完整回答或错误气泡，再解除 busy，避免逐字或重复播报。send（含重试）、reset 与卸载都会更新请求 epoch；所有迟到事件与结果在更新状态前再次核对代次，不能污染新对话。sessionStorage 只保留已完成的回合（含错误终态），恢复旧数据时删除末尾未获回复的 user 消息，不自动恢复加载态。

Cloudflare 固定（named）隧道应转发到本机 HTTP 服务，前端静态页面与 `/api/` 尽量同源。若经 Nginx 反代，可在 API location 中配置：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8010;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    gzip off;
    proxy_read_timeout 90s;
    proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
}
```

上述透传 CF 头的站点入口必须只接受受信 Cloudflare 隧道流量；若直接接受公网访问，不能信任客户端提供的 CF 头。固定隧道 ingress 的 service 指向该本机 Web 服务（例如 `http://127.0.0.1:8080`），只提供 API 时可直接指向后端 8010。对 `/api/*` 禁用缓存、响应压缩和代理缓冲，保留 text/event-stream、Cache-Control 和 X-Accel-Buffering 头。10 秒心跳用于避免 Cloudflare/中间代理约 100 秒无数据断开，但不延长应用总预算；无需增大 Cloudflare 超时以替代后端预算。检查真实域名下首条 meta 是否立即到达以及增量是否逐条到达，不能只以最终收到整段答案判断流式正常。

离线测试新增 `test_answer_stream.py`、`test_chat_stream.py`、`test_llm_stream.py` 和前端 `sse.test.ts`、`stream.test.ts`、`Stream.test.tsx`，使用假服务、异步生成器与 ReadableStream，不依赖真实 LLM/模型/Milvus。仍需本机运行完整 pytest、Vitest、tsc --noEmit、build，并验证真实供应商、浏览器和固定隧道；本轮没有联网或运行 npm/真实服务。

本轮 SSE 审核补充测试：取消时 shield 写入与失败后的取消传播、生产者异常终态、已到期预算不退避、半/全角标记与残缺前缀、可信代理 IP/CIDR，以及前端未知事件、可选 request_id、reader 清理失败、epoch 隔离、仅保存完整回合和无逐字播报。测试仍使用本地替身；完整 pytest、Vitest、tsc 与 build 留待本机执行。
