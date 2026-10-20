# 灵码 · 电商智能客服前端

React 18 + TypeScript 5 + Vite 5 + Tailwind CSS 3.4 + react-markdown 9，支持访客与账号的聊天界面，含入口登录页、SSE/JSON、引用、链路面板、多轮、浏览器与服务器历史列表、配额及店铺商品。视觉使用仓库设计 token 和系统字体栈，无在线字体或图片 CDN 依赖；店名默认「灵码」，「栖光」是台灯型号。

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

默认浏览器向同源 `/api/v1/chat/stream` 请求，Vite 把 `/api` 转到 `http://127.0.0.1:8010`；不代理 `/health`。后端依赖、入库及启动准备见[根 README](../README.md)。聊天 fixtures 可用 `VITE_MOCK=1 npm run dev`；账号与历史仍请求 API，未连接后端时保持身份未知并有限重试，不直接开放访客聊天。

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
| VITE_API_BASE | 空字符串 | API origin；自动追加 `/api/v1` 与具体接口路径，不要重复写 `/api/v1`；空值同源 |
| VITE_PROXY_TARGET | `http://127.0.0.1:8010` | 仅 Vite 开发服务器 `/api` 代理目标 |
| VITE_HUMAN_SUPPORT_URL | 空字符串 | 完整 http(s) 人工客服链接；空值/无效值隐藏入口 |
| VITE_STORE_NAME | `灵码` | 店铺名、欢迎语与标题；空白回退灵码，字标保持「灵」 |
| VITE_BRAND_SLOGAN | `买前问一问，买后有人管` | 入口品牌标语；与店名一起由 `src/lib/config.ts` 提供，空白回退默认文案 |
| VITE_WELCOME_ANIMATION | 开启（test 模式默认关闭） | `0` / `false` / `off` 关闭欢迎动画；标签页会话内仅播一次，可点击或按 Esc / Enter / 空格跳过，减少动态效果时直接跳过 |
| VITE_MOCK | `0` | 严格为 1 时聊天使用本地 fixtures；账号、配额与服务器历史仍走 API |
| VITE_STREAM | `1` | 默认 SSE；严格为 0 才只走 JSON |
| VITE_SHOW_TRACE_TOGGLE | `0` | 严格为 1 时向所有访客显示「查看链路」入口，仅演示使用 |

客户端变量构建时注入，变更后重启开发服务器或重新构建；VITE_* 不能放任何凭据。生产托管 dist，并同源反代 /api；VITE_API_BASE 可指定 API 基础地址，同源请求使用 credentials: same-origin，跨源绝对基址使用 include，覆盖账号、历史、JSON 与 SSE。跨源仍需服务端允许凭据的 CORS，跨站 Cookie 受 SameSite 限制，建议同站部署。无健康检查轮询。

## 流式回答与兜底

请求为 `{session_id,message,debug,history,conversation_id?}`，默认 fetch + ReadableStream POST SSE，事件如下：

| 事件 | 数据 |
| --- | --- |
| meta | request_id、session_id |
| status | stage：faq / routing / retrieving / generating |
| delta | text：文本增量 |
| done | 完整 ChatResponse，含 answer、route、strategy、sources、is_mock、trace |
| error | code、message、可选 request_id；缺省编号沿用 meta |

解析支持 UTF-8 跨块、CRLF、多行 data，忽略注释心跳和未知事件。首合法业务事件前超时 60 秒，之后每个合法业务事件重置 30 秒空闲计时；注释/未知事件不续期。后端每 10 秒 `: ping` 是传输心跳，不增加回答内容。

在首合法事件前发生网络失败、未识别为业务错误的 HTTP 非成功（含 404/405）、Content-Type 非 SSE、空流或事件解析失败时，自动调用一次 `/api/v1/chat`；实际代码兜底范围不限于 404/405。HTTP 业务拒绝（40001/40101/40301/42901/42902/50301）直接展示，不重复提交；收到合法 meta/status/delta/done/error 后失败或提前 EOF，显示错误并允许手动重试，避免重复回答；超时和主动取消不自动回退。reader 取消/释放失败不会覆盖结果或阻断兜底。

done 前引用 `[n]` 为普通文字，done 后以最终 answer 覆盖增量，并按 sources 显示引用、来源、路由与实际客户端耗时。status 对应「正在查找常见问题…」「正在理解问题…」「正在查阅店铺资料…」「正在生成回答…」。错误按业务码显示固定友好文案，42901 限流、50401 超时、40001 输入非法，42902 区分访客引导登录与账号明天再试；其余统一服务繁忙；code/requestId 保留在数据里，气泡不显示编号或后端内部错误文本。

后端总预算默认 40 秒，单次传输/首内容 12 秒、后续 content 空闲 20 秒，已有内容后不重试；前端超时设置不延长后端预算。所有事件和完成结果都核对请求 epoch；新对话、会话切换与卸载会取消旧请求，迟到结果不污染新会话。

## 多轮与访客历史列表

- 输入按 Unicode 码点计数，最多 500 字，空白不发送；Enter 发送，Shift+Enter 换行，IME 组合期间不发送。请求中锁定发送、快捷提问和重试，新对话与切换会话仍可用。
- 每次发送当前会话最近 3 个已完成无错误问答对，按时间顺序最多 6 条 `{role,content}`；两种角色均截到 500 码点，assistant 先去掉 `[n]`。拒答保留，错误和未完成回合排除；当前问题不放进 history，新对话发送空数组。
- 后端先校验全部 history 再保留最近 6 条，每条最多 1000 字、总 4000 字。有 history 时不读写缓存，先路由再用改写问句查 FAQ/检索/重排，关闭投机检索；服务端不根据 session 自动补历史。
- 当前 UUID 存 `localStorage['ecomrag.session_id']`，列表存 `localStorage['lingma.conversations.v1']`，每项含 id/title/createdAt/updatedAt/messages，时间为 Unix 毫秒。标题取首问前 20 码点；最多 30 个会话，每个最近 100 条已完成消息（50 轮），按 updatedAt 保留最新会话。
- 首轮失败只保留页面上的错误与重试入口，首次完整成功回答（含拒答）后才列入历史；已有成功会话可保留完整错误回合，但不将它发给模型。刷新时不恢复在途请求，未完成 user 不保存。
- 旧 `sessionStorage['ecomrag.messages']` 自动迁移完整回合并删除旧键。解析失败、禁用存储或配额满时退回内存；刷新可能丢失。localStorage 同源标签页共享，但不监听其他标签页变化、不合并并发编辑、不跨设备同步。
- 桌面 ≥1024px 使用默认 260px 侧栏，可拖动分隔线调整至 200–420px，左右方向键每次调整 16px，Home 到最小值，End 或双击恢复默认；宽度保存到 `localStorage['lingma.sidebarWidth.v1']`，损坏或存储不可用时回退默认，写入失败不影响使用。较窄屏从顶栏打开历史抽屉，不应用桌面侧栏宽度，按今天/昨天/近 7 天/更早分组。删除需行内二次确认；切换、新对话、删除当前会话均隔离旧请求。抽屉左上角保留侧栏切换图标，不显示“关闭”文字；可再次点图标、点击遮罩或按 Esc 关闭，支持焦点圈定与恢复及滚动锁。打开时先聚焦抽屉内的侧栏图标，关闭后回到原顶栏图标；删除确认的首次 Esc 仍只取消删除。

## 引用、链路与 Mock

Markdown 支持加粗、列表、换行，不启用 raw HTML；只转换正文文本节点中的有效 `[n]`，代码、已有链接中的编号保持原样，模型图片只显示替代文字。来源默认收缩，仅显示“引用来源（N）”标题按钮，点击或用 Enter/Space 展开/收起，使用与商品卡片详情、链路一致的 `disclosure-panel/trigger/marker` 样式：白底、细边框、相同圆角和内边距、13px 字号、左侧三角指示（收起朝右、展开朝下），并以 `aria-expanded` / `aria-controls` 关联隐藏详情。每条回答独立保存展开状态；流式增量期间不显示来源列表，完成后挂载的来源区域同样默认收缩，JSON 回答也默认收缩。主动点击正文引用仍会展开来源、滚动、聚焦并临时高亮对应卡片；卡片只显示编号、标题、摘要，不显示 doc_id。摘要在展示前去掉行首 Markdown 标记和重复标题，合并空白与省略号；空摘要使用独立兜底说明，不改接口与历史数据。

VITE_SHOW_TRACE_TOGGLE=1、URL `?debug=1` 或 `localStorage['ecomrag.debug']='1'` 任一条件才显示「查看链路」。入口可见不会自动开启，状态存 `sessionStorage['ecomrag.trace']`，默认关闭；开启后才发送 debug=true，关闭立即隐藏已有面板。后端仍需 ALLOW_TRACE=true，前端隐藏不是权限边界，长期公开部署恢复关闭。

真实 trace 包含 cache/faq/router/retrieve/rerank/generate/total；字段含 ms、hit、top_score、intent、candidates、kept、speculative。改写补充重排会填写 detail 和合并后 top_score；不再声称真实 detail 一律为空。链路默认折叠，中文映射，耗时整数毫秒、浮点分数两位，未知字段保留展示；并行阶段不能相加当总耗时。

`VITE_MOCK=1 npm run dev` 使用固定关键词响应：秒回→cache、划痕→带三条引用 RAG、火星→拒答、你好→闲聊、退货→FAQ、限流/超时→42901/50401，其余→预设 RAG。错误关键词优先，划痕优先于退货；Mock 不生成新政策，接收 history 但不据此推理。JSON 模拟等待 600–1500ms；流式先发阶段，再按 Unicode 字符每 30–60ms 增量输出，is_mock=true。Mock trace 标注本地模拟耗时，不代表实际性能。

手机宽度 ≤639px 时欢迎标题与说明仅在当前会话无消息时显示，首条消息发出后隐藏，新建空会话后恢复；桌面行为不变。Tailwind sm 从 640px 开始，与 useMobile 的 max-width: 639px 对齐。输入框下方仅保留字数。上滑阅读时保留位置，接近底部才自动跟随；手机快捷问题默认收起。生成中 aria-busy 抑制逐字播报，终态显示完整答案或错误。浏览器软键盘、真实 IME、读屏和实际代理传输需实测。

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


## 入口、账号与配额

- 启动先 GET `/api/v1/auth/me`。已登录直接进入首页，欢迎动效仍遵循每个标签页会话一次；未登录且本会话未选择访客时显示全屏入口页，该页不挂载 WelcomeSplash，也没有入场动画。
- 入口按确认样式呈现：直接引用现有 `/brand/lingma-b-seal.svg`，桌面 104px、手机（≤639px）84px；其下为 30px 衬线店名、可配置的 15px 品牌标语及带两侧细线的「智能客服」。字体使用既有 Noto Serif SC / Songti SC / serif 字体栈，不下载字体。
- 内容最大宽 360px，左右至少留 16px，品牌与卡片间隔 30px。白底卡片采用 0.5px 细边框、16px 圆角、22px 内边距；输入框高 44px，墨色提交与访客次要按钮均高 46px，中间显示「或」。注册提交文案为「注册并进入」，提示条含演示密码提醒；卡片下方间隔 16px 显示模拟店铺免责声明（复用 `shopInfo.ts`）。颜色使用现有设计令牌，入口专用样式不修改聊天首页或弹窗的布局。
- 入口页登录、注册成功或点击「以访客进入」时，先清除 `sessionStorage['lingma.welcome.v1']` 再挂载首页，沿用 `VITE_WELCOME_ANIMATION` 与 prefers-reduced-motion。`sessionStorage['lingma.entry.guest.v1']='1'` 记录访客选择，刷新保留，标签页会话结束后失效；浏览器的恢复标签页功能可能恢复 sessionStorage。存储被禁用时当前页面仍可进入。
- 访客在侧栏打开「登录/注册」弹窗，复用入口表单；弹窗支持 Esc、焦点圈定与关闭后焦点恢复，不重播欢迎动效。开始退出登录时立即中止流式并禁用 Composer；退出登录、注销账号成功后清除上述两键，返回没有欢迎动效的入口页；游客 localStorage 历史仍保留。访客流式中登录成功也会中止旧流式，账号聊天使用独立历史。
- 用户名校验为 3–20 位字母、数字、下划线；注册密码为 8–64 个 Unicode 字符，并校验强度。登录密码仅拦截超过 64 字符的输入，短口令交给服务端认证，不展示注册规则；密码输入使用 current-password / new-password。提交时防重复，42903 根据 retry_after_seconds 倒计时。注销账号须在二次确认弹窗输入密码。
- 侧栏展示服务端 quota 中的 remaining/limit；每次聊天请求结束（成功、错误）重新查询身份与配额，主动取消不触发旧请求的配额刷新。42902 使用 limit_scope 展示访客登录引导或账号明天再试，并阻止继续发送，仍可编辑输入内容。启动身份查询失败时保留身份未知，不挂载聊天；分别等待 1 秒、2 秒重试，最多请求 3 次，再提供手动重试。仅成功返回 user:null 才按游客展示。后续配额查询失败保留最后数据并提示；账号/历史请求有 15 秒超时。
- 品牌标语可用新增的 `VITE_BRAND_SLOGAN` 替换；欢迎动效配置与存储键保持原有规则，测试默认关闭，涉及动效的用例显式开启并使用假定时器。身份与密码不写入浏览器存储。

## 账号与历史接口约定

所有接口前缀为 `/api/v1`，统一 API 层按 API 地址来源选择 credentials：同源为 `'same-origin'`，跨源为 `'include'`。账号与历史的 POST/PATCH/DELETE 添加 `X-Requested-With: ecomrag`；GET 与聊天 POST 不添加该头。SSE 的非 2xx 响应只要包含数值业务 code 就直接抛出，不通过 JSON 重发问题。

| 接口 | 请求 / 返回摘要 |
| --- | --- |
| GET /auth/me | `{user: null 或 {id,username}, quota:{role,limit,used,remaining,resets_at}}` |
| POST /auth/register、/auth/login | `{username,password}` → `{user}`，分别 201 / 200 |
| POST /auth/logout | 200 |
| DELETE /auth/account | `{password}` → 200 |
| GET /conversations | `{items:[{id,title,updated_at,message_count}]}` |
| POST /conversations | `{}`（支持约定的可选 title），201 会话摘要 |
| GET /conversations/{id} | `{id,title,messages:[{role,content,route,sources,created_at}]}` |
| PATCH /conversations/{id} | `{title}` |
| DELETE /conversations/{id} | 删除会话 |
| POST /chat、/chat/stream | 登录且已取得对话 ID 时附加 `conversation_id`；后端自行持久化消息 |

错误沿用 `{code,message,request_id?}`，账号表单展示中文原因；40001 参数、40101 凭据/身份、40301 拒绝、40401 不存在、40901 重名、42901 提问限速、42902 今日配额（limit_scope）、42903 登录限速（retry_after_seconds）、50301 暂不可用。

登录后只显示服务器列表，不合并、上传或覆盖游客历史；打开时按需拉取消息，支持行内改名与二次确认删除。新对话延迟到首次发送前创建，聊天完成后重新获取列表，以服务端标题为准。多轮 history 仍沿用最近三轮拼装规则。创建/打开/改名/删除失败保留输入与已有内容并显示重试提示；刷新历史列表按钮可重新加载列表。请求取消、切换与迟到响应使用 epoch 隔离。

「店铺商品」入口在所有宽度下固定于聊天页右下角，不再放在顶栏或推荐问题行；推荐问题展开时仍可用。按钮最终采用方案 D：`card` 白底、1.5px `ink` 黑色边框、胶囊圆角、黑色 17px 衬线文字（与店名同一套 `font-serif`）、3px 字间距，文字后跟 7px 橙色圆点。圆点复用现有 `spark` 令牌，与仓库 logo 实际橙色 `#E07A3F` 一致；悬停切换为黑底白字并上浮 2px，橙点保持，不使用整体亮度滤镜。最小高度 44px，右侧至少留 24px 并考虑安全区，避开滚动条；输入区底部为按钮预留 2.75rem 高度及 1rem 间隔（默认字号下为 44px/16px），另保留底部安全区，使按钮与输入框、发送和推荐问题胶囊错开。文字计数、输入框增高和问题换行不改变这块预留空间。

弹窗顶部左侧复用顶栏 `LogoMark` 资源，右侧显示「灵码」及「用心挑选日常好物，让科技与生活轻松相伴。」，接着直接展示商品品类；没有额外可见的弹窗标题、免责声明、客服与承诺小节。弹窗可滚动，右上角保留「关闭」，可访问名称及关闭按钮分别为「店铺商品」「关闭店铺商品」，支持 Esc、焦点圈定和关闭后回到悬浮入口。所有店铺事实仍集中于 `src/shopInfo.ts`，本次只改变展示，入口登录页仍可复用其中的模拟店铺声明；品类卡片仅填入问题草稿，不自动发送、不触发欢迎动效，不修改后端契约。

## 本地联调与验收

已有依赖的环境中，在 frontend 下运行 `npm test`、`npm run typecheck`、`npm run build`。新增 Entry / ServerHistory / AuthForm / API 用例全部用 fetch mock，不连接真实服务；原有聊天回归用例改为直接挂载同一个 ChatHome，保留原断言，入口与身份启动由 Entry 用例覆盖。入口样式回归同时核对固定墨印素材、品牌文案、卡片内访客入口、注册文案，以及 WelcomeSplash 和入场动画类名均不存在；配置测试覆盖标语替换与空值回退。不得以 mock 测试代替真实浏览器验收。

无需读取 `.env*` 时使用 `VITE_SKIP_ENV_FILES=1 npm test` 与 `VITE_SKIP_ENV_FILES=1 npm run build`；配置跳过显式 loadEnv，并将 Vite 5 的自动环境文件搜索目录改为默认不存在的 `.vite-no-env`（请勿在其中放置配置文件）。ShopBreakpoint 用真实 Tailwind 配置生成 CSS，再按模拟视口展开媒体规则，覆盖 320/390/639/640/641/1024px 的唯一悬浮入口、缩放和推荐问题展开，校验方案 D 的白底黑字、1.5px 边框、胶囊圆角、品牌字体、17px 字号、3px 字距、7px logo 同色橙点、悬停反色及上浮、固定定位、安全区及输入区预留空间；Entry/History 覆盖店铺弹窗内容、品类填草稿、两种关闭入口、Esc、Tab 圈定与焦点恢复。AuthStream 覆盖流式中登出、重新登录和游客流式中登录。jsdom 不计算真实元素几何，仍需浏览器检查滚动条、软键盘和极长内容的实际布局。

获准进行真实联调后，可用 `VITE_PROXY_TARGET=http://127.0.0.1:8010 npm run dev` 将浏览器同源 `/api` 代理给另一任务提供的后端；无需配置跨源 Cookie。验证登录 Cookie、配额实际扣减、服务器标题/消息持久化、跨设备历史和错误信封，再在真实手机检查软键盘、焦点与动效。真实浏览器及后端联调必须由总控验收；本次开发不运行真实后端、不联网、不安装依赖。
