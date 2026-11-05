# 大计算看门狗（`hydrodynamics.run_watchdog`）

看门狗是一个独立进程，监视 `hydrodynamics.large_run_driver` 的运行目录。求解器卡住时，它会写告警，并默认停止计算。它不改动求解器和驱动，也可以接管已经在运行的计算。

```
python -X utf8 -m hydrodynamics.run_watchdog <运行目录> [--action stop|alert] [--once] [阈值参数]
```

运行目录就是驱动的 `output.directory`，或者 `<output.root>/<方案ID>`，里面有 `progress.jsonl` 和 `run_report.json`。看门狗只读取最后一个 `start` 事件之后的记录；`start` 事件里有驱动的进程号。

## 检查项

| 告警原因 | 条件 |
|---|---|
| `dt_collapse` | 最近 `dt_window` 条 `solver_progress` 的时间步中位数小于 `min_dt_s` |
| `slow_progress` | 最近 `speedup_window_min` 分钟内，模拟秒数除以墙钟秒数小于 `min_speedup` |
| `stale` | 求解阶段超过 `stale_min` 分钟没有新的进度行；求解结束后的关闭、导出、校验阶段阈值改为 `stale_after_solver_min` |
| `process_died` | `run_report.json` 仍是运行中，但驱动进程已不存在 |

`start` 事件之后的 `grace_min` 分钟内不检查（网格构建期间没有进度）。`run_report.json` 已是 `complete` 或 `failed` 时，看门狗正常退出。

## 参数

| 参数 | 类型 / 单位 | 默认 | 合法范围 |
|---|---|---|---|
| `--action` | 枚举 | `stop` | `stop`：停止驱动进程树，把 `run_report.json` 标为 `failed`（`failed_stage: watchdog`）；`alert`：只写告警 |
| `--min-dt-s` | 数值，s | 0.005 | (0, 60] |
| `--dt-window` | 整数，条 | 5 | [1, 1000] |
| `--min-speedup` | 数值，模拟秒/墙钟秒 | 2.0 | [0, 1e6]，0 表示关闭该项 |
| `--speedup-window-min` | 数值，min | 30 | (0, 1440] |
| `--stale-min` | 数值，min | 30 | (0, 1440] |
| `--stale-after-solver-min` | 数值，min | 240 | (0, 10080] |
| `--grace-min` | 数值，min | 10 | [0, 1440] |
| `--poll-s` | 数值，s | 60 | [1, 3600] |
| `--once` | 开关 | 否 | 只检查一次；退出码 0 = 正常/已结束/等待，2 = 告警，1 = 参数或目录无效 |

参数越界时给出字段名和合法范围，退出码 1。

## 输出

- `watchdog.jsonl`：每次检查追加一行，含状态、原因和指标（时间步中位数、推进倍速、空闲分钟数、进程号）。
- `WATCHDOG_ALERT.json`（`large-run-watchdog-alert-v1`）：告警原因、详情、指标、是否已停止进程、保留的检查点和续算命令。

检查点从不删除。排除原因后用 `large_run_driver <配置> --resume` 续算；方案 ID 是自动生成的，还要加 `--id <方案ID>`。

## 背景

2026-11-04 的 288 h 计算在第 18 h 把时间步压到约 2.6e-4 s。原因是自由出流的堰顶低于出口面床面，薄水层被算出了有限水头。这个问题已在 `598ea14` 修正，但当时没有任何机制发现，空转了一整夜。看门狗按默认阈值会在约 5 分钟内以 `dt_collapse` 停掉这类运行。
