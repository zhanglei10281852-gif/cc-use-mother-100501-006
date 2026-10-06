# 多式联运异常重排

县域生鲜货物经干线铁路、区域仓与末端冷链车送达门店的全链路控制塔服务。系统用具有**容量、时窗和温控能力**的运输班次组合货运承诺；包装单元（托盘）拆分后保持**连续保管关系**；装卸、交接、拒收、温度偏差事件**重复或乱序到达也能归入正确运输段**；发生枢纽封闭、班次延误、容量取消或部分损坏时，系统在**不破坏已完成路段**的前提下比较候选改线、冻结选定方案，并同步调整剩余预约、费用与承诺时间。

运营人员可以通过 HTTP API 或命令行回答四个问题：**货物现在在哪、为什么走这条路线、哪些承诺已失效、赔付责任落在哪一段。**

## 运行环境

- Python 3.11 或更高版本，仅使用标准库，无外部服务依赖
- 状态持久化到本地 JSON 文件（独占锁 + 原子替换），服务重启后自动继续处理未交接货物

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q src tests run_cli.py
```

## 命令行冒烟

```bash
python3 run_cli.py
```

在临时目录运行完整演示（登记班次与货物 → 在途事件 → 容量取消自动改线 → 部分损坏定责 → 部分签收），打印货物状态摘要。

## 命令行用法

```bash
PYTHONPATH=src python3 -m freight_replanning.cli --data-dir ./data demo          # 端到端演示
PYTHONPATH=src python3 -m freight_replanning.cli --data-dir ./data serve --port 8080
PYTHONPATH=src python3 -m freight_replanning.cli --data-dir ./data status SH-001 # 在哪/为什么/承诺/责任
PYTHONPATH=src python3 -m freight_replanning.cli --data-dir ./data unit SH-001-U001  # 保管链
PYTHONPATH=src python3 -m freight_replanning.cli --data-dir ./data recover       # 重启恢复扫描
```

其余子命令：`add-leg`、`add-shipment`、`event`、`disrupt`、`replans`、`choose`（人工强制选择，必须 `--reason`）、`refresh`、`legs`、`shipments`，详见 `--help`。

## HTTP API

`serve` 子命令启动后提供 JSON API：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/legs` | 注册运输班次（容量/时窗/温控/费率） |
| POST | `/shipments` | 登记货物，自动组合初始路线、预约与承诺 |
| GET | `/shipments/{code}` | 在哪、路线原因、承诺、费用、赔付责任、重排历史 |
| GET | `/shipments/{code}/liability` | 赔付责任清单 |
| GET | `/units/{code}` | 包装单元保管链与事件（含挂起标记） |
| POST | `/events` | 上报事件（`event_id` 幂等，乱序自动归段） |
| POST | `/disruptions` | 上报异常并触发重排（`hub_closed` / `leg_delayed` / `capacity_cancelled` / `partial_damage`） |
| GET | `/replans?shipment=` / `/replans/{id}` | 重排记录与候选方案 |
| POST | `/replans/{id}/choose` | 人工强制选择候选（必须填 `reason`，全程留痕） |
| POST | `/replans/{id}/refresh` | 补充运力后重新搜索未冻结的重排 |
| POST | `/recover` | 重启恢复：刷新派生状态，重试挂起事件，列出未交接货物 |

## 设计要点

- **事件归段**：每个包装单元的事件按 `occurred_at` 重放重建保管链；重复事件按 `event_id` 幂等丢弃，缺前序的乱序事件先挂起、补齐后自动归段，相互矛盾的事件挂起待人工处理而不污染保管链。
- **不破坏已完成路段**：重排只释放未履行的预约，已完成班次的预约保持 `fulfilled` 只读；在途单元不扯下车辆，到达枢纽后若无续程预约会自动触发重排。
- **并发安全**：所有修改在文件独占锁内完成"读取-修改-写入"，占位前重新校验容量与"同一单元不得时间重叠占位"，并发重排不会把同一托盘分给两辆车。
- **整票不被单点失败覆盖**：货物状态由包装单元状态聚合，部分签收 + 部分拒收呈现为 `partially_delivered`。
- **赔付责任**：损坏/拒收/温度偏差按发生时刻所在的保管段归责（拒收归到最后承运班次），随货物状态查询一并输出。
