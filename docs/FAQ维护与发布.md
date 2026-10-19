# FAQ 维护与发布

FAQ 发布包含 MySQL 提交、Redis 版本通知、各服务进程后台构建快照三个阶段。有实际变化的导入退出成功表示提交和通知成功；无变化时会明确提示“无变化，未发布”。所有 worker 生效需要逐一确认。本机制不新增 HTTP 管理接口，也不修改 `data/faq.csv` 内容。

## 发布流程

在已有依赖和连接配置的环境中，从仓库根目录进入 `backend` 运行下面的命令。本次开发的测试均注入替身，不需要配置文件、真实 MySQL/Redis 或模型；不要把凭据写进 CSV、命令参数或提交记录。

```bash
cd backend
python -m ingest.load_faq ../data/faq.csv --dry-run
python -m ingest.load_faq ../data/faq.csv
```

1. 先执行 `--dry-run`。它仅校验 CSV 并 SELECT 当前 FAQ，输出新增、修改、未变化、将被忽略的条数；不执行 INSERT/UPDATE/DELETE，不提交、不创建备份、不访问 Redis。它只需要读取 FAQ 表的权限；连接关闭时数据库可能结束只读事务。
2. 核对差异和近重复提示。导入表头必须严格为 `question,answer,category`，顺序不可改变；允许 UTF-8 BOM。问题和答案去除首尾空白后必须非空，问题最多 500 字，分类仅允许 `after_sales`、`logistics`、`payment`、`product`、`promotion`。完全相同的问题保留最后一行，前面的行计入忽略；按缓存规范化后相同的不同问句会提示所在行号，需人工核对答案一致性。近重复提示不会自动合并语义相近的问题。
3. 正式导入持有 `ecomrag_faq_import` MySQL 命名锁，先读出现状并完成备份，再在事务中按问题更新/插入，最后提交。备份失败时不修改数据。问题使用二进制精确比较，防止 MySQL 大小写不敏感的排序规则导致 prune 误删近重复问句。所有同类工具共用该锁；直接 SQL 修改不受该锁保护，发布期间应避免其他方式写表。
4. 备份位于仓库根目录 `backups/faq/faq-<UTC时间戳>.csv`，已加入 `.gitignore`。备份以独占方式创建且文件权限为 `0600`，包含所有行的 `id,question,answer,category,enabled,updated_at`，因此停用状态、重复行、ID 和更新时间都能恢复；不包含数据库/Redis 凭据。备份表头专用于恢复，不应传给普通导入命令。请按本地数据保护和留存要求管理备份。
5. 仅当有新增、修改或 prune 删除时，提交后才 `INCR faq:data:version`，然后用现有 SCAN 分批删除 `faq:cache:*`。无实际变化的正式导入仍备份当前状态并结束只读事务，但不执行 FAQ 写入、不递增版本、不清理缓存，输出“无变化，未发布”。保留版本键、限流计数和其他业务键；不会执行 FLUSHDB。数据版本键应持久保留，不设置 TTL，也不要在常规清缓存时删除。
6. 使用正常 FAQ 请求逐一访问实际服务进程/实例，检查新答案与日志 `FAQ 快照已生效 version=N`。默认最多每 3 秒检查一次 Redis，空闲 worker 在下一次 FAQ 查询时检查。重建完成还需要数据库、模型编码和线程池调度时间；命令成功不代表所有进程已经切换。新启动进程在初始化构建前后读取 Redis 版本（构建后强制读取一次，绕过 TTL）；前后相同则直接对齐，不会在首次请求时重复整表编码。构建期间有发布或 Redis 故障时保留构建前的版本，恢复后继续追赶，避免误认旧快照为最新。

默认导入会保留 CSV 中不存在的数据库问题。需要同步删除时，先预览清单，再显式确认：

```bash
python -m ingest.load_faq ../data/faq.csv --dry-run --prune
python -m ingest.load_faq ../data/faq.csv --prune --yes
```

`--prune` 包括停用行，会打印所有将删除的问题；正式执行即使清单为空也必须提供 `--yes`。未确认时不备份、不修改数据库。备份仍保存删除前的完整状态。

普通导入沿用已有启用语义：更新已存在的问题不会改变其 `enabled`，新增问题使用表默认的启用状态。CSV 中写入一个原本停用的问题不会自动将它启用。

## 回滚步骤

核对导入时打印的备份路径；在恢复前确认该备份对应需要回到的发布状态。

```bash
# 未提供 --yes 时仅校验备份并提示行数，不连接数据库。
python -m ingest.faq_restore --from backups/faq/faq-20261019T120000.123456Z.csv
python -m ingest.faq_restore --from backups/faq/faq-20261019T120000.123456Z.csv --yes
```

相对路径先按当前目录查找，再按仓库根目录查找。恢复会先在同一目录再备份当前状态，再在事务中删除当前 FAQ 并按备份完整恢复；保留备份中的 ID、停用状态和更新时间。AUTO_INCREMENT 的内部计数器不属于 CSV 备份。恢复后同样递增 Redis 版本并只清理 FAQ 缓存，再按发布流程逐一确认 worker。恢复或导入过程中数据库写入失败会回滚事务，已经写好的备份会保留。

## 多进程生效与并发

每个 `FaqService` 持有完整的 BM25 和问题向量快照。每次 FAQ 查询前检查专用版本键，进程内短 TTL 合并 Redis GET；同进程只允许一个版本检查和一个重建任务。新增配置 `FAQ_REFRESH_INTERVAL_S` 默认 3 秒，`FAQ_REFRESH_WARNING_INTERVAL_S` 默认 60 秒，其他配置默认值保持原样。多轮请求不使用答案缓存，仍可感知版本变化。

发现版本不同后，把重建交给独立的单线程有界后台池，复用现有 `BoundedExecutor` 实现，不占请求 `model_io` 池的名额；单请求 worker 在构建期间仍可查询旧快照。每个进程最多一个构建任务，名额不可用时下次请求重试。关闭服务时沿用执行器的等待/超时关闭约定。独立使用 `FaqService` 时只在发现变化后惰性创建一个单线程执行器，调用方负责 `close()`。

数据库读取和 BM25 构建不持有共享模型锁；后台按单条问句编码并复用现有模型锁，完整构建、校验成功后一次替换快照。查询捕获快照引用，整个查询都使用同一份索引和向量，不等待重建锁，也不会读到半成品。FAQ 查询始终用 `acquire(blocking=False)` 尝试共享模型锁，避免读取刷新状态后才启动重建的竞态：优先使用答案缓存、进程内最近查询向量（最多 512 条）或旧快照中精确规范化问句的向量；全新语义问句若暂时不能编码，则按 FAQ 未命中进入现有后续路由。模型锁竞争时采用相同的 FAQ 降级语义，不因刷新标志的变化转为阻塞等待；后续检索仍遵守其原有线程池和模型锁约定。

答案缓存键包含完整快照内容的 SHA-256 指纹，仍位于 `faq:cache:*` 前缀下。新快照发布后只会读取对应指纹的缓存；旧 worker 或旧请求即使在清理之后迟到写入，也只能写入旧命名空间，无法污染新快照。已经开始的旧请求可能完成并返回旧答案；尚未重建成功的进程仍服务旧快照，所以发布确认必须覆盖所有进程。重建成功后也会按现有前缀清理 FAQ 缓存；物理清理失败时指纹隔离仍防止新快照命中旧答案，残留键按原 TTL 到期。

## 故障与降级

- Redis 版本读取失败或值非法：保留当前快照，不向请求抛异常，暂时跳过 Redis 答案缓存；按检查间隔恢复探测，默认每 60 秒最多记录一次脱敏版本告警。不会打印异常原文、连接地址、SQL 或 FAQ 正文。
- MySQL、模型或向量校验导致后台重建失败：不替换旧快照、不推进本进程版本，旧快照继续服务；按刷新间隔允许下一次 FAQ 查询重试，重建告警也限频。重建中收到更高版本时不会丢失，下次版本检查继续追赶。
- 数据库已提交而 Redis 发布/清理失败：导入/恢复退出码为 `2`，提示数据库已经提交。此时不得假定各进程已生效；修复 Redis 后补发通知即可，不必重复写库。退出码 `0` 表示操作完成、无变化未发布或 dry-run 完成；校验、确认或数据库操作失败为 `1`。
- Redis 故障期间新启动进程仍从 MySQL 加载；已运行进程保留旧快照。需要全实例立即一致的切换时，应在总控层协调流量和实例确认，本机制提供可降级的最终一致刷新。

手动通知只递增版本号，不写 MySQL、不直接清缓存；各进程重建成功后清理 FAQ 缓存：

```bash
python -m ingest.faq_reload
# 如只需物理清理 FAQ 缓存，原命令仍可用：
python -m ingest.clear_cache
```

## 离线验证与总控验收

```bash
cd backend
python -m pytest tests/test_faq_refresh.py tests/test_faq_background.py tests/test_faq_admin.py \
  tests/test_faq_service.py tests/test_ingest_faq.py tests/test_clear_cache.py \
  tests/test_services.py tests/test_executor.py tests/test_multiturn.py
```

替身测试覆盖 SCAN match 前缀断言及过宽模式的对照、多进程版本变化、启动版本对齐与初始化期间发布、TTL 检查、构建失败重试、读取/编码期间并发查询和锁竞态、旧请求迟到缓存写入、Redis 故障降级、FAQ 前缀隔离、单 worker 请求与后台构建隔离、dry-run 无写入、无变化不发布与仅 prune 删除仍发布、备份恢复往返、prune 确认、严格表头/分类校验及通知失败退出码。测试使用临时 CSV、内存数据库/Redis 和假编码器，不访问真实服务；刷新提交使用真实 BoundedExecutor.try_submit 或 ThreadPoolExecutor，检查 Future 完成、取消前未执行任务的状态复位及重试，不再用返回 True 的 submit 替身冒充 Future。

### 需在真实环境验收

- 真实 Redis SCAN 的 `match="faq:cache:*"`：确认版本键与 `chat:rate:*` 保留，并检查多游标分页、扫描中并发增删与 INCR；fnmatch 替身不能替代 Redis 的实际 glob/游标行为。
- 真实 MySQL `BINARY`/`GET_LOCK`：确认排序规则下的精确更新/删除、跨连接命名锁互斥、事务回滚与备份恢复；内存 SQL 替身不能验证数据库锁和事务隔离。
- executor 取消：在项目 Python 3.11 和应用生命周期中实测请求超时/取消、后台任务取消、关闭时尚未结束的模型编码及线程池容量释放。离线真实 Future 测试已覆盖任务开始前取消后 `_refreshing` 复位，尚不能替代完整服务的异步取消和关闭验收。
- 真实 bge-m3 编码耗时与多实例流量下的版本收敛、失败重试及降级路径。

本任务工作区仅有 Python 3.9，缺少 pytest、pydantic 和 pydantic-settings，未运行完整 pytest 套件及服务装配测试。实际通过标准库 unittest 执行了 25 项导入/备份恢复/缓存/版本测试；另以简化 DTO、AppError 和 trace 替身执行了 13 项刷新逻辑 smoke，其中使用真实 BoundedExecutor、ThreadPoolExecutor 和 Future。后者不代表真实 Pydantic 契约、Python 3.11 异步路径或服务集成已通过；仍需总控验证。
