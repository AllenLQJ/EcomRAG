# 聊天商品卡片

依据 [商品卡片接口说明](../docs/商品卡片接口说明.md) 与 [账号与历史接口说明](../docs/账号与历史接口说明.md)。仅展示 JSON 响应或 SSE `done` 中的可选 `products`，最多三张，沿用接口顺序。名称、品类及一至两条 `highlights` 按纯文本展示，无价格、库存、链接或额外焦点停靠。「资料关联商品」表示引用资料相关商品，不表示逐项推荐。

## 图片部署

- 完整 HTTP(S) `image_url` 直接使用。
- 相对路径 `<sku>/main.webp`（及接口允许的 PNG/JPG/JPEG/SVG）使用图片根地址拼接：**未设置 `VITE_PRODUCT_IMAGE_BASE_URL` 时默认同源 `/images`**（由 `deploy/Caddyfile` 的 `handle_path /images/*` 托管 `data/images`，需设置环境变量 `ECOMRAG_IMAGES_DIR` 为该目录的绝对路径）；也可构建时显式设置，例如 `https://images.example.com/products` 或同源 `/products`。图片根地址与 `VITE_API_BASE` 独立，配置路径前缀会被保留。
- 相对路径未配置图片根地址、图片为空、URL 无效或加载失败，统一显示组件内 SVG/文字占位，无额外占位文件请求。图片与占位均固定 4:3，图片使用商品名 alt 与懒加载。
- 前端不提供静态图片服务或改动代理。部署方需确定公开图片根地址，并托管后端 `data/images/`；若后端 `IMAGE_BASE_URL` 已生成完整地址，无须额外前端配置。

## 显示与历史

窄屏单列，≥641px 两列，≥768px 三列。使用现有 `card/ink/line/muted` 等设计令牌，以及 Tailwind 默认的系统深色媒体查询；不修改全局主题或引入独立主题开关。流式仅显示增量文字，最终事件后挂载卡片。重试/重新生成沿用替换整条助手消息的行为，开始时移除旧卡片，并从请求上下文排除被替换的回答。

服务器历史契约仅保存正文、route、sources，不保存 `products`。当前页面刚生成的回答可展示卡片，重新打开服务器历史后不展示，不从型号、正文或引用伪造，也不额外查询商品。

访客本地历史沿用现有消息结构保留卡片：只保留 `sku/name/category/highlights/image_url`，最多三张和两条规格，限制字段长度，全部卡片紧凑 JSON（含转义）的 UTF-8 总计不超过 8 KiB。本地保存时也参考 32 KiB 单消息预算逐张移除超出预算的卡片；若原消息正文、来源等已超过预算，则仅省略卡片，保留原有历史行为。已有本地历史总量 2 MiB 字符上限和配额失败内存回退仍生效；没有卡片的旧消息兼容。服务端规定的单条消息 32 KiB 上限仍由后端执行，前端不会将卡片上传到历史接口或模型上下文，请求 `history` 始终仅含既有的 `{role, content}`。

## 验证

新增组件、图片路径、API、完整聊天与历史测试，覆盖有无卡片、一至三张/超量、纯文本规格、占位、响应式类名、JSON/SSE 最终事件、重试/重新生成清除、本地恢复和服务器历史不补造卡片。

本次沙箱中已尝试 `npm test` 与 `npm run build`，两者均在启动前被拒绝，输出 `zsh:1: operation not permitted: npm`（退出码 127）。因此测试及构建尚未经运行验证，需在允许 Node/npm 的环境执行；未安装依赖、读取 `.env*` 或运行真实后端。

需部署方决定的只有公开图片根地址及托管方式；前端默认在无法解析相对地址时展示占位。无需决定商品点击行为，本次保持不可点击。

## 改动文件

新增：

- `src/components/ProductCards.tsx`、`src/components/ProductCards.test.tsx`
- `src/api/products.ts`、`src/api/products.test.ts`
- `src/lib/productImages.ts`、`src/lib/productImages.test.ts`、`src/lib/productHistory.test.ts`
- `src/ProductChat.test.tsx`、`src/test/products.ts`
- `PRODUCT_CARDS.md`

修改：`src/components/ChatMessage.tsx`、`src/api/types.ts`、`src/api/client.ts`、`src/hooks/useChat.ts`、`src/lib/storage.ts`、`src/vite-env.d.ts`。未修改 App、账号/店铺组件、全局样式或并行任务列出的文件，未提交。
