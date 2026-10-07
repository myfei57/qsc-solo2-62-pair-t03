# 供液阀-压缩机启停顺序联锁设计

## 1. 背景

供液阀与压缩机原先各走各的：`SupplyValve.start_with_supply` 写入"开阀"后立即读回
自己的写入当作确认（假回讯），阀实际未开就启机，造成泵抽空。本设计在
`coldstore/interlock/sequence.py` 实现顺控联锁：**必须拿到现场回讯才允许启机**，
异常链路全程可追。

## 2. 两项拍板决策

### 决策一：回讯抖动 —— 默认延时确认，人工放行只做兜底

- **默认延时确认（消抖）**：开阀回讯须稳定保持 `feedback_debounce`（默认 1s）才
  被采信。抖动是测量回路问题而非工艺问题，自动消抖确定性处理，不把人拉进常态回路。
- **抖动越限转人工**：`chatter_window`（10s）内跳变超过 `chatter_max_edges`（6 次）
  判为传感器故障，自动通道闭锁，只能走人工放行。
- **人工放行四要素**：双人（放行人+复核人均 ≥L2 且不同人）、现场确认阀位、填写原因、
  限时（`override_ttl` 默认 30 分钟）自动失效。放行到期且回讯仍无效 → 自动进入异常
  回退。人工放行若常态化，联锁即被架空，所以它必须"重"。

### 决策二：异常回退 —— 短时保位、超时关阀到位，且先停机后关阀

- 运行中回讯缺失按时长分级：
  - `< run_loss_hold`（3s）：视为瞬断，维持输出，仅记录；
  - `3s ~ run_loss_trip`（15s）：**保位**（FAULT_HOLD），阀保持开、机保持转，报警；
  - `≥ 15s`：**异常回退**——先停压缩机 → 拿到停机回讯 → 再关阀到位 → 闭锁待人工复位。
- **严禁带机关阀**：关阀前必须确认压缩机已停（停机回讯超时 30s 才允许按超时推进，
  并报警提示检查硬线停机回路）。带机关阀正是本次抽空事故的复现路径。
- 回讯长期丢失意味着阀位不可知，必须回到"机停阀关"这一已定义安全态，而不是无限期
  保位。开阀阶段超时同理：压缩机从未启动，直接关阀到位后闭锁。

## 3. 时序参数（可整定，L3 权限，改参留痕）

| 参数 | 默认 | 含义 |
|---|---|---|
| `valve_open_timeout` | 20s | 发开阀令后等开到位回讯的时限 |
| `valve_close_timeout` | 20s | 发关阀令后等关到位回讯的时限 |
| `comp_start_timeout` | 15s | 启机令后等运行回讯的时限 |
| `comp_stop_timeout` | 30s | 停机令后等停机回讯的时限 |
| `feedback_debounce` | 1s | 回讯消抖：稳定此时长才采信 |
| `chatter_window` / `chatter_max_edges` | 10s / 6 次 | 抖动风暴判据 |
| `run_loss_hold` / `run_loss_trip` | 3s / 15s | 运行中回讯缺失分级阈值 |
| `override_ttl` | 1800s | 人工放行有效期 |

> 现场整定：开/关阀时限按阀门全行程实测时间 + 50% 裕量；消抖时长应大于触点
> 抖动实测宽度；`run_loss_trip` 应小于泵允许干运转时间。

## 4. 状态机

```
                 start(L1)                开阀回讯确认(消抖)        运行回讯
   IDLE ───────────────► VALVE_OPENING ──────────────────► COMP_STARTING ──────► RUNNING
    ▲                       │                                                        │
    │                       │ 开阀超时                              回讯缺失<3s 无动作 │
    │                       ▼                                        ≥3s ▼ FAULT_HOLD(保位)
    │                  FAULT_CLOSING ◄──────── 异常回退:先停后关 ◄── FAULT_STOPPING ◄┘ ≥15s
    │                       │              FAULT_STOPPING ──停机确认──┘
    │ 关阀到位              │ 关阀到位/超时
    └─────── IDLE ◄─ VALVE_CLOSING ◄─ STOPPING_COMP ◄── stop(L1) ── RUNNING
                               │
                    FAULT_LOCKED(闭锁) ◄── 关阀到位/超时（异常路径）
                               │ reset(L2)                ▲
                               └──────────► IDLE          │ 人工放行(L2双人)后可重开阀
```

要点：
- 输出由状态决定并在每次扫描时重_assert（防被人旁路改阀位）；
- 正常停机与异常回退共用"先停机、确认、再关阀"的顺序；
- 进入 FAULT_LOCKED 时联动全局 `Interlock.trip`，健康检查可见。

## 5. 权限分级

| 级别 | 角色 | 权限 |
|---|---|---|
| L0 VIEWER | 观察 | 只读状态/审计 |
| L1 OPERATOR | 操作员 | 正常顺控启停 |
| L2 SUPERVISOR | 值班长/工程师 | 人工放行（双人）、故障复位 |
| L3 ADMIN | 仪表/管理员 | 联锁参数整定 |

每次越权尝试都以 `AUTH ... DENY` 写入审计链；HTTP 侧返回 403。

## 6. 留痕与追溯

审计链（`sequence:trail`，只追加）每条记录：

```
id, time, comp_id, trace_id, category(COMMAND/STATE/FEEDBACK/ALARM/OVERRIDE/PARAM/AUTH),
actor_uid, actor_name, actor_level, action, detail(判定依据: 计时值/阈值/前后状态)
```

- **trace_id**：每次启动流程铸造一个，命令→回讯→状态迁移→异常→回退→闭锁全链同号，
  异常链路可整体回放；
- **按操作人追溯**：`POST /sequence/trail {"actor": "u-op"}`；也可按 `trace`、
  `category`、`comp` 过滤；
- 参数整定记录旧值→新值与操作人。

## 7. 运维接口

| 路由 | 说明 |
|---|---|
| `POST /sequence/start` `/stop` | 联锁启停（body 带 uid/name/level） |
| `POST /sequence/feedback` | 现场触点写入：valve_open / valve_closed / comp_run |
| `POST /sequence/tick` | 扫描推进（定时器与状态迁移），可挂周期任务 |
| `POST /sequence/release` | 人工放行（双人 + reason + field_confirmed） |
| `POST /sequence/reset` | 闭锁复位（L2） |
| `POST /sequence/param` | 参数整定（L3） |
| `POST /sequence/trail` | 审计链查询（按操作人/链路/类别） |
| `GET /sequence` | 全部控制器状态与当前参数 |

## 8. 落地注意

1. **硬线兜底**：软件联锁之外，开阀到位触点建议串入压缩机启动回路（硬线联锁），
   停机回讯超时联动硬线停机；本系统的超时推进只是软件层的最后手段。
2. 回讯触点本身故障（本次抖动场景）应有维修闭环：复位前需现场确认并消除传感器故障。
3. 定期传动试验：按班次/月度模拟回讯缺失，验证分级响应与回退顺序。
4. 旧的 `/valve/start`（无回讯确认）仅保留兼容，生产启停应走 `/sequence/start`。
