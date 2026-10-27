# 商品关系图数据

为知识图谱（Neo4j）演示准备的结构化数据，由 `data/products/products.csv` 与 `data/docs` 场景文档整理得出，不新增虚构商品。

**口径：所有兼容结论都是「规格层面匹配」。** 它只说明参数满足规则，不代表实际一定能用；能否使用仍取决于供电端、线材和受电设备的协商，与 `product_guide_charging_devices.md` 的立场一致。回答时必须带上这层限定，不得说成保证可用。

本目录的全部文件由 `backend/ingest/graph_data.py` 生成，不要手工修改；改规则或人工整理的规格后重新生成。`tests/test_graph.py` 会检查已提交文件与生成结果一致。

## 使用流程

在 `backend/` 执行。这些 CLI 都不读 `.env`，连接参数只取进程环境：`NEO4J_URI`（默认 `bolt://127.0.0.1:7687`）、`NEO4J_USER`（默认 `neo4j`）、`NEO4J_PASSWORD`（必填）、`NEO4J_DATABASE`（默认 `neo4j`）。

```bash
uv run python -m ingest.graph_data            # 预览生成结果与差异
uv run python -m ingest.graph_data --yes      # 写入 data/graph
uv run python -m ingest.graph_data --check    # 与已提交文件不一致时返回 1
```

启动 Neo4j：先在根目录 `.env` 设置 `NEO4J_PASSWORD`（至少 8 位），再在仓库根目录执行 `docker compose --profile graph up -d neo4j`。neo4j 服务放在 `graph` profile 里，平时的 `docker compose up -d` 不会启动它。浏览器访问 <http://localhost:7474> 可以打开 Neo4j Browser。

```bash
export NEO4J_PASSWORD=与 .env 中相同的密码
uv run python -m ingest.graph_load            # 预览节点与关系数
uv run python -m ingest.graph_load --yes      # 清空 :Ecom 子图并在一个事务内重建
uv run python -m eval.graph_demo "衡电 A3 配联枢 C1 线，65 瓦电脑够吗"
uv run python -m eval.graph_demo --plan-only "栖光 L4 能用哪款充电器"   # 只看实体链接和 Cypher，不连库
uv run python -m eval.graph_demo --eval       # 对 eval_graph.jsonl 统计 expected_skus 召回
uv run python -m eval.graph_demo --cypher "MATCH (n:Ecom:Product) RETURN n.name LIMIT 5"
```

演示 CLI 不调用模型，也不接入在线问答链路：先按商品全名、「系列 型号」、SKU 做规则实体链接（单独的「L1」有歧义，不链接），再按关键词选意图和 Cypher 模板，以读访问模式查询。`--eval` 只把查询结果里出现的 id 计入召回，问句中链接到的实体不算。

## 图模型

所有节点带 `:Ecom` 标签和唯一的 `id` 属性（约束 `ecom_id`），导入只清空 `:Ecom` 子图。

| 节点 | id 示例 | 说明 |
| --- | --- | --- |
| `Product` | `qiguang-l1` | 商品；充电设备带 `c_out_ports`、`two_c_devices` 等多设备属性 |
| `Series` / `Category` | `series:栖光` / `category:桌面台灯` | 系列与品类 |
| `Port` / `Interface` | `hengdian-p2#c_io` | 供电端口与信号接口，`power_w` 等为数值 |
| `Item` | `item:充电适配器` | 包装物品与收纳类别 |
| `External` | `ext-laptop-65w` | 店内不售的外部设备 |
| `Scene` / `Doc` | `scene:commuting` / `product_scene_commuting.md` | 场景与 RAG 语料文档 |

关系：`IN_SERIES`、`IN_CATEGORY`、`RELATED_TO`、`HAS_PORT`、`HAS_INTERFACE`、`INCLUDES` / `NOT_INCLUDES`、`CAN_HOLD`、`FEATURES`（带 `origin`）、`DESCRIBED_IN`、`DESCRIBES`（`Doc → Product`），以及 `derived_edges.csv` 的推导关系（带 `level`、`rule`、`detail`、`cable`）。`Doc` 节点的 id 就是 `data/docs` 文件名，之后接入 RAG 时可以由此回溯原文块。

## 文件

| 文件 | 行数 | 内容 | 来源 |
| --- | --- | --- | --- |
| `power_specs.csv` | 48 | 端口级供电规格：角色、接口、方向、口数、电压、电流、功率、PD、合计上限 | 人工整理；`spec_field`/`spec_value` 保留原文字段供核对 |
| `interfaces.csv` | 24 | 数据、视频、音频、无线接口 | 人工整理，保留原文字段 |
| `package_items.csv` | 256 | 包装内含与不含物品，按类型归类（cable/adapter/audio_cable/receiver/battery 等） | 自动拆分 `package_contents`、`not_included` |
| `external_devices.csv` | 12 | 店内不售的外部设备，如 45/65/100 瓦电脑、HDMI 显示器、音频线、C 转 A 线 | 构造，用于演示约束推理 |
| `storage_fit.csv` | 29 | 收纳包与可收纳物品；等级为 类别匹配 / 待核对 / 尺寸条件 | 依据「适用物品」原文 |
| `scenes.csv` | 72 | 场景与商品；`origin=doc` 来自 10 篇场景文档，`origin=constructed` 为扩展构造 | 文档 + 构造 |
| `derived_edges.csv` | 209 | 按下列规则推导的关系边，含规则编号、等级和说明 | 规则计算 |
| `multi_device.csv` | 6 | 每个供电商品的 C 口数与是否能同时给 2 台、3 台 C 口设备供电 | 规则计算 |
| `eval_graph.jsonl` | 94 | 多跳评测题，含参考答案 | 由上述数据计算 |

`origin=constructed` 的场景行只用于图谱演示，不能当作店铺的推荐结论；评测题只使用 `origin=doc` 的场景。

## 角色

- `sink`：受电商品（耳机、C 口台灯、音箱、C 口加湿器、键行 K2/M2）
- `source` / `source_sink`：衡电充电器 / 充电宝（充电宝另有 `recharge_in` 补电输入）
- `host_powered`：有线连接主机供电、无电池（键行 K1、K3、M3）
- `battery`：干电池供电（键行 M1）
- `cable` / `hub`：联枢线材、扩展器、音频转接器

## 推导规则

| 编号 | 关系 | 条件 | 等级 |
| --- | --- | --- | --- |
| R1 | `CAN_POWER` | 接口均为 C 型，且供电端 C 口单口最高功率 ≥ 受电端额定输入功率（电压 × 电流） | 规格层面匹配 |
| R1b | `CAN_POWER` | 同上，但单口功率低于受电端的输入上限（仅充电宝补电） | 规格层面部分匹配 |
| R2 | `CAN_POWER` | 接口均为 C 型，但受电端未注明电压电流（听岚全系耳机） | 接口层面匹配 |
| R3 | `NOT_COMPATIBLE` | 受电端为 12 伏圆孔直流接口，店内供电商品只有 C/A 口 | 接口不一致 |
| R4 | `NOT_COMPATIBLE` | 供电端单口功率低于外部电脑需求 | 功率不足 |
| R5 | `CABLE_SUPPORTS` / `CABLE_INSUFFICIENT` | 线材最高功率与外部电脑需求比较 | — |
| R6 | `PASSTHROUGH` | 联枢 H1 向主机供电 85 瓦与电脑需求比较；供电端和线材也需满足 | — |
| R7 | `CONNECTS_TO` / `NOT_COMPATIBLE` | 数据、视频、音频接口一致 | — |
| R8 | `POWERS` | AA 电池供电 | — |
| R9 | 多设备 | C 型输出口数 ≥ 设备数；合计上限见 `multi_device.csv` | — |
| R10 | 收纳 | 「适用物品」原文类别 | — |
| R11 | 场景 | 场景文档列出的商品 | — |
| R12 | 包装 | 包装是否含适配器、充电线 | — |

规则约定：

- 只比较 C 口。店内没有 C 转 A 线，A 型口的单口上限也未单列，所以 A 口不参与供电推导。
- 功率比较用的是供电端「单口最高」，没有按 5 伏档逐一核对电流；只有衡电 A1 明示 5 伏 3 安。
- 商品自带的「充电线」「供电线」都没写另一端接口，统一记为「未注明」，答案里照实说明。
- 外部设备节点以 `ext-` 开头，不是店内商品。

## 评测集

`eval_graph.jsonl` 的格式兼容 `eval.run_eval`（`question`、`expected_route`、`expected_doc`，多轮题带 `history`），并增加以下字段：

- `kind`：题型，包括 power_pair、power_list、no_path、laptop_combo、hub_chain、multi_device、recharge、signal、storage、scene、scene_cross、aggregate
- `hops`：推理跳数（2–4）
- `expected_skus`：答案应涉及的商品或外部设备
- `graph_rules`：依赖的规则编号
- `reference_answer`：参考答案；含兼容判断的题目末尾带规格层面限定

题目中正例和反例都有，例如 C1 线 60 瓦不够 65 瓦电脑、L4 圆孔接口无店内供电商品、A1 单口无法同时供两台设备，用于对比开启图谱前后的效果。现有 RAG 语料没有跨商品兼容的原文，不接图谱时，这类题预计会拒答或答不全。

```bash
cd backend
uv run python -m eval.run_eval --eval-file data/graph/eval_graph.jsonl --output logs/eval-graph.json
```
