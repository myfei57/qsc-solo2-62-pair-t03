# 供液阀-压缩机启停顺序联锁设计

## 背景

供液阀与压缩机此前各走各的：`SupplyValve.start_with_supply` 把"开阀"写入存储后
立即启机，读回的是自己的写入而非现场回讯——出现过阀未开就启机、泵抽空的事故。
本模块（`coldstore.sequence`）把两者绑进一条强制时序：**拿不到回讯，绝不允许启机**。

## 两项拍板决策

### 1. 回讯抖动 → 延时确认（默认），人工放行仅作受控旁路

- 默认路径是**延时确认**：回讯须连续稳定 `debounce` 秒才采信，正常触点抖动由
  联锁自动吸收，不转人工。
- 抖动超限（`chatter_window` 内边沿数 > `chatter_edges`）判定**回讯回路故障**，
  联锁走回退并锁定，必须由 L2 复位。
- **人工放行（manual release）** 保留为仪表确认故障后的受控旁路：L2 签发、
  绑定到具体操作人、单次使用、限时（`bypass_ttl`）有效、全程留痕。
- 理由：抖动是回路故障的症状，若默认转人工放行，等于把仪表故障常态化——
  这正是抽空事故的温床。

### 2. 异常回退 → 终态关阀到位，分级执行，顺序不可逆

- 回讯缺失 < `hold_max`：**保位**（输出冻结）+ 报警，等待恢复；恢复即回到原流程。
- 缺失 ≥ `hold_max`：执行**安全回退时序**——
  停压缩机 → 拿到停机回讯 → 关阀 → 拿到关到位回讯 → 锁定待复位。
- 终态必须是**关阀到位**这个可验证的确定态，不是"保持原位"这个不可知态。
- 回退链路自身异常（停机回讯也缺失）时：**阀门保位、禁止带机关阀**、升级人工。
  绝不为了够到"关阀到位"而在压缩机状态未知时强行关阀，制造第二次抽空。

## 时序参数（L2 可调，持久化于 `sequence:timing`）

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `debounce` | 1.0s | 回讯延时确认（防抖） |
| `open_timeout` | 30s | 开阀回讯超时 → 保位 |
| `stabilize` | 3s | 开到位确认 → 允许启机的最小间隔 |
| `confirm_valid` | 120s | 开到位确认有效窗口，超期重新确认 |
| `start_timeout` | 15s | 启机回讯超时 → 回退 |
| `hold_max` | 5s | 回讯缺失保位上限 → 回退 |
| `stop_timeout` | 15s | 停机回讯超时 → 故障锁定（阀保位） |
| `close_timeout` | 30s | 关阀回讯超时 → 故障锁定 |
| `chatter_edges` / `chatter_window` | 6 / 10s | 抖动判定：窗口内边沿数上限 |
| `bypass_ttl` | 600s | 人工放行许可有效期 |

## 状态机

```
            start(L1)                 开到位回讯+稳定期        运行回讯
IDLE ──────────────────► OPENING ──────────────────► READY ──► STARTING ──► RUNNING
 ▲                          │ 开阀回讯超时                                    │
 │                          ▼                                                 │ 回讯缺失
 │                        HOLD ◄──────────────────────────────────────────────┘
 │                          │ 缺失 < hold_max：保位，可恢复
 │                          │ 缺失 ≥ hold_max：回退
 │                          ▼
 │                     FALLBACK_STOP ──停机回讯──► FALLBACK_CLOSE ──关到位回讯──► FAULT
 │                          │ 停机回讯也缺失：阀保位，禁止关阀，升级人工        （锁定）
 └──────────────────────────┴───────── L2 复位（须确认关到位+停机） ◄───────────┘

正常停机：stop(L1) → STOPPING ──停机回讯──► CLOSING ──关到位回讯──► IDLE
```

## 权限分级

| 级别 | 角色 | 权限 |
| --- | --- | --- |
| L0 | viewer | 只读：状态、追溯查询 |
| L1 | operator | 正常启停（start/stop）、持证放行启机（release） |
| L2 | engineer | 签发人工放行（bypass）、故障复位（reset）、时序参数（timing） |
| L3 | admin | 预留：授权管理 |

越权操作一律拒绝并记录 `REJECT` 事件（含操作人），留痕包括未遂。

## 留痕与追溯

- 每次启停流程分配 `cycle` id；该周期内全部事件（COMMAND / TRANSITION /
  FEEDBACK / ALARM / FAULT / AUTH / BYPASS / REJECT / RESET / TIMING）带同一
  cycle 写入审计流（`Auditor`，kind=`sequence`，detail 为 JSON）。
- **异常链路可追**：从首次回讯缺失 → 保位 → 回退各步 → 锁定 → 复位，一条
  cycle 完整回放。
- **按操作人追溯**：`POST /sequence/trace {"operator": "张三"}`；也可按
  `machine` / `cycle` / `event` 过滤。授权（AUTH）与使用（BYPASS）分别记录
  签发人与持证人，可对碰。

## 运维接口

| 路由 | 说明 |
| --- | --- |
| `GET /sequence` | 全部机组联锁状态与时序参数 |
| `POST /sequence/machine` | 单台机组状态 |
| `POST /sequence/start` | L1 联锁启机流程（开阀→确认→稳定→启机） |
| `POST /sequence/stop` | L1 联锁停机流程（停机→确认→关阀） |
| `POST /sequence/feedback` | 注入现场回讯（open_limit/close_limit/running） |
| `POST /sequence/tick` | 扫描周期：推进防抖与定时器 |
| `POST /sequence/bypass` | L2 签发人工放行许可 |
| `POST /sequence/release` | L1 持证放行启机（单次、限时） |
| `POST /sequence/reset` | L2 故障复位（须确认关到位+停机） |
| `POST /sequence/timing` | L2 调整时序参数 |
| `POST /sequence/trace` | 按操作人/机组/周期/事件追溯 |

请求体约定：`id` 机组号，`operator` 操作人，`role` 角色
（viewer/operator/engineer/admin），`at` 可选时间戳（缺省取系统时间，
测试用显式时间保证确定性）。生产部署时 operator/role 应绑定认证层。

## 运行测试

```
python3 -m unittest discover -s tests
```
