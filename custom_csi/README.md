# CSI 接口与 OSWorld 运行说明

更新日期：2026-10-23（Asia/Shanghai）。

本目录通过兼容 OpenAI API 的 CSI 接口运行 RSIAgent。当前配置使用不同模型承担 Actor 与 Verifier，采用“广泛探索 → 针对性深化 → 冻结记忆评测”的流程。已完成的代码验证和旧配置的历史结果见 [验证记录](VALIDATION.md)。

## 当前模型分工

协议保留原有的规范模型标识，在发送请求时映射到实际模型。默认配置与 `task_089` 专用配置目前采用以下分工：

| 角色 | 协议模型标识 | 实际调用模型 |
|---|---|---|
| Actor、目标任务执行、记忆写入 | `z-ai/glm-5.3` | `DeepSeek-V4-Pro` |
| Verifier、Curriculum、视觉读取、升级后的 Actor | `moonshotai/kimi-k3` | `Qwen3.8-Max` |
| 升级后的 Verifier | `z-ai/glm-5.3` | `DeepSeek-V4-Pro` |
| 封闭评测器 | `role_routes.sealed_evaluator.wire_model` | `Qwen3.8-Max` |
| 用户模拟器 | `role_routes.user_simulator.wire_model` | `Qwen3.8-Flash` |

## 修改模型：只编辑 route.json

默认入口和 `task_089/run.py` 现在共用 **`custom_csi/route.json`**：

- `model_map["z-ai/glm-5.3"]`：Actor、目标执行、记忆写入、升级后的 Verifier。
- `model_map["moonshotai/kimi-k3"]`：常规 Verifier、Curriculum、委托视觉读取、升级后的 Actor。
- `role_routes.sealed_evaluator.wire_model`：封闭评测器。
- `role_routes.user_simulator.wire_model`：用户模拟器。
- `base_url` 和 `api_key_env`：上述所有角色共用的接口地址和凭据环境变量名。

Agent 的 `canonical_model` 保持不变；其实际模型统一从 `model_map` 推导，
不需要再在 `role_routes` 重复填写 `wire_model`。兼容旧路由文件时，Agent 条目
残留的 `wire_model` 会被忽略，以 `model_map` 为准。评测器和用户模拟器的
`wire_model` 则直接生效。

每次新运行会把路由快照和自动生成的两份实验锁保存到
`results/custom_csi/<name>_<task>_<arm>/config/`。每个任务、每个实验分支有独立目录；
`--arm both` 分别生成 `_baseline` 和 `_rsi` 目录，两边使用相同的源路由和角色配置。
**换模型无需手动修改锁文件，也无需运行 `prepare_locks.py`。**
**修改 Actor、Verifier 或升级后 Verifier 的 YAML 配置后，也无需手动更新哈希。**
启动器会根据当前文件自动计算哈希，写入本次运行的两份实验锁，不修改源锁模板。
源锁模板仍须声明一致的 baseline/RSI 配置路径及原始哈希；模型分工、任务发布版本、
记忆策略等一致性检查继续生效。本次锁一旦保存便保持固定，后续阶段仍会检查配置是否
漂移；请在启动新实验前修改 YAML。修改源路由只影响下次启动，已启动的运行使用自己的路由快照。

执行 `python3 custom_csi/run.py --dry-run` 可查看四类实际模型和展开后的角色映射，
不创建运行文件，也不调用接口。通过 CSI 入口启动时，上述自动配置才会生效；
直接调用底层 benchmark 模块时仍需显式指定路由和运行锁。

如需为某次运行选择另一份路由，可传 `--route-file <路径>`。保留的
`custom_csi/task_089/route.json` 仅在显式选择时使用，不再由 task_089 入口自动加载。

这是替换模型后的结构复现，分数不能直接当作论文原模型的复现分数。当前接口仍使用流式响应；推理控制参数和推理历史传递均未启用，因此不能声称请求行为与论文原接口完全一致。

两个 Agent 模型的路由输出上限均为 **65,536 token**。实际请求预算由角色配置决定，并受该上限约束；这不表示每次调用都要求生成 65,536 token。当前另有两项约束：

- `strict_request_parameters=true`：请求失败重试时保留已发送的参数，不自动删掉 JSON 格式或采样参数。
- `fail_on_truncation=true`：仍然截断的响应会抛错，不作为正常 Agent 回复返回。达到路由上限后，不通过重复发送相同预算来恢复。

封闭评测器沿用 512 token 的最小请求输出预算。该设置不保证实际可见输出长度；用户模拟器的预算由实验锁单独指定。

## BRS 波次与对照条件

Phase 1 已恢复 `parallel_waves=true`。同一波次的项目读取同一份波次开始前的记忆快照，同波全部分支结束后才按顺序提交记忆。Curriculum 决定波次内容和宽度，`--parallelism` 只控制同时执行的容量。

默认并发数为 1，可通过 `--parallelism 1–4` 调整。单并发仍保留波次快照和提交屏障。`park_verified_branches=true` 会释放已验证分支的 VM，保留候选状态，在轮到该分支提交时恢复，从而减少同时驻留的 VM。

默认项目预算为 8，可通过 `--project-budget` 调整；Curriculum 可以提前结束。Phase 2 继续深化当前任务，Phase 3 使用冻结记忆评测。每个任务都有独立的协议与记忆目录。

启动前，运行器会检查 baseline 与 RSI 的 Actor/Verifier 模型、配置哈希、升级切换、评测器、用户模拟器、任务资产及任务发布版本是否一致，并要求：

- Actor 与 Verifier 使用不同的实际模型。
- baseline 与 Phase 3 使用同一目标执行配置。
- baseline 禁用记忆；Phase 3 只复制冻结记忆，不回写宿主机记忆。

当前协议为 `target_conditioned_adaptation`：Phase 2 和 Phase 3 使用同一个任务。即使字段名为 `held_out_tasks`，也不能据此宣称验证了未见任务泛化。

## 凭据与运行环境

以下命令均从 RSIAgent 根目录执行：

```bash
cd /data/k8s/yx/RSIAgent
```

将 `custom_csi/.env.example` 复制为本地 `custom_csi/.env`，填入：

```text
CSI_API_KEY=...
```

也可以直接设置同名环境变量。凭据不应写入路由、协议、实验锁或命令行参数。当前 CSI 地址使用 HTTP，传输不会由 TLS 加密。

正式运行使用 `../OSWorld-V2/.venv/bin/python`，需要 Python 3.12、OSWorld 和 RSIAgent 依赖，以及已准备的 VM 磁盘、任务资源、Docker/KVM 和 `ffprobe`。先确认 Python 环境：

```bash
../OSWorld-V2/.venv/bin/python --version
../OSWorld-V2/.venv/bin/python -c \
  'import docker, PIL, numpy; print("python environment ready")'
```

练习环境的网络隔离还依赖宿主机上的 `ebtables`、`ebtable_filter`、`ebt_ip6` 模块，以及 `runtime.json` 固定的外层 Docker 镜像。该要求适用于 H20/Linux，也适用于具备对应内核支持的 WSL2。

需要构建镜像时，使用固定基础镜像摘要和软件包版本的 Dockerfile：

```bash
docker build --pull=false \
  -t rsiagent/osworld-csi-ebtables-legacy:2.0.11-6 \
  custom_csi/docker
sudo modprobe -a ebtables ebtable_filter ebt_ip6
```

也可以加载已有镜像。实际运行依据 `runtime.json` 的 `outer_image` 镜像 ID，而不是上述标签；须确认本机存在该镜像。重新构建后若 ID 不同，应核对构建内容，再更新该字段。提供器只接受固定镜像 ID 或摘要。Verifier 文件恢复使用 OSWorld 控制器上传路径。

运行无模型 smoke：

```bash
../OSWorld-V2/.venv/bin/python custom_csi/smoke_runtime.py
```

该命令启动临时空任务 VM、安装网络隔离并检查通信边界，成功后向 `results/smoke/` 写入记录，再清理 VM。它不调用模型或官方评测器；通过也不表示模型接口已经验证。

若出现 `VM failed to become ready within timeout period`，表示宿主机的
`/screenshot` 检查未通过，不一定表示 Ubuntu 没有启动。2026-10-22 的两次
失败画面已显示桌面，QEMU/KVM 正常，但宿主机访问 5000 接口均超时。
具体证据与尚未确定的原因见 [验证记录](VALIDATION.md)。

可先运行一次短诊断，等待截图接口约 120 秒，再采集证据；不自动重试第二次：

```bash
../OSWorld-V2/.venv/bin/python -u custom_csi/smoke_runtime.py \
  --boot-timeout 120 --boot-attempts 1
```

失败记录保存在 `results/boot_failures/`。除启动画面外，`diagnostics.json`
现在还包含容器内直连 `20.20.20.21:5000` 的结果、DHCP 租约和转发规则，
用来区分宿主机端口转发与 VM 内服务问题。容器里的 `ss` 没有列出 5000
不能单独证明服务没启动，因为服务位于 VM 内，容器通过 NAT 转发。
短超时只用于取证，不能据此断言启动耗时异常；默认仍为 600 秒、最多两次。

## 运行命令

先查看计划，不调用模型、不启动 VM，也不要求 OSWorld 虚拟环境或 API 凭据：

```bash
python3 custom_csi/run.py --dry-run
python3 custom_csi/task_089/run.py --dry-run
```

默认入口的默认任务是 `task_080`，专用入口的默认任务是 `task_089`。两者都支持显式传入 `--tasks`。

仅探测当前模型接口：

```bash
../OSWorld-V2/.venv/bin/python custom_csi/run.py \
  --arm probe --name csi_probe_20261022_01
```

探测会实际调用模型：分别以长上下文和 65,536 的请求输出预算检查 Actor 文本响应、Verifier JSON 响应，再以 32,768 预算检查图片输入。它验证请求接受能力与返回格式，不证明能够持续生成到上限，也不证明完整 Agent 任务已跑通。

按顺序运行 task_080 的 baseline 与 RSI：

```bash
../OSWorld-V2/.venv/bin/python custom_csi/run.py \
  --arm both --tasks task_080 --name csi_task080_20261022_01 \
  --project-budget 8 --parallelism 1
```

使用 task_089 专用配置：

```bash
../OSWorld-V2/.venv/bin/python custom_csi/task_089/run.py \
  --arm both --name csi_task089_20261022_01 --parallelism 1
```

正常运行会先执行探测。复用已有探测时，使用同一配置下成功生成的记录：

```bash
../OSWorld-V2/.venv/bin/python custom_csi/run.py \
  --arm both --tasks task_080 --name csi_task080_20261022_02 \
  --skip-probe \
  --probe-receipt results/custom_csi/csi_probe_20261022_01_task_080_probe/probe.json
```

运行器检查记录状态、当前路由文件的 SHA-256，以及长上下文、JSON、图片和主要输出预算检查项。旧的小预算探测记录不能用于本次配置。代码提供的 `--allow-unstable-route` 与 `--skip-probe` 一起使用，可显式绕过未提供探测记录时的检查，但这种运行不能标为已通过接口验收。

批量任务可写为 `--tasks task_001 task_002`；`--tasks all` 选择发布版的 108 个任务。
任务之间顺序执行，每个任务的 Phase 1 内部容量由 `--parallelism` 控制。
目录名统一为 `<name>_<task>_<arm>`，`--arm both` 会展开为 baseline 和 RSI 两个分支；
`--arm probe` 以 `_probe` 结尾。同一次启动只做一次模型接口探测，其余目录保存复用记录。
可以用相同的 `--name` 分别启动 baseline 和 RSI，已有同任务、同分支的输出仍会被拒绝覆盖。
建议 `--name` 只写日期或批次，例如 `20261030_01`；程序不会去重名称中已有的任务名或分支名。

## 结果位置与比较方法

| 内容 | 路径 |
|---|---|
| RSI 运行计划、探测记录、配置与分阶段日志 | `results/custom_csi/<name>_task_NNN_rsi/` |
| baseline 运行计划、探测记录、配置与日志 | `results/custom_csi/<name>_task_NNN_baseline/` |
| baseline 轨迹与任务结果 | `results/task_NNN/seed5400_<name>_task_NNN_baseline/result.json` |
| RSI 分阶段结果与冻结记忆 | `results/recursive_improvement/<name>_task_NNN_rsi/` |
| Phase 3 轨迹与任务结果 | `results/task_NNN/seed<seed>_<name>_task_NNN_rsi_phase3/result.json` |
| Phase 3 汇总 | 上述 RSI 目录下的 `phase3/result.json` |

配置、日志、缓存和 RSI 的 `protocol.json` 直接放在各自运行目录中，无额外的任务子目录。
`--dry-run` 的 `runs` 列表会列出每个任务、分支的目录及配置锁路径。
以上规则用于新运行；已有目录和其协议中的路径不自动迁移。

比较 baseline 的 `result.json.score` 与对应 Phase 3 的 `task_results[0].score`。Phase 2 的 Verifier `PASS` 是学习过程中的验证结论，不是官方任务得分。

当前实际通过了哪些检查、哪些仍待验证，以及旧版 `0.05` 分数的适用范围，见 [验证记录](VALIDATION.md)。
