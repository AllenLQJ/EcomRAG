# 灵码 · 电商智能客服前端

React 18 + TypeScript 5 + Vite 5 + Tailwind CSS 3.4 + react-markdown 9，独立访客聊天界面，支持 SSE/JSON、引用、链路面板、多轮与浏览器历史列表。视觉使用仓库设计 token 和系统字体栈，无在线字体或图片 CDN 依赖；店名默认「灵码」，「栖光」是台灯型号。

## 安装与启动

Node.js ≥18.18、npm，在根目录执行：

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:3000>；固定端口 3000，strictPort=true，占用会报错。后端在 backend 运行：

```bash
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010 --no-proxy-headers --reload
```

默认浏览器向同源 `/api/v1/chat/stream` 请求，Vite 把 `/api` 转到 `http://127.0.0.1:8010`；不代理 `/health`。后端依赖、入库及启动准备见[根 README](../README.md)。仅看本地固定响应用 `VITE_MOCK=1 npm run dev`，不需要后端。

```bash
npm test             # Vitest 单次运行
npm run typecheck    # tsc --noEmit，含测试与配置
npm run build        # 类型检查后构建，产物 dist/
npm run preview      # 静态预览，不提供 /api 开发代理
```

日常只跑 npm test + npm run typecheck；大提交/合并 main 前加构建，并执行[开发文档第 7 节](../docs/电商RAG智能客服-开发文档.md#7-评测测试与验收)的后端三个评测集、检索评测与并发压测。测试数量以 `npm test` 的实际输出为准。

## 环境变量

[`env.sample`](env.sample) 仅参考，不会被 Vite 自动加载。可直接传给命令：

```bash
VITE_STORE_NAME=灵码 VITE_PROXY_TARGET=http://127.0.0.1:8010 npm run dev
```

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| VITE_API_BASE | 空字符串 | API origin；自动追加 `/api/v1/chat` 或 `/api/v1/chat/stream`，不要重复写 `/api/v1`；空值同源 |
| VITE_PROXY_TARGET | `http://127.0.0.1:8010` | 仅 Vite 开发服务器 `/api` 代理目标 |
| VITE_HUMAN_SUPPORT_URL | 空字符串 | 完整 http(s) 人工客服链接；空值/无效值隐藏入口 |
| VITE_STORE_NAME | `灵码` | 店铺名、欢迎语与标题；空白回退灵码，字标保持「灵」 |
| VITE_WELCOME_ANIMATION | 开启（test 模式默认关闭） | `0` / `false` / `off` 关闭欢迎动画；标签页会话内仅播一次，可点击或按 Esc / Enter / 空格跳过，减少动态效果时直接跳过 |
| VITE_MOCK | `0` | 严格为 1 时使用本地 fixtures，不请求后端 |
| VITE_STREAM | `1` | 默认 SSE；严格为 0 才只走 JSON |
| VITE_SHOW_TRACE_TOGGLE | `0` | 严格为 1 时向所有访客显示「查看链路」入口，仅演示使用 |

客户端变量构建时注入，变更后重启开发服务器或重新构建；VITE_* 不能放任何凭据。生产托管 dist，并同源反代 /api；也可构建时设置 VITE_API_BASE，此时后端 CORS_ORIGINS 必须允许实际前端源，默认仅 localhost/127.0.0.1:3000。无健康检查轮询。

## 流式回答与兜底

请求为 `{session_id,message,debug,history}`，默认 fetch + ReadableStream POST SSE，事件如下：

| 事件 | 数据 |
| --- | --- |
| meta | request_id、session_id |
| status | stage：faq / routing / retrieving / generating |
| delta | text：文本增量 |
| done | 完整 ChatResponse，含 answer、route、strategy、sources、is_mock、trace |
| error | code、message、可选 request_id；缺省编号沿用 meta |

解析支持 UTF-8 跨块、CRLF、多行 data，忽略注释心跳和未知事件。首合法业务事件前超时 60 秒，之后每个合法业务事件重置 30 秒空闲计时；注释/未知事件不续期。后端每 10 秒 `: ping` 是传输心跳，不增加回答内容。

在首合法事件前发生网络失败、任意 HTTP 非成功（含 404/405）、Content-Type 非 SSE、空流或事件解析失败时，自动调用一次 `/api/v1/chat`；实际代码兜底范围不限于 404/405。收到合法 meta/status/delta/done/error 后失败或提前 EOF，显示错误并允许手动重试，避免重复回答；超时和主动取消不自动回退。reader 取消/释放失败不会覆盖结果或阻断兜底。

done 前引用 `[n]` 为普通文字，done 后以最终 answer 覆盖增量，并按 sources 显示引用、来源、路由与实际客户端耗时。status 对应「正在查找常见问题…」「正在理解问题…」「正在查阅店铺资料…」「正在生成回答…」。错误按业务码显示固定友好文案，42901 限流、50401 超时、40001 输入非法，其余统一服务繁忙；code/requestId 保留在数据里，气泡不显示编号或后端内部错误文本。

后端总预算默认 40 秒，单次传输/首内容 12 秒、后续 content 空闲 20 秒，已有内容后不重试；前端超时设置不延长后端预算。所有事件和完成结果都核对请求 epoch；新对话、会话切换与卸载会取消旧请求，迟到结果不污染新会话。

## 多轮与历史列表

- 输入按 Unicode 码点计数，最多 500 字，空白不发送；Enter 发送，Shift+Enter 换行，IME 组合期间不发送。请求中锁定发送、快捷提问和重试，新对话与切换会话仍可用。
- 每次发送当前会话最近 3 个已完成无错误问答对，按时间顺序最多 6 条 `{role,content}`；两种角色均截到 500 码点，assistant 先去掉 `[n]`。拒答保留，错误和未完成回合排除；当前问题不放进 history，新对话发送空数组。
- 后端先校验全部 history 再保留最近 6 条，每条最多 1000 字、总 4000 字。有 history 时不读写缓存，先路由再用改写问句查 FAQ/检索/重排，关闭投机检索；服务端不根据 session 自动补历史。
- 当前 UUID 存 `localStorage['ecomrag.session_id']`，列表存 `localStorage['lingma.conversations.v1']`，每项含 id/title/createdAt/updatedAt/messages，时间为 Unix 毫秒。标题取首问前 20 码点；最多 30 个会话，每个最近 100 条已完成消息（50 轮），按 updatedAt 保留最新会话。
- 首轮失败只保留页面上的错误与重试入口，首次完整成功回答（含拒答）后才列入历史；已有成功会话可保留完整错误回合，但不将它发给模型。刷新时不恢复在途请求，未完成 user 不保存。
- 旧 `sessionStorage['ecomrag.messages']` 自动迁移完整回合并删除旧键。解析失败、禁用存储或配额满时退回内存；刷新可能丢失。localStorage 同源标签页共享，但不监听其他标签页变化、不合并并发编辑、不跨设备同步。
- 桌面 ≥1024px 使用默认 260px 侧栏，可拖动分隔线调整至 200–420px，左右方向键每次调整 16px，Home 到最小值，End 或双击恢复默认；宽度保存到 `localStorage['lingma.sidebarWidth.v1']`，损坏或存储不可用时回退默认，写入失败不影响使用。较窄屏从顶栏打开历史抽屉，不应用桌面侧栏宽度，按今天/昨天/近 7 天/更早分组。删除与清空需行内二次确认；切换、新对话、删除当前会话、清空均隔离旧请求。抽屉支持 Esc、遮罩关闭、焦点圈定与恢复、滚动锁，底部保留「历史仅保存在本浏览器」。

## 引用、链路与 Mock

Markdown 支持加粗、列表、换行，不启用 raw HTML；只转换正文文本节点中的有效 `[n]`，代码、已有链接中的编号保持原样，模型图片只显示替代文字。来源默认展开，点击引用会展开来源、滚动、聚焦并临时高亮卡片；卡片只显示编号、标题、摘要，不显示 doc_id。摘要在展示前去掉行首 Markdown 标记和重复标题，合并空白与省略号；空摘要使用独立兜底说明，不改接口与历史数据。

VITE_SHOW_TRACE_TOGGLE=1、URL `?debug=1` 或 `localStorage['ecomrag.debug']='1'` 任一条件才显示「查看链路」。入口可见不会自动开启，状态存 `sessionStorage['ecomrag.trace']`，默认关闭；开启后才发送 debug=true，关闭立即隐藏已有面板。后端仍需 ALLOW_TRACE=true，前端隐藏不是权限边界，长期公开部署恢复关闭。

真实 trace 包含 cache/faq/router/retrieve/rerank/generate/total；字段含 ms、hit、top_score、intent、candidates、kept、speculative。改写补充重排会填写 detail 和合并后 top_score；不再声称真实 detail 一律为空。链路默认折叠，中文映射，耗时整数毫秒、浮点分数两位，未知字段保留展示；并行阶段不能相加当总耗时。

`VITE_MOCK=1 npm run dev` 使用固定关键词响应：秒回→cache、划痕→带三条引用 RAG、火星→拒答、你好→闲聊、退货→FAQ、限流/超时→42901/50401，其余→预设 RAG。错误关键词优先，划痕优先于退货；Mock 不生成新政策，接收 history 但不据此推理。JSON 模拟等待 600–1500ms；流式先发阶段，再按 Unicode 字符每 30–60ms 增量输出，is_mock=true。Mock trace 标注本地模拟耗时，不代表实际性能。

手机宽度 <641px 时欢迎标题与说明仅在当前会话无消息时显示，首条消息发出后隐藏，新建空会话后恢复；桌面行为不变。输入框下方仅保留字数。上滑阅读时保留位置，接近底部才自动跟随；手机快捷问题默认收起。生成中 aria-busy 抑制逐字播报，终态显示完整答案或错误。浏览器软键盘、真实 IME、读屏和实际代理传输需实测。

## 目录

```text
frontend/
├── package.json、tsconfig.json、vite.config.ts
├── tailwind.config.ts、postcss.config.js、index.html、env.sample
├── public/favicon.svg
└── src/
    ├── main.tsx、App.tsx、index.css
    ├── api/          # client、SSE、类型/校验、错误
    ├── components/   # 消息、输入、来源、trace、历史列表
    ├── hooks/        # useChat、滚动、手机/桌面断点
    ├── lib/          # 引用、格式、配置、history、storage、会话时间
    ├── mocks/        # fixtures
    └── test/         # RTL/jest-dom 设置、流和会话工具
```

测试与模块就近放置，覆盖请求、SSE、Mock、引用、输入/IME、历史边界/迁移、切换取消、epoch、存储异常和链路可见性。此目录说明依据 package.json、vite.config.ts 与 src，不把历史交付记录当作当前验证结果。

## 公网部署与常见问题

公网采用本机 + Cloudflare 固定 named tunnel，临时隧道不支持项目所需 SSE；生产链路和 Nginx 示例见[开发文档第 8 节](../docs/电商RAG智能客服-开发文档.md#8-部署防滥用与已知限制)。保留 Content-Type: text/event-stream、Cache-Control: no-cache、X-Accel-Buffering: no、Content-Encoding: identity，关闭 API 的缓存、压缩与缓冲，验证 meta 和 delta 实时到达。

后端只监听 127.0.0.1:8010，使用 --no-proxy-headers。可信固定隧道可设置 TRUST_PROXY_HEADERS=true、CLIENT_IP_HEADER=cf-connecting-ip，socket 对端还须在 TRUSTED_PROXIES（默认回环地址）；中间代理须保留验证后的 CF 头，禁止绕过入口伪造。缺失/非法头或对端不可信时回退 socket 地址，不能靠前端设置真实 IP。

- **3000 启动报占用**：strictPort=true，先释放端口。
- **preview 能看页面但问答失败**：preview 无开发 API 代理，需生产反代或正确的 VITE_API_BASE/CORS。
- **修改变量无效**：env.sample 不自动加载；运行时应传变量，构建变量变更需重新构建，VITE_MOCK 仅 1 开启、VITE_STREAM 仅 0 关闭。
- **人工入口不显示**：VITE_HUMAN_SUPPORT_URL 必须为完整 http(s) 地址；它只是外链，没有工单系统。
- **历史刷新丢失**：仅完整回合会保存，首轮失败不进列表；存储异常只保留内存，不做跨设备同步。
- **流式整段出现或中途失败**：查固定隧道和代理缓冲，流开始后的错误不会自动重复请求，使用重试入口。
- **并发慢**：本机重排串行，既有记录约每分钟 20 个知识库问题；海外到智谱可能抖动，16 GB 内存压力下重排可从约 2.5 秒退化至 20 秒以上，前端超时不能消除这些瓶颈。

## 变更与决策记录

- 后端拒答阈值从 0.5 调为 0.2，减少 child 模式低分应答题误拒；改写补充重排不设相似度门槛，依据 backend/app/pipeline.py、retrieval/rewrite.py 及对应回归测试，前端据最终 route 展示拒答。
- 固定隧道沿用 docs/演示准备.md 的 SSE 部署约定；api/client.ts 在首合法事件前提供 JSON 兜底，已开始的流不自动重发。
- glm-5.3-flash 需后端配置 enabled/low 思考，依据后端诊断与思考参数测试；浏览器不保存或发送供应商凭据。
- 后端 LLM 并发默认 2，来自 config.py/test_settings.py；演示建议 4 来自既有演示配置，仍需核对配额与压测，不改变前端请求契约。
