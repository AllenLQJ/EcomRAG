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
| LLM | 通义千问 Qwen（阿里云国际站免费额度） | OpenAI 兼容协议，可换厂商 |
| 结构化存储 | MySQL 8 | 存 FAQ 与对话日志 |
| 检索 | Milvus 混合检索 + bge-m3 + Reranker（默认开启） | `RERANK_ENABLED` 可关 |
| 前端 | React + Vite + Tailwind 独立界面 | 视觉按 UI 设计稿实现 |
| 开发与演示机 | 本机 M1，16 GB 内存 | 全部依赖用 arm64 镜像 |
| 公网 | 海外 VPS（无需备案） | 见第 8 节 |
| 样例数据 | 虚构店铺：15–20 份文档、30 条 FAQ、20 条测试题 | 用 LLM 生成并人工抽检 |

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
| Reranker | BAAI/bge-reranker-v2-m3 | 已确认 | `RERANK_ENABLED=true`；候选 20 → 保留 5；VPS 无 GPU 时候选可降到 10 |
| LLM | 通义千问（阿里云国际站 Model Studio），OpenAI 兼容接口 | 已确认 | 路由用 qwen-flash 或 qwen-turbo，生成用 qwen-plus；模型名与免费额度以控制台为准 |
| 结构化存储 | MySQL 8（docker，`faq_pairs`、`chat_logs`） | 已确认 | `faq/repo.py` 封装访问 |
| 缓存 / 限流 | Redis 7，FAQ 答案 TTL 24h | 建议 | 连不上时跳过缓存，限流退化为进程内计数 |
| BM25 | rank_bm25 + jieba | 建议 | 启动时从 MySQL 加载到内存 |
| FAQ 命中阈值 | bge-m3 余弦 ≥ 0.8 | 已确认 | 误命中风险高于 0.85，联调后用测试集复核 |
| 父块 / 子块 | 父块按标题切、上限 1200 字；子块 300 字、重叠 50 | 建议 | 子块前拼文档标题提升召回 |
| 混合检索 | 稠密 Top20 + 稀疏 Top20，RRF k=60 | 建议 | 去重后交给 Reranker |
| 拒答阈值 | rerank 最高分 < 0.3 走 fallback | 建议 | 联调后用测试集校准 |
| 超时 | 单次 LLM 20s，整个请求 40s | 建议 | 超时返回 504 |

## 3. MVP 功能与接口

P0 的判定标准只有一条：能用 curl 跑通「提问 → 缓存 / FAQ / 路由 / RAG → 带引用的回答」的真实链路。前端放在这条链路跑通之后，因为它不在关键路径上。

**P0 — 必须完成**

- `GET /health`（mysql / redis / milvus / llm 四项状态）与 `POST /api/v1/chat`（非流式）
- C 端防滥用：按 IP 与 session 限流、提示词护栏、生产环境不返回 trace（见第 5 节）
- Redis 精确缓存：只缓存 FAQ 命中结果，TTL 24h；Redis 连不上时自动跳过
- FAQ：MySQL `faq_pairs` 启动时载入内存 → BM25 召回 Top5 → bge-m3 余弦 ≥ 0.8 判命中
- LLM 路由（一次调用，结构化 JSON）：输出 `intent` 与 `rewritten_query`；解析失败默认 knowledge
- RAG：direct 检索 → Milvus 混合检索（稠密 + 稀疏各 Top20，RRF k=60）→ 按 `parent_id` 去重 → Reranker 重排 → Top5 父块 → 带 [n] 引用生成
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
- 错误结构：HTTP 状态码 + `{"code", "message", "request_id"}`；错误码 40001 参数非法、42901 限流、50001 内部错误、50201 LLM 调用失败、50301 Milvus / MySQL 不可用、50401 超时
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
- [ ] 注册阿里云国际站 Model Studio，拿到 Qwen key，确认免费额度、有效期与限流；备一家备用
- [ ] 样例数据：15–20 份文档、30 条 FAQ、20 条测试题（先定演示脚本的问题，再反向准备数据）
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

1. `eval/run_eval.py` 跑 20 条测试题，校准 FAQ 与拒答阈值
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
| Qwen 免费额度用尽、过期或限流 | 演示中断 | 准备阶段在控制台确认额度与 QPS；开发期多用缓存和小模型；备第二家 key，只改 `.env` 切换 |
| 本机内存压力（Milvus + MySQL + Redis + bge-m3 + Reranker） | 卡顿或容器被杀 | 机器为 16 GB，正常可运行；模型使用全局单例，给容器设内存上限，卡顿时先关 Reranker |
| Milvus（etcd + minio）compose 起不来 | 检索全断 | 准备阶段先实测；实在不行改用 Milvus Lite 只跑稠密检索，`retrieve()` 接口不变 |
| 模型权重共约 4.5 GB，下载慢 | 开工即卡住 | 提前下载到本机，VPS 上单独下载一次；配 HF 镜像 |
| VPS 无 GPU，Reranker 慢 | 公网版单次 +1–2s | 候选从 20 降到 10；模型全局单例预热 |
| 串行 LLM 调用延迟 6s+ | 演示体验差 | 路由用快速小模型；前端显示阶段进度 |
| FAQ 阈值 0.8 误命中 | 答非所问 | 联调后用 20 条测试题复核；如有误命中再上调 |
| LLM 编造政策（面向 C 端会变成客诉） | 可信度与纠纷 | 仅据上下文作答并标 [n]；低分走 fallback；提示词禁止承诺政策外的赔付 |
| 评委和几十人同时访问 | Qwen 限流或额度耗尽、模型推理排队，延迟明显变长或报错 | 演示前预热缓存；后端限制并发并排队；提前压测；VPS 作为主入口，本机备用（见第 8 节） |

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
- [ ] `eval/run_eval.py` 在 20 条测试集上输出路由准确率、检索 Hit@5、各阶段平均耗时（如实记录，不设硬门槛）
- [ ] 核心函数有 pytest 且通过；界面上的样例数据已标注

**已定决策**

| 决策 | 结论 |
| --- | --- |
| LLM 厂商 | 通义千问 Qwen，用阿里云国际站免费额度 |
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
6. 开启第 5 节的限流；在阿里云控制台为该 key 设置用量上限，免费额度用尽后不自动转付费
7. 运维：容器 `restart: unless-stopped`，用 `/health` 做存活检查，每天备份 MySQL

**多人同时访问（评委与几十人在线）**

几十人同时在线时，真正同时处理的请求通常只有几个；瓶颈不在隧道，而在 Qwen 限流、模型推理排队和本机内存。以下措施都要在演示前做完：

- 入口：评委链接以 VPS 固定域名为主，本机用隧道做备用；不要把会变动的临时隧道链接发给评委。
- 并发上限：后端同时最多处理 3–4 个 LLM 请求，超出的排队；前端显示「排队中」，比直接报错体验好。具体上限以压测结果为准。
- 预热缓存：演示前把演示脚本里的问题和常见问题各问一遍，使 FAQ 与缓存命中，命中的请求不调 LLM。
- 压测：在阶段 4 用 20–30 个并发请求跑一遍，记录延迟与报错，再定并发上限；写一个简单的压测脚本。
- 额度：在控制台确认 Qwen 免费额度、有效期与 QPS；准备第二家厂商的 key，只改 `.env` 切换。
- 本机备用：演示期间接电源，运行 `caffeinate -dims` 防止休眠；不要合盖。
- 降级：VPS 慢时先关 Reranker 或把候选从 20 降到 10；仍不够时让前端提示稍后再试。
