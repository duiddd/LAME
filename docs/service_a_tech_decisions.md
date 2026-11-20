# Service A (Trading & Accounts) 技术决策记录

负责人:Pin-Yun Kuo · 状态:进行中(按决策逐项推进)

本文记录 A 已经确定的决策、其理由和后果。设计见 [service_a_architecture.md](service_a_architecture.md)。A 如何配合其他服务目前的实现,列在末尾「A 如何配合其他服务」。

## 1. 输入输出契约

### 1.1 用户侧

| 请求 | 字段 | 说明 |
| --- | --- | --- |
| 注册 `POST /api/auth/register` | `username`、`password` | 不收集邮箱或任何真实个人信息 |
| 登录 `POST /api/auth/login` | `username`、`password` | 设置 HttpOnly cookie(短效 access token 与 refresh token),浏览器之后自动带上(D20) |
| 换发 `POST /api/auth/refresh` | refresh cookie | 轮换 refresh token 并发放新的 access token |
| 模拟充值 `POST /api/accounts/deposits` | `asset`、`amount`;Header `Idempotency-Key` | 立即入账 |
| 下单 `POST /api/orders` | `symbol`、`side`(BUY / SELL)、`type`(LIMIT / MARKET)、`price`(仅限价单)、`qty`;Header `Idempotency-Key`(前端生成的 `client_order_id`) | 金额为十进制字符串,进入 A 后转为整数(D8) |
| 改单 `PUT /api/orders/{order_id}` | 新的剩余 `qty`(可增可减);不能改价格 | 沿用同一个 `order_id`(D24) |
| 撤单 `DELETE /api/orders/{order_id}` | — | 经 D 发出 |
| 查询 | 订单、成交、资产、K 线、行情摘要 | 只能查自己的订单和资产 |

拒绝时带 `reason`:`RATE_LIMITED`、`INVALID_TICK`、`INVALID_LOT`、`QTY_TOO_SMALL`、`QTY_TOO_LARGE`、`NOTIONAL_TOO_LARGE`、`PRICE_OUT_OF_BAND`、`NO_FRESH_PRICE`、`NO_LIQUIDITY`、`INSUFFICIENT_BALANCE`、`MODIFY_UNAVAILABLE`、`SYSTEM_BUSY`、`UNAUTHORIZED`、`USERNAME_TAKEN`。下单成功送出时回 `202`;只有确定没有送到 D 时才回 `503`(D12)。

### 1.2 与其他服务

| 链路 | 方向 | 方式 | 内容 |
| --- | --- | --- | --- |
| 命令 | A → D | HTTP + JSON:`POST /v1/orders`、`/v1/cancels`、`/v1/modifies` | 下单、撤单、改单;D 的同步回复**只确认收到** |
| 状态查询 | A → D | HTTP + JSON:`GET /v1/orders/{order_id}?user_id=` | 只用于对账(D5) |
| 补发 | A → D | HTTP + JSON:`POST /v1/notifications/rewind` | 请 D 从某个 `notify_seq` 重发 |
| 通知 | D → A | HTTP + JSON,A 提供 `POST /internal/order-events` | 风控拒绝、B 接受(带 B 的 `day`、`order_seq`)、成交、撤单、拒绝、过期、日终、改单结果 |
| 行情 | B → 浏览器、B → A | WebSocket `ws://<B>:9999/md/<symbol>` | `bids`、`asks`(每边 0–3 档)、`last_price`(可为 null)、`volume`、`last_ms`;不含 `symbol` |
| 关联账户 | 共享文件 | `linked_accounts.json` | A 与 D 都读取(D16) |
| 新闻 | 浏览器 → C | HTTP `GET /api/news?symbol=&limit=`,不带 cookie | 由 A 绘制交易页的新闻面板,每 15 秒刷新 |
| 聊天框 | 浏览器 → C | `<iframe>` 嵌入 C 的 `/widget` | UI 归 C,A 不传参数(D15) |

## 2. 已定决策

### D1. 语言与框架:Python 3.12 + FastAPI

- **决策:** A 使用 Python 3.12、FastAPI、Pydantic v2。
- **理由:**
  1. 与 C、D 使用同一套技术,compose、Dockerfile、监控接入方式可以共享。
  2. FastAPI 原生 async,适合 A「大部分时间在等数据库和下游」的 I/O 密集型负载。
  3. Pydantic 在 API 边界完成校验,错误信息自动带字段名。
- **备选:** Java + Spring Boot(负责人更熟悉,但内存占用大,与组内其他服务不一致);Go。
- **后果:** Python 单进程吞吐低于 JVM 或 Go。A 的瓶颈在数据库事务和下游等待,预计可接受;Week 6 基线测量后确认。

### D2. 单进程、单 worker

- **决策:** uvicorn 只开 **1 个 worker**。限流令牌桶、熔断器状态、负载削减的延迟窗口、对账任务都放在进程内存中。
- **理由:** 多 worker 等于多个进程,各有一份内存状态:限流会按 worker 数放大,对账任务会重复执行。单进程最简单,与 B、D 的单实例设计一致。
- **备选:** 多 worker + Redis 集中保存状态。
- **后果:** 只用到一个 CPU 核心。**改变决策的事实:** 若 Week 6 基线显示单进程达不到目标下单速率,改为多 worker + Redis。

### D3. A 与 D 的交互:同步只确认收到,结果全部由 D 主动通知

- **决策:**
  - A 向 D 下单、改单、撤单时,D 的同步回复**只代表收到**,不代表已完成风控或已送到 B。A 收到确认后,订单状态只改为 `SENT`,**不释放冻结资金**。
  - 风控结果、B 的结果、成交、撤单结果全部由 D 主动调用 A 的 `POST /internal/order-events` 通知。
  - 行情由浏览器和 A 直接订阅 B 的 WebSocket;其余与 B 的往来都经过 D。
- **理由:**
  1. A 的下单请求很快返回,不被风控和撮合的耗时拖住,用户立即看到「已送出」。
  2. B 只有 D 一个命令入口,所有订单结果从 D 一个来源进入 A,顺序和去重规则只有一套。
- **备选:** D 的同步回复直接带回风控结果和 B 的第一个结果(D 现有协议 2.1)。A 少处理一种中间状态,但下单请求要等风控和撮合完成。
- **后果:**
  - D 宕机时,新单无法下,**已有订单的结算也会暂停**,因为所有结果都经由 D 通知;冻结资金保持不动。写入 ADR。
  - D 需要可靠地发送通知(按序、重发直到 A 确认,见 D21),这要求 D 保存待确认的通知。

### D4. 三层幂等

| 层 | 防止的重复 | 键 | 实现 |
| --- | --- | --- | --- |
| 请求层 | 用户连点、HTTP 重发 | 前端生成的 `client_order_id`(充值另有自己的键) | `UNIQUE (user_id, client_order_id)`,重复请求返回已有订单 |
| 订单层 | A 重发导致同一订单两次进入撮合 | A 生成的 `order_id`(ULID),原样交给 D | D 依它去重;改单走 `POST /v1/modifies`(D24) |
| 通知层 | 同一通知或成交被处理两次 | D 的 `notify_seq`;成交另用 B 的 `(day, seq)`,**不用 `order_id`** | `notify_cursor`、`trades` 主键、`ledger_entries` 唯一约束,与结算同一事务 |

- **理由:**
  - 只靠 A 生成的 key 挡不住连点:两次点击是两个请求,会生成两个不同的 `order_id`。
  - 一张订单可以分多次成交,这些成交的 `order_id` 相同,用 `order_id` 去重会把第二笔成交误判为重复。
- **`order_id` 用 ULID:** 全局唯一且按时间有序,只用于 A 与 D 之间。B 的 `order_id` 是整数,由 D 另外分配 `b_order_id` 并负责对照,所以 A 不需要关心 B 的「同一交易日不重复」契约。
- **订单结案条件:** 状态为终结状态,**且**已结算数量等于累计成交量。两个数字都来自 D:`filled_qty` 由 `ORDER_UPDATE` 带来的累计成交量覆盖,`settled_qty` 在每次结算 `TRADE` 时累加。B 对同一次撮合先输出 `Trade`、后输出 `OrderEvent`,所以正常情况下两者在终态时已相等;这个条件仍然保留,成本只有两个字段和一次比较,还能发现 D 的排序错误。结案时该订单的冻结金额必须为 0。

### D5. 只依据 D 的通知释放资金;卡住的订单用退避查询对账

- **决策:**
  1. 只要请求可能已到达 D,冻结资金就只在收到 D 的通知时释放(规则见架构文档 5.2)。确认收到、超时、查询结果都不释放。唯一的例外是请求确定没有离开 A(D12)。
  2. **已送出但没有收到回复:** 订单为 `UNKNOWN`,以同一个 `order_id` 指数退避重发(D 会去重),最多 `RESEND_WINDOW`;仍无确认就改发撤单,等撤单结果的通知。
  3. **已确认但迟迟没有通知**(停在 `SENT`):10 秒后向 D 查询该订单状态,之后 20 秒、40 秒,再每 60 秒一次。若 D 已有 A 没收到的结果,视为漏了通知,请 D 从 A 的 `notify_seq` 重发;若 D 仍在处理,继续等待,界面显示「处理延迟」。用户可随时撤单。
- **理由:**
  - 超时和确认都不代表订单的最终结果,此时释放资金,之后成交就会用到已释放的钱。
  - 只「一直冻结、不查询」,通知丢失时没有人会发现。每 5 分钟查一次则太慢:系统的延迟预算以毫秒计,demo 也只跑几十分钟,资金被冻结 5 分钟才发现问题,用户体验和演示都不可接受。退避查询在前几十秒密集确认,之后放慢,不给 D 造成负担。
  - 查询是只读的,不会改变订单。改用「超时就撤单」虽然也能逼出结果,但会违背用户想保留订单的意图。
  - 「超过 `RESEND_WINDOW` 改发撤单」是为了避免订单在用户下单很久之后才生效。
- **实现方式:** 回复的 `last_notify_seq` 大于 A 的游标,就调用 `POST /v1/notifications/rewind`,从游标 + 1 重发。回 `404` 时 A 发撤单,留下墓碑后由 `CANCEL_RESULT TOMBSTONED` 释放资金。
- **后果:** D 不可用期间资金保持冻结,这是提案已接受的代价:冻结资金换取永不以已释放资金成交。

### D6. 数据库:PostgreSQL + SQLAlchemy 2 Core(async)+ asyncpg + Alembic

- **决策:** 用户、账本、订单、成交存于独立的 PostgreSQL 容器。使用 SQLAlchemy Core 手写 SQL(不用 ORM),Alembic 管理表结构版本。
- **冻结用一条条件更新完成:**
  `UPDATE accounts SET available = available - :amt, held = held + :amt WHERE user_id = :u AND asset = :a AND available >= :amt`
  影响 0 行即余额不足。插入订单、冻结、写流水在同一事务内提交。
- **理由:**
  1. 余额需要强一致与行级锁;多用户并发写入时,SQLite 单写者会成为瓶颈。
  2. 条件更新本身是原子操作,不需要先读后写,不会出现两张单同时读到「余额够」的竞态,也不会死锁。
  3. 表上加 `CHECK (available >= 0 AND held >= 0)`,即使代码有错,数据库也拒绝负余额。
  4. Core 的 SQL 显式可见,没有 ORM 的隐式加载与自动 flush。
- **备选:** `SELECT ... FOR UPDATE` 悲观锁(正确,但多一次往返);乐观锁版本号(争抢同一账户时反复重试);SQLite。
- **后果:** compose 多一个 PostgreSQL 容器和命名卷。

### D7. 结算与游标同一事务,实现等效一次

- **决策:** 每处理一条 D 的通知,在**同一事务**内完成:余额变动、写流水、记录成交(B 的 `(day, seq)`)、推进 `notify_cursor`。
- **理由:** D 会重发通知。结算和游标一起提交,A 崩溃重启后,重复的通知被游标和唯一约束挡下,效果上就是恰好一次。
- **后果:** 每条通知一次数据库事务;Week 6 基线测量后确认吞吐足够。

### D8. 金额一律用整数

- **决策:** 价格以 tick 的整数倍、数量以 lot 的整数倍表示;余额以各资产的最小单位存为 `BIGINT`。只在 API 边界用 `Decimal` 与十进制字符串互转。**全系统不使用 float。**
- **理由:** 与 B 的整数 tick/lot 一致(B D5),无浮点误差,账本加总逐位可验证。
- **后果:** tick、lot、各资产最小单位放在 A 的交易对配置中(D10)。一个单位代表多少,全组于 11-19 采用 B 的 `pairs.json`:价格 2 位小数、数量 6 位小数,BTC-USDT lot 100(0.0001 BTC)、ETH-USDT lot 1000(0.001 ETH);USDT 也与 B 相同为 6 位小数。所有 `价格 × 数量` 经同一个函数换算成 USDT 单位,有余数即报错;因为 1 tick(0.01)× 1 lot(0.0001 BTC、0.001 ETH)是整数个 USDT 单位,结果总是精确,加载配置时会检查这一点。

### D9. 限流:每用户内存令牌桶

- **决策:** 每个用户一个令牌桶,初始值为每秒 5 单、突发 10 单。撤单使用独立且容量更大的桶,保证用户随时能撤单。参数可配置。
- **理由:**
  1. 令牌桶允许短暂突发,同时限制平均速率,符合人工交易的使用模式。
  2. A 是单进程(D2),不需要 Redis。
  3. 撤单是降低风险的操作,不应被下单配额挡住。
- **备选:** 固定时间窗计数(窗口边界会允许两倍突发);Redis 集中式限流(多实例才需要)。
- **后果:** A 重启后桶会重置,短时间内限流偏松,可接受。限流保护「单个用户太快」,负载削减(D13)保护「整个系统过载」,两者分开。

### D10. 订单大小管理:按交易对的静态配置

- **决策:** 每个交易对配置 `tick`、`lot`、`min_qty`、`max_qty`、`max_notional`。价格须为 tick 的整数倍,数量须为 lot 的整数倍,名目金额 = 价格 × 数量,不得超过 `max_notional`。市价单以保护价(D11)估算名目金额。改单使用同样的检查。
- **理由:** B 从 `match_engine/config/pairs.json` 读交易对,D 从中复制 `tick`、`lot`,而 A 还需要自己的限额。所以 A 有自己的配置,小数位、`tick`、`lot` 与 B 相同;`tests/test_pairs.py` 检查三份文件一致,避免「A 放行、B 拒绝」。11-18 曾因 A 用 3 位小数、lot 1 而 B 用 6 位、lot 100/1000,B 以 `INVALID_QTY` 拒绝 A 小于 0.1 BTC 的订单,数量也相差 1000 倍;整合测试发现后改正。
- **备选:** 按用户分级限额(超出本项目需要)。
- **后果:** 修改限额需要重启 A,可接受。

### D11. 价格保护

- **决策:**
  - **新鲜度:** 距离 A 最后收到该交易对任何消息(含心跳)的时间。超过 3 秒,限价单与市价单都以 `NO_FRESH_PRICE` 拒绝(fail-closed)。`last_ms` 只用来判断新旧,重连时重设。
  - **参考价:** 依序取 `last_price`、买一与卖一的中间价、仅有的单边最优价。
  - **限价单:** 价格须在参考价 ±10% 以内。行情新鲜但完全没有价格时(例如当日开始时)跳过这项检查。
  - **市价单:** 买单按「卖一价 × 1.05」、卖单按「买一价 × 0.95」计算保护价,用于名目金额检查与冻结金额。没有对手方时以 `NO_LIQUIDITY` 拒绝。
  - **市价买单的发送方式:** 用户看到的仍是 `MARKET`,但 A 以 `LIMIT` 和上限价(卖一价 × 1.05,向下取到 tick)发给 D。`ACCEPTED` 提交后立即撤销剩余;崩溃时由 `orders.reconcile` 补发撤单。市价卖单仍是真正的 `MARKET`。
- **理由:** 防止明显输错的价格;市价单的成交价在下单时未知,需要一个上限才能确定冻结金额。参考价过期时 ±10% 的保护失去意义,所以限价单与市价单规则一致。
  - 心跳重复相同的 `last_ms`,用它判断新鲜度会把平静但正常的市场当成过期。
  - 一律要求参考价的话,当日第一张单永远下不了,订单簿也就一直是空的。
  - B 的市价买单没有价格上限,薄盘口下成本可能超过冻结金额,余额变负就会使全部结算停止(D28)。负责人决定以 A 的调整为主,不要求其他服务修改。
- **备选:** 由 B 和 D 支持市价单价格上限(语义最正确,但需要两个服务修改);冻结全部可用余额(仍无法保证足够,也不合理)。
- **后果:**
  - 成本永远不超过冻结金额。代价是剩余部分在撤单到达 B 之前的短暂时间内挂在簿上:会出现在订单簿中,其他订单可能以不超过上限价的价格与它成交,`last_price` 也可能短暂跳动。
  - 市价卖单在 B 没有价格下限;卖单冻结的是基础资产,不会透支。

### D12. 调用 D:超时、重发、熔断

- **决策:**
  - 调用分别设置连接超时和总超时;因为 D 只确认收到,超时针对的是确认,而不是风控决定。
  - 按失败类型决定如何回应用户:
    - **确定 D 没有接收**(削减或熔断中未调用;`ConnectError`、`ConnectTimeout`、`PoolTimeout`;首次发送就收到 D 的 `503` `RISK_UNAVAILABLE`、`RISK_BUSY`,此时 D 不留记录):回 `503 SYSTEM_BUSY`。首次发送收到 D 的 `400`(也不留记录)同样释放并告警,回 `400`。若已建立订单,则标记为 `REJECTED`(`D_UNAVAILABLE`)并立即释放冻结,**之后绝不重发**。前端再次尝试时使用新的 `Idempotency-Key`。
    - **重发 `UNKNOWN` 订单时收到 `503` 或 `400`:** 维持 `UNKNOWN`,因为之前的请求可能还在途中。
    - **D 回 `409`:** A 的 bug,保留冻结并告警。
    - **已送出但没有回复**(`ReadTimeout`、连接中途断开、其他 5xx):回 `202`,订单为 `UNKNOWN`,保留冻结,界面显示「确认中」并提醒勿重复下单;以同一个 `order_id` 指数退避重发,最多 `RESEND_WINDOW`(D5)。
  - 撤单重发直到确认。
  - 熔断器自行实现(约 40 行):连续 5 次失败、超时或 `RISK_UNAVAILABLE` 则打开,10 秒后半开,放行 1 个试探请求。打开期间,新单在**冻结资金之前**就以 `SYSTEM_BUSY` 拒绝;撤单不经过熔断器,改为排队重发。
- **理由:** 请求已送出时告诉用户「失败,请重试」,D 却可能已收到订单,用户重试就会产生两张单,若同时释放冻结还可能把账户扣成负数。反过来,既然告诉用户失败,就不能在背后重发。D 依 `order_id` 去重,所以重发新单是安全的;熔断在冻结前判断,避免为注定失败的订单冻结资金。自行实现熔断器逻辑透明,不引入以同步为主的第三方库。
- **备选:** pybreaker、tenacity 等第三方库。
- **后果:** 熔断参数需在故障注入测试中调整。

### D13. 负载削减在 A,D 的规则始终开启

- **决策:** A 维护 30 秒滑动窗口内「从送出订单到第一个结果(`ACCEPTED`、`RISK_REJECTED` 或 B 拒绝)」的时间。仍在等待的订单按当前年龄计入,`RISK_BUSY` 视为超过阈值的样本。连续三次(每秒评估一次)p99 超过 D 在预算中的份额,就以 `SYSTEM_BUSY` 拒收新单;p99 回落到阈值的 80% 以下才恢复。撤单永不削减。
- **理由:** 实现提案中降级模式的决策:过载时在入口拒单,而不是关闭 D 的规则。D 的 `202` 总是很快,反映不出 D 内部的排队,所以不用它作为依据。在 A 本地测量,不依赖监控系统。回滞(80%)避免在阈值附近反复切换。
- **后果:** 过载时部分用户无法下单(用可用性换保护)。阈值由 Week 6 基线确定。

### D14. 推送前端使用 SSE

- **决策:**
  - `/api/stream` 以 Server-Sent Events 推送该用户的订单与余额更新。
  - 处理通知的同一事务写入 `user_events`(`event_id` 递增);提交之后才推送,事件带 `id`。
  - 浏览器重连时依 `Last-Event-ID` 或 `?last_event_id=` 按序补发。ID 已被清理(保留 24 小时)时,前端重新抓取 REST。
  - 不另设轮询 outbox 的后台任务:提交与推送之间只有 A 崩溃才会中断,而 A 崩溃时所有 SSE 连接都会断开,重连时就会补发。
  - 公开的行情数据不写入 `user_events`,否则每笔成交要为每个用户写一行。
- **理由:** A 只需单向推送,SSE 比 WebSocket 简单。提交后才推送,避免用户看到随后被回滚的成交。事件日志使断线重连时只补漏掉的事件,且保证顺序。
- **备选:** WebSocket(双向,但 A 用不到);提交后推送、重连时全部重新抓取 REST(最简单,但没有逐条不漏的保证);只加 outbox 表而不补发(只保证事件进入 hub,不保证浏览器收到,价值很小)。
- **后果:** 多一张表和定期清理。`EventSource` 在收到 401 后会永久关闭,新建连接也不带 `Last-Event-ID`,因此前端要在 access token 到期前换发,并自行记录最后的 `event_id` 作为备援。access token 到期时服务端关闭 SSE,以免连接绕过 token 的有效期。

### D15. 前端:React + Vite + TypeScript,A 负责外壳与交易页

- **决策:**
  - 前端构建为静态文件,由 nginx 容器在 3000 端口提供。版面参考常见的现货交易页,使用 LAME 自己的名称和标识。
  - 两个页面:**交易页**(主页,A 负责)与 **News & Analyst 页**(C 负责,以全页 `<iframe>` 嵌入 A 的外壳)。
  - 技术:TanStack Query 管理服务器数据;Lightweight Charts 画 K 线;Tailwind 与 CSS 变量做深色主题;React Router 管理路由,交易对放在网址中。不使用 Zustand 或 Redux。
  - 订单簿:B 每个交易对最多约每秒 1,000 条快照,只保留最新一份,约 100ms 更新一次画面。
  - C 的聊天框用 `<iframe>` 嵌入 `/widget`,不传交易对;C 不可用时 A 显示离线提示。新闻面板由 A 调用 `GET /api/news?symbol=` 绘制。
  - 内部保留完整状态机,界面显示简化标签(对照表见架构文档第 13 节)。
  - 交易页区块:左侧订单簿(浏览器直连 B 的 WebSocket);中间 K 线图与下单区;右上市场列表;右侧中间 **C 的聊天框预留位**(取代「最新成交 / 我的成交」);右下所选交易对的新闻(C 的新闻接口);下方「当前委托、历史委托、历史成交、持有币种」分页。区块与数据来源对照见架构文档第 13 节。
- **理由:**
  - TypeScript 类型可与 A 的 API schema 对齐;订单簿直连 B,少一跳,A 不用转发行情流量。
  - B 的每条快照都是完整的,跳过中间的快照不丢信息,而每条都渲染会使画面卡顿。
  - C 已在 `main` 交付独立页面、widget 和 REST,不需要共用 React 代码。JS widget 运行在 A 的网域,可以带着用户的 cookie 调用下单接口,还会注入全局样式;`<iframe>` 跨来源,接触不到 A 的页面。
- **备选:** Redux Toolkit;ECharts、Recharts;CSS Modules、组件库;JS widget;用 C 的 REST 自己做聊天 UI(聊天 UI 属于 C 的范围)。
- **后果:** 依赖 C 的 `/widget` 独立运行。C 拒绝时可退回 JS widget,但要接受全局样式与脚本信任的风险。

### D16. 关联账户

- **决策:** 关联账户定义在 D 已有的 `linked_accounts.json` 中(同一组视为同一人控制),由 A 与 D 分别读取,D 用于洗量交易规则(R2)。A 依它建立种子用户并沿用其中的 `user_id`,自行注册的用户从 100000 开始编号,不属于任何组。`user_id` 是 `BIGINT`,因为 D 与 B 需要整数。
- **理由:** 账户归 A 管理,A 是用户资料的唯一权威。关联名单是刻意设计的测试场景,配合 B 的操纵者机器人,让 D 能测量 precision 与 recall。
- **后果:** A 不需要 `linked_accounts` 表,也不需要提供接口。机器人使用种子账户,经过 A 的公开 API。

### D17. 容器布局

- **镜像:** `python:3.12-slim`,多阶段构建,依赖版本锁定,非 root 用户运行。
- **代码位置:** `service_a/`(后端、迁移、测试)与 `frontend/`,与现有的 `service_c/`、`match_engine/` 并列。
- **端口:** 8000(API、SSE、`/internal/order-events`、`/healthz`、`/metrics`)。`/internal/*` 只接受 compose 网络内的调用。3000 为前端。
- **前端转发:** nginx 把 `/api` 转给 A,使 cookie 同源;浏览器直连 B 与 C。开发时由 Vite 以相同方式转发。
- **数据库:** PostgreSQL 容器 + 命名卷;Alembic 迁移完成后 `/healthz` 才返回健康。
- **配置:** 挂载交易对配置文件(与 B 共用);环境变量配置数据库地址、token 密钥、限流参数、超时、削减阈值、`RESEND_WINDOW`。

### D18. 运行指标

| 指标 | 含义 |
| --- | --- |
| `lame_a_orders_total{result}` | 按结果分类的订单数 |
| `lame_a_http_request_duration_seconds{endpoint,method,status}` | API 延迟，按状态码算错误率 |
| `lame_a_d_ack_latency_seconds` | D 的确认延迟(仅用于监控) |
| `lame_a_order_completion_seconds` | 下单到第一个结果的时间(负载削减的输入) |
| `lame_a_circuit_state`、`lame_a_shedding_active` | 保护机制状态 |
| `lame_a_orders_by_status{status}` | 各状态订单数,尤其 `UNKNOWN`、`SENT` |
| `lame_a_notification_lag_seconds` | D 生成通知到 A 结算完成的延迟;拿不到 B 的时间 |
| `lame_a_notification_gaps_total` | 以 409 回应的缺号次数 |
| `lame_a_duplicates_ignored_total{layer}` | 各幂等层挡下的重复数 |
| `lame_a_auth_failures_total` | 登录失败次数 |
| `lame_a_notify_failures_total{reason}` | 以 `422` 拒绝的通知数 |
| `lame_a_notify_cursor_age_seconds` | 游标距上次前进的时间 |
| `lame_a_orders_unsettled` | 已是终态但尚未结案的订单数 |
| `lame_a_bg_task_restarts_total{task}` | 后台任务重启次数 |
| `lame_a_md_age_seconds{symbol}` | 距离 A 最后收到该交易对任何消息的时间 |

### D19. 第三方依赖

| 依赖 | 用途 | 授权 |
| --- | --- | --- |
| fastapi、uvicorn、pydantic | Web 框架与校验 | MIT / BSD |
| sqlalchemy、asyncpg、alembic | 数据库访问与迁移 | MIT / Apache 2.0 |
| httpx | 调用 D | BSD |
| websockets | 订阅 B 的行情 | BSD |
| sse-starlette | SSE | BSD |
| prometheus-client | 指标 | Apache 2.0 |
| python-ulid | `order_id` 生成 | MIT |
| argon2-cffi | 密码哈希(D20);不经过 passlib | MIT |
| PyJWT | access token(D20) | MIT |
| pydantic-settings | 从环境变量读取配置 | MIT |
| pytest、pytest-asyncio、hypothesis | 测试 | MIT / Apache 2.0 / MPL 2.0 |

前端依赖:

| 依赖 | 用途 | 授权 |
| --- | --- | --- |
| @tanstack/react-query | 服务器数据缓存 | MIT |
| lightweight-charts | K 线图 | Apache 2.0 |
| react-router-dom | 路由 | MIT |
| tailwindcss | 样式 | MIT |
| openapi-typescript | 由 OpenAPI 生成类型(开发用) | MIT |
| msw、vitest、@testing-library/react | mock 与测试(开发用) | MIT |

均为活跃维护的主流库。熔断器自行实现(D12)。安装时再核对版本、授权(含 lightweight-charts 的署名要求)并运行漏洞检查。

### D20. 注册与登录

- **决策:**
  - 用户以 `username` + `password` 注册与登录。不收集邮箱、手机或任何真实个人信息;不做找回密码与身份验证(KYC)。
  - **token:** 短效 JWT 加 refresh token,都放在 HttpOnly cookie。access token 用 HS256 和 PyJWT,有效期 15 分钟,验证时不查数据库,`user_id` 从 token 取得。
  - **refresh token:** 随机字符串,效期 7 天(暂定),只存哈希;每次换发都轮换。已用过的 token 再次出现即视为被盗,作废同一 family 的全部 token。短暂的宽限期和前端的单次换发避免多分页同时换发造成误判。
  - **登出:** 作废 refresh token family 并清除 cookie。
  - **密码哈希:** Argon2id(`argon2-cffi`),在线程池中执行。
  - **登录防护:** nginx 按 IP 限流,加上哈希并发上限,超过的请求立即回 `429`。不做账户锁定。
  - **写入接口:** 只接受 `application/json`,检查 `Origin`,不开启 CORS。
- **理由:**
  - 课程要求系统中只有合成数据(brief 第 7 节),注册只需要能区分用户;种子用户与自行注册的用户使用同一套机制。
  - 每个请求不查数据库,且实现 JWT 是负责人的学习目标。在 A 这个规模下,查询 session 的成本其实可以忽略,所以这是学习价值上的选择,不是性能上的必需。
  - 放在 HttpOnly cookie,SSE 可以直接使用,XSS 也读不到 token。
  - Argon2 占用大量 CPU 和内存(默认每次约 64 MiB,待实现时确认),放在事件循环上会卡住所有下单请求;不限制并发,登录洪流可能使容器内存耗尽。
  - 账户锁定会让攻击者能故意锁住受害者。同一主机的不同端口属于同一个 site,所以 `SameSite` 不足以阻止 C 的页面向 A 发请求。
- **备选:** 服务端 session(可立即撤销,但每个请求查库);JWT 加内存黑名单(可立即撤销,但 A 重启后失效,且把 A 绑定在单实例上);bcrypt(有 72 字节限制)。
- **后果:** **登出后,已被复制的 access token 最多还能用 15 分钟**,这是明确接受的代价。只删除浏览器中的 token 不能使 token 本身失效。

### D21. 接收 D 的通知:按序、幂等、缺号由 A 告知

- **决策:**
  - 每条通知带 D 分配的连续 `notify_seq`;来自 B 的通知另带 B 的 `(day, seq)`。
  - D 一次只送一条,按序发送,未收到 2xx 就重发同一条。
  - A 提交事务后才回 `200`;重复的通知(`notify_seq` 已处理)也回 `200`,不再处理;跳号的通知回 `409` 并附**最外层**的 `{"expected_seq": N}`,D 从该编号重发。D 直接读取这个键,所以不能用 FastAPI 的 `HTTPException`。
  - D 的通知超时是 2 秒,A 要在这之内处理完。
- **理由:**
  - HTTP 回调会重试、可能乱序。连续编号让 A 能判断重复与缺号;由 A 在回应中告知期望的编号,不需要另设补发接口。
  - 按序处理才能保证「订单事件早于其后的撤单结果」这类先后关系,释放资金的规则(D5)依赖它。
- **备选:** D 并发发送、A 自行排序缓冲(更快,但 A 要处理缓冲与超时,复杂度高);A 定期向 D 拉取(延迟高)。
- **后果:** 一次只送一条会限制通知吞吐;Week 6 基线测量后确认足够。若不足,先改为一次发送一批连续编号、在一个事务内应用。D 需要保存未确认的通知。A 内部的处理方式见 D28。

### D22. K 线由 A 从成交计算

- **决策:** A 把每笔结算过的成交存入 `trades` 表,`GET /api/market/candles` 按时间桶聚合出 OHLCV:开 = 桶内第一笔成交价,高 / 低 = 最高 / 最低成交价,收 = 最后一笔成交价,量 = 成交量加总。周期为 **5 秒、15 秒、1 分、5 分**,默认 15 秒。MA(7)、MA(25)、MA(99) 由前端依收盘价计算。没有成交的时间桶不产生 K 线,不填补假数据。
  - **计算:** 查询时用 SQL 聚合(PostgreSQL 14 以上的 `date_bin`),每个 `(symbol, interval)` 有 1 秒的内存缓存;API 默认返回 300 根。
  - **时间:** 使用通知中的 `ts`(D 生成通知的时间),因为 B 的 `Trade` 没有时间戳。A 断线后补发的成交仍会落入正确的桶,D 与 B 断线后补发的成交则会挤在一起,这是接受的代价。桶内顺序依 `(b_day, b_seq)`,因为同一命令产生的多笔成交 `ts` 相同。
  - **实时更新:** 前端每秒轮询,带上 `after_seq`。A 从之后新增成交中最早的时间桶开始重算并返回,这样补发的成交也能修正旧 K 线。换日时重新抓取。
  - **demo:** 演示前让 B 的机器人至少运行 25 分钟,使 5 秒与 15 秒的 MA(99) 有值;1 分和 5 分的 MA(99) 可能画不出来。
- **理由:**
  - B 不提供 K 线和成交历史(B D6);A 为了结算本来就收到全部成交,用它计算最准确,也能算出成交量。用行情快照的 `last_price` 取样会漏掉成交,也算不出成交量。
  - demo 运行时间短,用秒级周期才能在图上显示足够多的 K 线。
  - 查询时聚合不加重结算事务,结果也一定与 `trades` 一致。公开数据共用缓存,所以轮询的成本低。
- **备选:** 由行情快照取样;请 B 提供 K 线;维护 `candles` 表并在结算时更新(每笔成交多 4 次 upsert,加重循序瓶颈);内存维护;SSE 广播 K 线(未登录时看不到,需要另一条公开通道);灌入假的历史成交(违反本决策)。
- **后果:** 系统刚启动时图是空的。C 需要 K 线时可向 A 取得,避免两套计算结果不同。**改变决策的事实:** 实测首次加载 p95 超过 200ms(暂定),或成交速率使轮询扫描过多行时,改为由后台任务依水位更新的汇总表。

### D23. 市场列表

- **决策:** 最新价取 B 快照中的 `last_price`(浏览器直连取得);涨跌幅以**当日开盘价**(当日第一笔成交价,由 A 的 `/api/market/summary` 提供)为基准:(最新价 − 开盘价)÷ 开盘价;价格的小数位数由交易对的 tick 决定。列表只有提案范围内的两个交易对。
- **理由:** demo 运行时间远短于 24 小时,「24 小时涨跌」没有可比较的数据;以当日开盘价为基准,demo 期间就能显示有意义的涨跌。
- **备选:** 买一和卖一的中间价(不是实际成交价;单边无挂单时算不出来);24 小时涨跌。
- **后果:** 当日尚无成交时不显示涨跌。依赖交易日的定义。

### D24. 改单沿用同一个 order_id

- **决策:** 改单只改数量,不改价格(11-18 团队决定),数量可增可减。`PUT /api/orders/{order_id}` 只接受新的**剩余** `qty`,带价格的请求回 `422`;A 以同一个 `order_id` 通过 `POST /v1/modifies` 交给 D,不带价格(D 协议 2.3:不带即不变),由 D 转为撤单加新单。不再检查 ±10% 价格带(价格没变,挂单时已检查过),但仍检查数量与名目金额上限。市价单、`PENDING`、`UNKNOWN` 状态的订单不能改单。D 拒绝改单(例如 `501`)时,A 释放补冻结的部分并回 `MODIFY_UNAVAILABLE`。所需资金增加时,A 先补冻结差额再发送;所需资金减少时,收到改单成功的通知后才释放差额,因为确认之前旧订单仍可能按原数量成交。改单期间订单显示 `MODIFYING`;改单被拒时订单维持原样,补冻结的部分释放。
- **理由:** 用户对「同一张订单」的认知不变,A 的订单表、历史记录与前端显示都只需要一个 ID。
- **备选:** A 自行撤单 + 以新 `order_id` 下新单(不依赖 D 的改单接口)。实现时两种做法都保留,可通过配置切换,以便在团队讨论后快速调整。
- **对 D 文档 2.3 的回答:**
  - 撤单途中又成交:新数量 = 请求的 `qty` 减去途中成交量,否则总量可能超过用户的意图;`MODIFY_RESULT` 报告实际的 `price` 和 `qty`。
  - 旧单已全部成交:`MODIFY_RESULT REJECTED`(`ALREADY_FINAL`),A 释放补冻结的部分。
  - 失去时间优先权:可以接受,因为 B 没有改单功能。
  - 旧单已撤、新单被拒(D 未列出):先送 `ORDER_UPDATE CANCELLED`,再送 `MODIFY_RESULT REJECTED`。
- **后果:** 改单期间到达的成交按旧的冻结价结算,直到 `MODIFY_RESULT`。在 D 完成之前,用户无法使用 D 的改单。

### D25. 持有币种

- **决策:** 「持有币种」分页显示每种资产的可用、冻结与总额,数据来自 `GET /api/accounts/me`,通过 SSE 实时更新。
- **理由:** 资产本来就由 A 管理,不需要额外数据来源。

### D26. 模拟充值

- **决策:** 新用户初始资产为 0。用户在界面上选择资产与金额进行模拟充值,立即入账,并写入 `DEPOSIT` 流水。每次充值有上限(可配置),以 `Idempotency-Key` 防止重复入账。可充值的资产为交易对配置中出现的资产。不提供提现。
- **理由:** 系统没有真实资金,模拟充值让自行注册的用户也能完整体验下单流程,且不涉及真实个人或支付信息。
- **后果:** 账本不变式改为「各资产全体用户 `available + held` 之和 = 种子总额 + 全部充值」。

### D27. 后端结构

- **决策:**
  - **按功能分模块:** `auth`、`accounts`、`orders`、`market`、`push`、`shedding`、`metrics`、`seed`。与 D 的往来都属于订单生命周期,所以放在 `orders` 下:`orders.precheck`、`orders.dclient`、`orders.events`、`orders.reconcile`。
  - **依赖方向:** router → service → repository 或 adapter。service 不导入 FastAPI。模块之间只调用对方的 service,不碰对方的 repository。`accounts` 是唯一写入余额的模块。
  - **port 只用于需要替换的依赖:** D、B 的 WebSocket、时钟、ID 生成器。PostgreSQL 不设 port。
  - **依赖组装:** 在 FastAPI 的 lifespan 中统一创建单例,router 通过薄的 `Depends` 取用,后台任务在创建时接收。
  - **事务:** 由最外层 service 开启,把 connection 传给 repository。
  - **后台任务:** 在进程内以 asyncio task 运行,由 supervisor 记录异常、退避重启并报告给 `/healthz` 与指标。行情的单条坏消息只跳过和计数。
  - **错误处理:** service 抛出业务异常,在一处映射为 HTTP 状态码与 `reason`。配置用 `pydantic-settings` 读取环境变量。
- **理由:**
  - 按功能分模块,改一个功能只动一个目录,模块边界也清楚。只在外部依赖处设 port,避免一次性的抽象;用假的 PostgreSQL 反而测不到条件更新和事务行为。
  - 限流器、熔断器、SSE hub 有状态,必须只创建一次;后台任务不经过 HTTP 请求,`Depends` 无法提供依赖。
  - 冻结与下单、结算与游标都要求多张表在同一事务内提交(D6、D7)。
  - 行情缓存必须与下单的价格检查在同一进程;没有被 await 的 asyncio task 出现异常时会被默默吞掉。
- **备选:** 按技术分层;完整的六角架构;每个请求用 `Depends` 创建依赖;依赖注入容器;每个 repository 自己开启事务;Celery 等独立 worker。
- **后果:** `orders` 是最大的模块,以子目录划分。repository 函数都多一个 connection 参数。

### D28. 通知的处理方式

- **决策:**
  - 在 handler 内同步应用,提交后才回 `200`;不设 inbox 表。
  - 用 `SELECT ... FOR UPDATE` 锁住 `notify_cursor`,在同一事务内比较序号并应用(流程见架构文档 5.6)。
  - 暂时性失败回 `503`,由 D 退避重发。格式错误、未知订单、非法转移或违反余额约束回 `422` 并**停线**,绝不跳过。D 对两者都持续重发,所以告警由 A 的指标负责。
  - 合法的晚到通知没有作用,回 `200`:`TOMBSTONED` 之后的 `RISK_REJECTED`、终态订单的任何 `CANCEL_RESULT`。终态订单的 `TRADE` 在 `settled_qty + qty ≤ filled_qty` 时合法。
  - `202` 只把 `PENDING`、`UNKNOWN` 改为 `SENT`,因为 D 的发送器独立运行,通知可能先于 `202` 到达。
  - 所有终态的释放统一为 `qty − filled_qty`,并受订单 `held_amount` 限制,因为 `REJECTED` + `SELF_TRADE_PREVENTED` 可能发生在部分成交之后,同一结果也可能有两条通知。
  - 状态转移集中在一张表中,测试枚举所有组合。
  - 事务提交后才推送 SSE(D14)。
- **理由:**
  - D 本来就会重发,处理也只是几毫秒的事务。inbox 多一张表和一个 worker,吞吐却相同,因为瓶颈是按序处理。
  - 在事务外检查序号会产生 check-then-act 竞态:D 重发时两个请求都判定为下一条,释放资金可能发生两次。把判断和结果放在同一事务,崩溃后重发也只会判定为重复。锁放在数据库,正确性不依赖单实例。
  - 账本系统中,错账比暂停更难修复。
- **备选:** inbox 加后台 worker(D 的超时很短时才需要);`asyncio.Lock`(依赖单实例);条件更新游标的乐观锁(正确,但第二个请求会白做一次);死信表后跳过(账会错)。
- **后果:** 一条坏通知会暂停全部结算,需要依靠告警及时发现。假设值每条 2–5ms,约每秒 200–500 条,待基线实测;不足时先改批量通知。信任 D 分配的序号,不校验同一序号内容是否相同。

### D29. 开发顺序与测试

- **决策:**
  - 先定 API 契约:Pydantic schema 加回 `501` 的空路由,由 OpenAPI 生成 TypeScript 类型;SSE 事件类型手写。
  - 按 M0–M6 推进(架构文档第 17 节):前端先用 mock 做出画面,再逐步接上真实后端。开发时在页面内模拟 A 的 API 与 B 的行情(不使用 Service Worker,因此不增加部署文件),C 用真实服务。
  - 测试分层:单元、真实 PostgreSQL 的集成、hypothesis 不变式、fake D 的故障、前端逻辑,以及 `tools/integration_check.py`(真实的 A 与 D,加 D 自带的 fake B)和一次浏览器检查。
- **理由:** 两端都由同一人开发,一份契约可以避免不一致。fake D 使 A 不必等 D 完成。A 的正确性依赖 `FOR UPDATE`、`CHECK` 与条件更新,SQLite 无法重现。
- **备选:** 手写 TypeScript 类型;自写 mock server;Testcontainers;SQLite。
- **后果:** fake D 依 `feature/risk_control` 的代码行为编写。11-17 已在本机让 A、D、B、C 与前端一起运行(`service_a/tools/run_local.ps1`),`service_a/tools/local_e2e.py` 对真实的 B 与 D 通过,包括 D 的改单。打包进根目录的 compose 尚未完成。系统负载测试与故障注入脚本按提案属于 B 与 D,A 不重复。

## 3. 待决事项(A 内部)

1. **数值参数:** 限流速率、价格带宽(±10%)、保护价系数(5%)、超时、`RESEND_WINDOW`、对账查询间隔、削减阈值、单次充值上限,都是初始值,待 Week 6 基线测量后确定。
2. **宽限期:** refresh token 重复使用的宽限秒数(D20),实现时确定。

## 4. A 如何配合其他服务

A 以 D 与 B 目前的实现为准进行调整,以下事项都不要求队友修改。依据是阅读 `feature/risk_control`(`d1635ed`)与 `feature/match_engine`(`8b2e3c5`)的代码,尚未实际联调。

| 服务目前的行为 | A 的做法 |
| --- | --- |
| D 收到 `422` 后持续重发、不告警 | 由 A 的指标告警(D28) |
| D 已实现改单(11-06),不带价格即保持原价 | A 只送新的数量(D24) |
| D 的 `ts` 是生成通知的时间;B 的 `Trade` 没有时间戳 | K 线使用 D 的 `ts`(D22) |
| B 的市价单没有价格上限,D 拒绝市价单带价格 | 市价买单以带上限的限价单发送,再撤销剩余(D11) |
| B 的心跳重复相同的 `last_ms` | 新鲜度以收到消息的时间计算(D11) |
| B 在都柏林时间 00:00–23:00 接单(11-06 由 09:30–16:00 改为此时段),23:00 撤销所有挂单 | 时段外界面显示「休市」 |
| B 的交易日是都柏林日期 `YYYY-MM-DD` | 当日开盘价依 `b_day` 计算 |
| B 读 `pairs.json`(6 位数量小数,lot 100/1000),D 复制其 `tick`、`lot` | A 的交易对配置采用相同的小数位、`tick`、`lot`,`tests/test_pairs.py` 检查三者一致(D10) |
| D 的 `linked_accounts.json` 写定了 `user_id` | A 依同一文件建立种子用户(D16) |
| 机器人经过 A | 使用公开 API 和种子账户,没有专属功能;限流参数可配置 |
| C 的聊天框发送它自己选单中的交易对 | 接受现状,A 只负责嵌入 |

**需要全组确认(不要求改代码):**

1. **价格与数量的小数位:** 各服务都用整数,但没有服务定义一个单位代表多少。A 需要它才能转换十进制,并从 `price × qty` 算出名目金额。
2. **改单:** D 的文档向 A 提出三个问题,A 的回答见 D24。
