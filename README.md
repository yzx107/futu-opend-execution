# futu-opend-execution

面向港股富途 OpenD 的执行层原型，默认以模拟、dry-run 和可回放验证为先。

## 项目目标

这个仓库用于把 Futu/OpenD 的下单、风控、行情触发和日志记录逻辑从研究代码里独立出来。

当前目标是构建一套小而可测试的执行层：

- 连接本机 Futu OpenD 网关
- 默认不做真实下单
- 真实交易必须经过显式开关和多重风控
- 支持后续扩展到港股新股暗盘、首日交易等场景

## 范围

## 项目开发进度（截至 2026-04-25）

### 已完成（MVP）

- [x] 执行层核心数据模型与风控校验
- [x] 暗盘计划器、模拟成交与下单执行闭环
- [x] 暗盘开盘触发器（限频、冷却、kill-switch）
- [x] Normal Trade 下单路径与 Web 控制台
- [x] JSONL 全链路日志与 replay 回放能力
- [x] 单元测试覆盖主要交易流程（31 项）

### 进行中

- [ ] OpenD 连接健康检查与自动恢复策略
- [ ] 实盘前检查项（账户状态/交易上下文）进一步细化
- [x] CI 中增加多 Python 版本矩阵与兼容性验证（3.10/3.11/3.12）

### 下阶段里程碑

1. 增加连接稳定性策略：retry、timeout、reconnect。
2. 补齐 paper-trading 工作流与示例脚本。
3. 增强 Web 控制台的订单状态可观测性（更细粒度状态与错误提示）。

当前已经包含：

- Python 包结构
- 基于环境变量的运行配置
- 暗盘买入计划器
- 基于可见卖盘的限价买入模拟
- 经纪商提交、轮询、超时撤单流程
- `dark_status` 开盘触发器
- JSONL 事件日志
- replay 回放模式
- 单元测试
- 安全运行文档

计划继续完善：

- OpenD 连接健康检查
- retry / timeout / reconnect 策略
- 更完整的账户与交易上下文探针
- paper-trading 工作流

当前阶段不做：

- 默认无人值守实盘交易
- 策略信号生成
- 组合优化
- 把账号、密码等敏感信息提交到 git

## 安全模型

本项目刻意采用 simulation-first / dry-run-first 设计。

核心开关：

- `FUTU_ALLOW_REAL_TRADE=0`：真实交易必须关闭
- `FUTU_ALLOW_REAL_TRADE=1`：只表示环境允许真实交易，CLI 仍需显式 `--real`
- `FUTU_TRADE_PASSWORD`：真实账户解锁交易时才需要

设计原则：

- 默认 fail closed
- 下单前先做输入校验和风控校验
- 每个触发、下单请求、券商响应、错误都写 JSONL
- 策略逻辑和执行逻辑分离
- `.env` 被 `.gitignore` 忽略，敏感信息不进入仓库

## 目录结构

```text
.
├── .env.example
├── .github/
│   └── workflows/
│       └── ci.yml
├── LICENSE
├── README.md
├── setup.py
├── tests/
│   ├── test_grey_open.py
│   ├── test_greymarket.py
│   ├── test_orders.py
│   └── test_snatch.py
└── src/
    └── futu_opend_execution/
        ├── __init__.py
        ├── config.py
        ├── grey_open.py
        ├── models.py
        ├── risk.py
        ├── execution/
        │   ├── broker.py
        │   ├── futu.py
        │   ├── futu_quote.py
        │   ├── futu_runtime.py
        │   ├── market_data.py
        │   └── simulator.py
        └── services/
            ├── greymarket.py
            ├── orders.py
            └── snatch.py
```

## 暗盘买入计划器

`build_grey_market_buy_plan(...)` 用于回答一个问题：

```text
给定当前可见卖盘，想买入目标数量，最低需要挂到什么限价？
```

当前行为：

- 读取开盘后的可见 ask book 快照
- 从低到高遍历卖盘档位
- 找到覆盖目标数量所需的边际卖价
- 可选增加若干 tick buffer
- 在同一快照上模拟预期成交
- 如果可见流动性不足或超过价格上限，则拒绝生成计划

示例：

```python
from futu_opend_execution import (
    GreyMarketBuyRequest,
    OrderBookSnapshot,
    QuoteLevel,
    build_grey_market_buy_plan,
)

snapshot = OrderBookSnapshot(
    symbol="09868",
    asks=(
        QuoteLevel(price="3.28", quantity=100),
        QuoteLevel(price="3.29", quantity=200),
        QuoteLevel(price="3.30", quantity=500),
    ),
)

request = GreyMarketBuyRequest(
    symbol="09868",
    quantity=250,
    tick_size="0.01",
    price_buffer_ticks=0,
)

plan = build_grey_market_buy_plan(request, snapshot)
print(plan.minimum_limit_price)         # 3.29
print(plan.selected_limit_price)        # 3.29
print(plan.expected_fill.average_price) # 3.286
```

注意：计划器只基于快照计算，不保证真实成交。真实环境还会受延迟、队列位置、盘口撤单、券商内部撮合等影响。

## 经纪商提交流程

仓库提供了面向 OpenD 的交易适配器和提交服务：

- `submit_grey_market_buy_plan(...)`：提交限价买入计划，轮询订单状态，返回执行报告
- `execute_grey_market_buy(...)`：计划生成和提交合并为一步
- `run_grey_market_snatch(...)`：等待可交易状态、拉取盘口、生成计划、可选提交
- `FutuOpenDTradeBroker`：映射到 OpenD 的下单、查单、历史查单、撤单、解锁
- `FutuOpenDQuoteClient`：映射到 OpenD 的市场状态和盘口读取

当前 Futu 股票交易路径没有使用原生 `IOC`。项目用 `DAY` 单加快速撤单来模拟 IOC 行为。

安装 OpenD SDK 依赖：

```bash
pip install -e '.[futu]'
```

提交示例：

```python
from futu_opend_execution import (
    FutuOpenDTradeBroker,
    GreyMarketBuyRequest,
    OrderBookSnapshot,
    QuoteLevel,
    RuntimeConfig,
    execute_grey_market_buy,
)

config = RuntimeConfig.from_env()
request = GreyMarketBuyRequest(
    symbol="09868",
    quantity=250,
    ioc_timeout_seconds="0.8",
    remark="grey-open-snatch",
)
snapshot = OrderBookSnapshot(
    symbol="09868",
    asks=(
        QuoteLevel(price="3.28", quantity=100),
        QuoteLevel(price="3.29", quantity=200),
    ),
)

with FutuOpenDTradeBroker(config) as broker:
    report = execute_grey_market_buy(request, snapshot, broker, config=config)

print(report.latest_order.status)
print(report.latest_order.dealt_quantity)
print(report.remaining_quantity)
```

CLI：

```bash
PYTHONPATH=src python -m futu_opend_execution.harness 09868 250
PYTHONPATH=src python -m futu_opend_execution.harness 09868 250 --execute --remark grey-open-snatch
```

分批建仓（harness engineering 风格）：

```bash
PYTHONPATH=src python -m futu_opend_execution.harness 09868 1000 \
  --execute \
  --tranche-weights 0.5,0.3,0.2 \
  --tranche-buffer-ticks 0,1,2 \
  --allow-partial-fill-final-tranche \
  --remark grey-open-ladder
```

说明：

- `--tranche-weights` 按权重拆分总数量并顺序执行多笔抢单；
- `--tranche-buffer-ticks` 可为后续 tranche 设置更激进的价格缓冲；
- `--allow-partial-fill-final-tranche` 只对最后一笔开放 partial fill，兼顾建仓完成度和成本控制。

## 暗盘开盘触发器

`grey_open` 是更贴近实盘开盘场景的安全触发器。它会订阅或读取：

- `QUOTE`
- `ORDER_BOOK`
- `TICKER`
- `dark_status`
- best bid / best ask
- 盘口服务器时间戳

只有同时满足以下条件，才会生成订单意图：

- `dark_status == TRADING`
- `best_ask > 0`
- `best_ask <= max_price`
- `quantity <= max_qty`
- `max_price * quantity <= max_notional`
- 未超过 `max_order_attempts`
- 冷却时间和 30 秒窗口限频未触发
- kill-switch 文件不存在

为了低于 OpenD 文档限频，触发器最多允许 30 秒内 14 次订单尝试，并且强制连续两次尝试至少间隔 50ms。

针对“暗盘第一秒交易量较大”的场景，支持开盘突发窗口参数：

- `--opening-burst-seconds`：首次观察到 `dark_status=TRADING` 后，持续多少秒使用突发模式（默认 0.0 秒，即关闭）
- `--opening-burst-cool-down-ms`：突发窗口内的下单冷却（默认 50ms）

行情触发机制默认改为 push-first：优先消费 OpenD 推送行情；若短时间内没有收到推送，再自动回退到轮询读取，兼顾速度与稳健性。

### Live Dry-run

dry-run 是默认模式。它会连接 OpenD，准备行情和交易上下文，记录事件并打印 `would_place_order`，但不会解锁交易，也不会调用 `place_order`。

```bash
PYTHONPATH=src python -m futu_opend_execution.grey_open live HK.01234 \
  --quantity 1000 \
  --max-price 12.80 \
  --max-qty 1000 \
  --max-notional 12800 \
  --max-order-attempts 3 \
  --cool-down-ms 300 \
  --opening-burst-seconds 1.0 \
  --opening-burst-cool-down-ms 50 \
  --kill-switch-file /tmp/futu-grey-open.STOP \
  --log-file logs/grey_open_01234.jsonl
```

如果 Futu SDK 需要写日志目录，建议设置：

```bash
FUTU_SDK_HOME_OVERRIDE=/tmp/futu-sdk-home
```

### Replay / Simulate

replay 模式读取历史 JSONL 行情事件，用同一套触发逻辑回放，不连接 OpenD。推荐在任何真实交易前先跑 replay。

支持扁平格式：

```json
{"symbol":"HK.01234","dark_status":"TRADING","best_bid":"12.60","best_ask":"12.70"}
```

也支持 OpenD 形状：

```json
{"symbol":"HK.01234","raw_quote":{"dark_status":"TRADING"},"raw_order_book":{"Ask":[["12.70",1000,1]],"Bid":[["12.60",500,1]],"svr_recv_time_ask":"2026-04-24 16:15:00.001"}}
```

运行：

```bash
PYTHONPATH=src python -m futu_opend_execution.grey_open replay logs/grey_open_01234.jsonl HK.01234 \
  --quantity 1000 \
  --max-price 12.80 \
  --max-qty 1000 \
  --max-notional 12800 \
  --log-file logs/replay_01234.jsonl
```

### Real-run

真实交易需要两个开关同时打开：

- 环境开关：`FUTU_ALLOW_REAL_TRADE=1`
- CLI 开关：`--real`

真实模式会先解锁交易，安装尽力而为的订单/成交推送 handler，然后用以下参数提交港股普通限价买单：

- `OrderType.NORMAL`
- `TrdSide.BUY`
- `TrdEnv.REAL`
- `TimeInForce.DAY`

示例：

```bash
FUTU_ALLOW_REAL_TRADE=1 FUTU_TRADE_PASSWORD='...' \
PYTHONPATH=src python -m futu_opend_execution.grey_open live HK.01234 \
  --real \
  --quantity 1000 \
  --max-price 12.80 \
  --max-qty 1000 \
  --max-notional 12800 \
  --max-order-attempts 3 \
  --cool-down-ms 500 \
  --kill-switch-file /tmp/futu-grey-open.STOP \
  --log-file logs/real_grey_open_01234.jsonl
```

在另一个终端创建 kill-switch 文件即可阻止新的订单生成：

```bash
touch /tmp/futu-grey-open.STOP
```

每次运行会写入 JSONL，包括：

- `quote_event`
- `orderbook_event`
- `trigger_event`
- `order_request`
- `order_response`
- `order_push`
- `fill_event`
- `error_event`

## Web UI 控制台

CLI 对非工程师不够友好，所以仓库提供了本地 Web UI。它默认只监听 `127.0.0.1`，第一屏就是交易控制台：

- 顶部：OpenD 状态、最近报价、事件数量
- 左侧：正常交易
- 右侧：暗盘抢单 dry-run 评估
- 底部：JSONL 事件日志

启动：

```bash
PYTHONPATH=src python -m futu_opend_execution.web_app --port 8765
```

打开：

```text
http://127.0.0.1:8765
```

Web UI 当前支持：

- 正常交易报价刷新
- 自动读取 `lot_size`
- 正常交易 `BUY` / `SELL`
- `NORMAL` 限价单
- `MARKET` 市价单
- 手数 / 股数
- `max_notional` 后端风控
- dry-run / real 双模式
- 实盘确认短语：`确认实盘`
- 下单后轮询订单终态
- 暗盘抢单 dry-run 评估
- 全局 kill switch
- 日志 tail
- `/api/health?active=1&symbol=00700` 主动探测 OpenD 报价链路

健康检查接口示例：

```bash
curl "http://127.0.0.1:8765/api/health"
curl "http://127.0.0.1:8765/api/health?active=1&symbol=00700"
```

Web UI 的安全边界：

- 页面默认 dry-run
- 实盘必须同时满足 `.env` 中 `FUTU_ALLOW_REAL_TRADE=1` 和页面切换到实盘
- 实盘提交前必须输入 `确认实盘`
- 后端会重新校验价格、数量、订单类型、`max_notional` 和 kill switch
- 同一真实订单摘要 3 秒内重复点击会被后端拦截
- 暗盘抢单 Web UI 当前只开放 dry-run 评估，实盘抢单先不在页面里自动发单

## 正常交易下单

`normal_trade` 是普通港股交易时段的 CLI。它会先读取：

- 股票基础信息里的 `lot_size`
- 实时报价
- 一档买卖盘

然后可以按手数或股数生成订单。默认仍然是 dry-run，不会真实下单。

dry-run 示例：

```bash
PYTHONPATH=src python -m futu_opend_execution.normal_trade HK.00700 \
  --side BUY \
  --order-type NORMAL \
  --quantity-mode LOTS \
  --lots 1 \
  --limit-price 495 \
  --max-notional 50000 \
  --log-file logs/normal_trade_00700.jsonl
```

输出会包含类似：

```text
would_place_order code=HK.00700 side=BUY qty=100 lot_size=100 ...
```

真实提交需要 `.env` 中已配置：

```env
FUTU_ALLOW_REAL_TRADE=1
FUTU_ACC_ID=你的真实账户ID
FUTU_TRADE_PASSWORD=你的交易密码
```

并显式传入 `--real`：

```bash
PYTHONPATH=src python -m futu_opend_execution.normal_trade HK.00700 \
  --real \
  --side BUY \
  --order-type NORMAL \
  --quantity-mode LOTS \
  --lots 1 \
  --limit-price 495 \
  --max-notional 50000 \
  --remark normal_one_lot_test \
  --log-file logs/real_normal_trade_00700.jsonl
```

市价单示例：

```bash
PYTHONPATH=src python -m futu_opend_execution.normal_trade HK.00068 \
  --real \
  --side BUY \
  --order-type MARKET \
  --quantity-mode LOTS \
  --lots 1 \
  --max-notional 20000 \
  --remark normal_market_one_lot
```

注意：`--real` 会提交真实订单。市价单没有成交价上限，后端只用当前盘口估算 `max_notional` 风险金额。

## 快速开始

### 1. 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -e .
```

如需连接 OpenD：

```bash
pip install -e '.[futu]'
```

### 2. 配置环境

```bash
cp .env.example .env
chmod 600 .env
```

`.env` 已被 `.gitignore` 忽略。不要把交易密码提交进 git。

默认变量：

```env
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
FUTU_ALLOW_REAL_TRADE=0
FUTU_SECURITY_FIRM=FUTUSECURITIES
FUTU_ACC_ID=0
FUTU_ACC_INDEX=0
FUTU_TRADE_PASSWORD=
FUTU_SDK_HOME_OVERRIDE=
FUTU_ORDER_POLL_INTERVAL_SECONDS=0.2
FUTU_CANCEL_ORDER_GRACE_SECONDS=2.0
FUTU_DEFAULT_IOC_TIMEOUT_SECONDS=1.0
FUTU_QUOTE_POLL_INTERVAL_SECONDS=0.5
FUTU_DEFAULT_WAIT_FOR_OPEN_TIMEOUT_SECONDS=300.0
FUTU_DEFAULT_ORDER_BOOK_DEPTH=10
FUTU_GREY_MARKET_OPEN_STATES=AUCTION,MORNING,AFTERNOON,AFTER_HOURS_BEGIN,HK_CAS,NIGHT_OPEN
```

### 3. 加载本地 `.env`

```bash
set -a
source .env
set +a
```

### 4. 运行本地检查

```bash
python -c "import futu_opend_execution; print('ok')"
python -m unittest discover -s tests
```

## 实盘验证建议流程

目标是验证 API 能否真实接受暗盘或港股订单，不是抢成交。

建议顺序：

1. 确认 OpenD 正在监听 `127.0.0.1:11111`
2. 查询账户列表，确认真实账户 `acc_id`
3. 设置 `.env`，包括 `FUTU_ACC_ID` 和 `FUTU_TRADE_PASSWORD`
4. 调用 `unlock_trade`
5. 读取目标标的盘口
6. 提交一笔远离盘口、极小数量、预期不成交的限价探针单
7. 立刻撤单
8. 查询订单最终状态和错误码
9. 把所有结果写入 JSONL

只有探针确认 `accepted -> cancel accepted -> terminal status` 后，才进入暗盘开盘 dry-run 和 real-run。

## 开发说明

- 需要 Python 3.11+
- 连接 OpenD 时需要本机已登录 Futu OpenD
- `futu-api` 是可选依赖，不安装也能跑计划器和测试
- Futu SDK 默认会写 HOME 下的日志；沙箱或权限受限时设置 `FUTU_SDK_HOME_OVERRIDE`
- `grey_open` 默认 dry-run，真实交易前必须 replay 测试
- 真实交易必须显式设置 `FUTU_ALLOW_REAL_TRADE=1`
- 当前 `IOC` 使用 `DAY` 单加快速撤单模拟

## Roadmap

### Phase 1: 项目骨架

- [x] 创建 package skeleton
- [x] 发布仓库
- [x] 添加 CI 和基础文档

### Phase 2: 模拟执行 MVP

- [x] 配置加载器
- [ ] 连接健康检查
- [x] 执行请求模型
- [x] 基于可见盘口的暗盘买入模拟
- [x] 结构化日志
- [x] OpenD 适配器形状

### Phase 3: Broker 集成

- [x] OpenD 交易上下文 wrapper
- [x] 下单抽象
- [x] 订单状态协调
- [ ] retry / timeout / reconnect policy
- [x] 显式实盘风控

## License

MIT
