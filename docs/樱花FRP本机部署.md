# 樱花 FRP 本机演示部署

访问链路：`rag.ailingma.com` → 樱花 HTTP/HTTPS 建站隧道 → 本机 Caddy → 前端构建文件或 `127.0.0.1:8010`（FastAPI）。HTTP 使用本机 `8080`，HTTPS 使用本机 `8443`。

## 当前隧道

- 名称：`ecomrag`
- ID：`29590406`
- 节点：日本2（74）
- 类型：HTTP 建站隧道
- 本地 IP：`127.0.0.1`
- 本地端口：`8080`
- 绑定域名：`rag.ailingma.com`
- 节点域名：`frp-cup.com`（由用户提供的樱花配置确认）。客户端连接端口为 `8088`，此端口不填入 DNS 记录值，也不附加到网站访问地址。

## 本机准备

以下命令均从仓库根目录执行；需要已安装依赖、准备好根目录 `.env`、本地模型，并完成文档及 FAQ 入库。Caddy 使用本机已经安装的程序。

1. 检查依赖服务：

   ```bash
   docker compose ps
   ```

2. 构建前端：

   ```bash
   cd frontend
   npm run build
   cd ..
   ```

   构建应使用真实服务模式，API 基址为空以走同源入口；不要配置指向访客自身电脑的 localhost API 地址。

3. 在一个终端运行后端：

   ```bash
   sh deploy/start-local.sh
   ```

4. 在另一个终端从仓库根目录运行网站入口：

   ```bash
   ECOMRAG_FRONTEND_DIR="$PWD/frontend/dist" caddy validate --config deploy/Caddyfile --adapter caddyfile
   ECOMRAG_FRONTEND_DIR="$PWD/frontend/dist" caddy run --config deploy/Caddyfile --adapter caddyfile
   ```

5. 打开 `http://127.0.0.1:8080`，确认页面及聊天可用。后端状态可在本机通过 `http://127.0.0.1:8010/health` 检查。

后端与 Caddy 均以前台命令运行，关闭对应终端或按 Ctrl+C 将停止对应服务。没有配置开机自启。

## 阿里云 DNS 与客户端

在 `ailingma.com` 的解析设置中添加：

| 字段 | 内容 |
| --- | --- |
| 记录类型 | CNAME |
| 主机记录 | rag |
| 解析线路 | 默认 |
| 记录值 | frp-cup.com |
| TTL | 默认 |

若已有 `rag` 同名记录，先核对冲突，不要直接覆盖其他用途的记录。

在本机官方 macOS Apple Silicon 客户端中配置访问密钥并启动隧道 `29590406`。访问密钥仅由用户在本机客户端填写，不放入本仓库、截图或聊天。隧道变为在线且 DNS 生效后，通过 `http://rag.ailingma.com` 测试。

应另外用国内手机流量验证页面能打开、聊天可完成，以及 SSE 事件逐步到达；本机经当前代理访问成功不能证明国内直连成功。

## HTTPS 配置

`deploy/Caddyfile` 已配置 `rag.ailingma.com` 的自动证书管理。Caddy 通过现有 HTTP 隧道的公网 80 端口完成 HTTP-01 域名验证，并在 `127.0.0.1:8443` 提供 TLS。无需阿里云 DNS API 密钥。

在樱花控制台另建一条隧道：

| 字段 | 内容 |
| --- | --- |
| 节点 | 日本2（74），与现有 HTTP 隧道相同 |
| 类型 | HTTPS 建站隧道 |
| 名称 | ecomrag-https |
| 本地 IP | 127.0.0.1 |
| 本地端口 | 8443 |
| 绑定域名 | rag.ailingma.com |
| 自动 HTTPS | 关闭，本机 Caddy 已处理 TLS |
| 创建 HTTP 重定向 | 关闭，保留现有 HTTP 隧道 |

阿里云 CNAME 保持 `rag` → `frp-cup.com`。创建后在本机启动器刷新并启用新增隧道，再访问 `https://rag.ailingma.com`，公网网址不附加 `8443`。

保留旧 HTTP 隧道，供当前 HTTP 访问及自动证书续期使用；外部 HTTPS 验收完成前不强制将 HTTP 重定向。证书与私钥存放在 Git 忽略的 `logs/caddy-data/`，不要提交、分享或在正常维护时删除该目录。

上线检查应使用系统证书信任验证 HTTPS，不能通过忽略证书错误来判定配置成功。

## 当前限制

- 本机、T7、Docker、后端、Caddy 和樱花客户端需要持续运行。
- 本机已配置 HTTPS，外网 HTTPS 是否可用仍取决于证书签发和新增 HTTPS 隧道的在线状态；HTTP 入口仅用于公开样例测试与域名验证。
- 不信任未经核实的转发 IP 头。`deploy/start-local.sh` 设 `RATE_LIMIT_IP_ENABLED=false`，暂时只使用每会话 50 次/分钟的额度，不再让所有访客共用回环 IP 桶；代码默认仍启用 IP 限流。每会话日额度默认 200，不因并发调到 50 而降低。session 可以更换，这只是避免误伤的临时措施。
- [樱花官方真实 IP 说明](https://doc.natfrp.com/bestpractice/realip.html)：HTTP 隧道会将真实 IP 追加到 XFF 最右侧，前面的值可被访客伪造；HTTPS 隧道需两端配合 Proxy Protocol。当前 Caddy 与隧道未完成该对接，不能直接开启对原始 XFF 的信任。完成透传验证后，应恢复 IP 与会话双重限流。
- `admin off` 禁用 Caddy 管理接口；Caddy 与后端只监听回环地址。
- 不映射 MySQL、Redis、Milvus 或 Vite 开发服务器。
- 备案与账号实名认证依实际部署及平台规则办理，本配置不代表已完成备案。

参考：[樱花 Web 应用指南](https://doc.natfrp.com/app/http.html)、[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)、[Caddy 反向代理说明](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。
