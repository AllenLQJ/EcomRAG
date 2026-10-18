# 栖木 · 电商智能客服前端

React 18 + TypeScript + Vite 5 + Tailwind CSS 3.4，视觉遵循 `docs/UI设计规范.md` 与桌面、手机设计稿。字体全部来自系统字体栈，无 CDN、在线字体、外部图片或运行时图标依赖。

## 安装与启动

需要 Node.js 18.18+（推荐 Node.js 22 LTS）和 npm。在仓库根目录执行：

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:3000>。端口固定 3000，已启用 `strictPort`；端口被占用会明确报错。

```bash
npm test             # Vitest，一次运行
npm run typecheck    # TypeScript 静态检查（包含测试与配置）
npm run build        # tsc --noEmit 后再 vite build，产物在 dist/
npm run preview      # 预览构建产物，不提供 /api 开发代理
```

本次实现未运行任何 npm 命令，未安装依赖、执行测试、类型检查、开发服务或构建。依赖均使用稳定版 caret 范围，首次安装后请在本机检查并保存生成的锁文件。

## 环境变量

模板为 [`env.sample`](./env.sample)，仓库不提供环境文件。可以直接把变量传给命令，不需要新建任何 `.env.*` 文件：

```bash
VITE_STORE_NAME=栖木 VITE_PROXY_TARGET=http://127.0.0.1:8010 npm run dev
```

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `VITE_API_BASE` | 空 | 请求 `${base}/api/v1/chat`；空值走同源开发代理。填写服务 origin，不重复附加 `/api/v1` |
| `VITE_PROXY_TARGET` | `http://127.0.0.1:8010` | 只供 Vite dev server 代理 `/api`，不进入浏览器配置 |
| `VITE_HUMAN_SUPPORT_URL` | 空 | 完整 http(s) 人工客服地址；空值或无效值隐藏顶栏与拒答链接 |
| `VITE_SHOW_SAMPLE_BADGE` | `true` | 仅值为 `false` 时隐藏样例标签；手机端将样例标识放在页脚 |
| `VITE_STORE_NAME` | `栖木` | 店铺名、欢迎语和网页标题；字标保持「栖」 |
| `VITE_SHOW_TRACE_TOGGLE` | `0` | 严格为 `1` 时向所有人显示“查看链路”开关，仅用于演示部署 |
| `VITE_MOCK` | `0` | 严格为 `1` 时只使用本地 fixtures，不请求后端 |

Vite 的客户端变量在构建时注入，改变配置后需要重启开发服务或重新构建。不要把任何密钥放入 `VITE_*`。开发代理目标仅在 Vite 配置读取；没有前端健康检查轮询。

## Mock 演示

```bash
VITE_MOCK=1 npm run dev
```

每次模拟等待 600–1500ms，返回 `is_mock: true`，回答旁显示 Mock 样例标识。输入「秒回」显示缓存命中的「秒回」来源标签；输入「划痕」显示三条引用的 RAG 回答（包含相邻 `[1][2]`，用于验收间距）；「火星」显示拒答；「你好」显示闲聊；「退货」显示 FAQ；「限流」和「超时」分别模拟 42901、50401；其他输入显示样例 RAG。错误关键词优先匹配，其次「秒回」，划痕优先于退货关键词。Mock 只是固定样例，不会针对其他问题生成政策。

构建时 `VITE_SHOW_TRACE_TOGGLE=1`，或访问 <http://localhost:3000/?debug=1>，或设置 `localStorage.setItem('ecomrag.debug', '1')`，才显示页头「查看链路」按钮。开发者标志只决定入口可见，不自动请求 debug；开关默认关闭，点击开启后请求才带 `debug: true` 并显示回答下方的非空链路折叠区。开关存在 `sessionStorage['ecomrag.trace']`，同标签页刷新保留；关闭后立即隐藏所有链路面板，后续请求带 `debug: false`，即使响应或恢复消息含 trace 也不展示。所有 storage 读写异常均退回内存状态，不影响切换与发送。移除 URL 参数和 `localStorage['ecomrag.debug']`，并保持构建配置为 0，可隐藏入口。

前端隐藏不是安全边界。后端 `ALLOW_TRACE` 默认 `false`，只有请求 `debug: true` 且后端允许时才返回 trace；仅演示环境才开启，公网长期运行的版本应改回 `false`，前端 `VITE_SHOW_TRACE_TOGGLE` 也应恢复为 0。Mock RAG 开启后返回六阶段演示数据，候选数为 10，说明保留「本地模拟耗时」。

已只读核对 `backend/app/pipeline.py`、`contracts.py`、`schemas.py`、`tracing.py` 及 FAQ/检索的记录调用：实际阶段为 cache、faq、router、retrieve、rerank、generate、total；当前记录字段为 stage、ms、hit、top_score、intent、candidates、kept，推测检索时另带 speculative。schema 还定义 route、strategy、detail（当前真实记录未填，可能序列化为 null；Mock 填入 strategy/detail）。当前 trace 不记录问题或答案原文、重写问题、文档正文、API key 或凭据；`contracts.py` 的 question/answer、rewritten_query/content 不会被这些 trace 调用写入。trace 仍暴露路由意图、命中情况、数量和耗时，后端开关需继续限制。未知阶段与字段由前端原样显示。

## 与后端联调

本前端默认后端端口 **8010**。根 README 里的后端命令原用 8000；联调时请在 `backend/` 使用：

```bash
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010 --reload
```

再在 `frontend/` 执行 `npm run dev`，确保 `VITE_MOCK` 未开启。浏览器请求同源 `/api/v1/chat`，开发服务器转发到 8010。后端 CORS 默认允许 `http://localhost:3000` 与 `http://127.0.0.1:3000`；使用其他 origin 或直接设置 `VITE_API_BASE` 跨域访问时，需同步配置后端 CORS。生产环境用 Web 服务器托管 `dist/` 并反代 `/api`，或构建时设置 `VITE_API_BASE`。

请求严格发送 `{ session_id, message, debug }`，输入按 Unicode 码点计数，上限 500 字，空白不可发送。请求 60 秒后通过 AbortController 中止并显示超时；耗时由客户端实际测量。新对话也会中止旧请求并忽略迟到结果。错误信息按业务码映射，不把后端内部错误原文展示给顾客。

## 交互与存储

- 手机（≤640px）使用短占位「输入你的问题」；快捷问题默认收起，点「常见问题」展开后换行排列，选中即发送并收起。桌面保持原占位与快捷问题样式。手机欢迎标题及说明常驻消息滚动区上方，与 Mobile.dc.html 一致，桌面仅空状态显示。
- Enter 发送，Shift+Enter 换行；IME 组合期间的 Enter 不发送。请求期间锁定输入、发送、快捷提问与重试；新对话仍可用。
- 匿名 UUID 存在 `localStorage['ecomrag.session_id']`；消息存在 `sessionStorage['ecomrag.messages']`，刷新恢复、关闭标签页由浏览器清理。新对话清空并更换 UUID。存储被禁用、内容损坏或配额满时继续以内存方式工作。
- Markdown 支持加粗、列表、换行，不启用 raw HTML。仅语法树文本节点中的有效 `[n]` 会转为引用按钮，代码块、行内代码、已有链接内的编号保持原样；未知编号显示原文。模型给出的图片仅显示替代文本，不自动加载外部资源。
- 来源默认展开，点击或按 Enter 激活引用会展开来源、滚动、聚焦并暂时高亮对应卡片；trace 仅在开发者开关开启且非空时出现，默认折叠，阶段与字段中文显示。来源卡片只显示编号、标题、摘要，不显示 doc_id（包含 FAQ），字段仍保留在数据和类型中。
- 接近底部时跟随新消息；上滑阅读时保留位置。手机触控目标至少 44px：引用使用独立的「引用」按钮组，相邻热区不重叠；桌面行内引用为紧凑的至少 24×24px 按钮，不撑开行高。快捷标签外层点击区至少 44px；减弱动态效果设置下关闭脉冲。

## 本轮视觉规范补充

受“只修改 frontend/”限制，本轮未编辑 `docs/UI设计规范.md`；其中旧规则“15px 正文”“11–12px 引用编号”“卡片含文件名”应以这里为准：回答正文 16px / 500（系统字体栈含非可变字体），加粗 600；用户正文 16px，所有辅助小字至少 12px；引用摘要 14px，使用 `body-muted` 语义 token，在卡片白底上对比度约 9.2:1。卡片仅含编号、标题、摘要。所有界面配色使用 Tailwind token；favicon 的独立 SVG 文档由 Vite 在开发与构建时注入 ink/canvas token，避免另写色值。

## 目录

```text
frontend/
├── package.json、tsconfig.json、vite.config.ts
├── tailwind.config.ts、postcss.config.js、index.html、env.sample
└── src/
    ├── main.tsx、App.tsx、index.css
    ├── api/          # 请求/响应类型、校验、错误映射、可替换的 API client
    ├── components/   # 顶栏、输入、Markdown、来源、trace、消息
    ├── hooks/        # 会话状态与滚动跟随
    ├── lib/          # 引用语法树转换、格式化、安全存储、配置
    ├── mocks/        # 关键词匹配的本地响应
    └── test/         # RTL / jest-dom 的全局设置
```

测试与模块就近放置：`format.test.ts`（中文与未知字段映射）、`citations.test.tsx`、`client.test.ts`、`Composer.test.tsx`、`App.test.tsx`、`storage.test.ts`、`fixtures.test.ts`。补充手机收起/展开/发送后收起、短占位与断点切换、独立引用按钮组、欢迎区保留、来源文件名消失、trace 默认关闭/入口条件/会话持久化/隐藏服务端 trace/存储异常、缓存样例与相邻引用；保留原有测试覆盖，包括引用边界与代码跳过、所有错误码/网络/非 JSON/超时、输入与 IME、聊天及重试与新会话、存储异常与恢复、trace、debug 与 Mock。

## 已知限制与待验证

- 本期为非流式单页，不做登录、个人订单查询、真实人工工单、深色模式；人工入口只是配置的链接。
- 若在请求中刷新，已发送的问题会保留，但浏览器不能恢复该在途请求；可重新提问。存储不可用时刷新无法保留会话。
- localStorage 的会话 ID 在同源标签页共享，消息按标签页独立。某标签页新开对话会更新共享 ID，其他已打开标签页仍保持其当前会话直到刷新。
- `env.sample` 仅作模板，不会被 Vite 自动加载。生产静态预览需要另外配置 API 服务。
- 尚未验证：依赖安装与解析、TypeScript 检查、Vitest 测试、Vite 构建、浏览器视觉与移动端软键盘/安全区、真实 IME 与读屏、开发代理及真实后端联调。请在本机逐项验收。
