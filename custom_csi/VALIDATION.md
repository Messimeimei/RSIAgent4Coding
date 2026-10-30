# CSI 配置验证记录

更新日期：2026-10-22（Asia/Shanghai）。

当前双模型、BRS 波次配置的相关本地回归测试已通过 **41 项**。尚无本次配置在 H20 上完成模型接口探测、无模型 smoke 和 baseline/RSI 全流程的验证记录。下面分别记录当前验证状态与旧配置的历史结果。

## 当前配置与验证状态

| 项目 | 当前配置或结果 | 验证范围 |
|---|---|---|
| 模型分工 | Actor 为 DeepSeek-V4-Pro；Verifier、Curriculum、视觉读取为 Qwen3.8-Max | 已核对路由与实验锁，并通过相关本地测试 |
| 评测条件 | 评测器为 Qwen3.8-Max，用户模拟器为 Qwen3.8-Flash；baseline 与 RSI 使用一致条件 | 已通过一致性与配置漂移拒绝测试 |
| BRS | 启用波次，共享波前记忆快照；默认并发 1，可选 1–4；启用已验证分支的 VM 释放 | 已通过配置与参数传递测试，尚未进行本次配置的真实 VM 验证 |
| 请求预算 | 两个 Agent 模型的路由上限均为 65,536；严格保留请求参数，截断时抛错 | 已通过模拟接口测试，尚未验证真实服务承载能力 |
| 默认入口与 task_089 入口 | 默认任务分别为 task_080 和 task_089 | dry-run 测试通过，无模型调用或 VM 启动 |
| Python 3.12 正式环境 | 本次回归测试使用 Python 3.10.12 和临时依赖 | 尚未在正式 Python 3.12 环境复跑这组测试 |
| H20 的 ffprobe、内核模块、固定 Docker 镜像 | 后续运行所需前置条件 | 本次文档更新未完成这些项目的验收 |
| H20 无模型 smoke | 入口为 `custom_csi/smoke_runtime.py` | 已运行但失败；桌面与 KVM 正常，宿主机截图接口超时，详见下方证据 |
| 当前路由的真实模型探测 | 要求长上下文、JSON、图片及实际请求预算检查 | 路由仍标为 `requires_current_route_probe` |
| 当前配置的 baseline / RSI 全流程 | 尚无新的可比较得分 | 旧配置分数不能作为本次运行结果 |

最初同步文档时，上传副本中不存在 `results/` 目录。目前已有本机启动失败记录，
但本文件后面的旧配置历史路径、哈希和得分仍仅为旧文档记载，未重新核验。

## 2026-10-22 无模型 smoke 失败分析

16:37、16:47（北京时间）保存的两次失败记录属于同一次 smoke 的两轮启动：

- `results/boot_failures/20261022T083712Z_58d861a2d8864894bdf789b57797e7c8/`
- `results/boot_failures/20261022T084737Z_aac72ada56d341a3a561fe243373bbb4/`

两轮各等待 600 秒。两张 `boot-screen.png` 都显示已登录的 Ubuntu 桌面；
QEMU 报告 `VM status: running`、`kvm support: enabled`；容器未报告 OOM。
宿主机的三种访问方式（继承代理的 localhost、绕过代理的 localhost 和
127.0.0.1）访问 5000 截图接口均为 `ReadTimeout`。

因此已确认失败位于截图控制接口就绪检查，发生在任务设置、网络隔离安装和
模型调用之前。日志最后停在 EFI 加载信息不能证明系统卡在 EFI。
现有证据尚未区分容器转发故障、VM 内控制服务未监听或截图请求处理阻塞。
失败容器及其临时卷已清理。

新增诊断会在清理前从容器直连默认 VM 地址 `20.20.20.21:5000`，分别访问
根路径与截图接口，并采集 DHCP 租约、邻居表、转发规则和计数器。
若直连截图返回 200 而宿主机仍超时，应重点检查宿主机到容器再到 VM 的转发；
若根路径能返回 HTTP 而截图超时，应检查 VM 内截图处理；若二者均失败，
还需结合租约和网络规则判断 VM 地址、连通性与服务监听情况。
这些是取证改进，尚未构成运行故障已修复的证明。

## 已完成的本地测试

2026-10-23：完成统一路由配置的回归验证，**85 项测试通过**。覆盖
`test_custom_csi_runner.py`、`test_llm_route_file.py`、
`test_custom_evaluator_transport.py`、`test_runtime_paths.py`、
`test_usersim_runtime.py` 和 `test_recursive_improvement_framework.py`。
使用系统 Python 3.10 的临时虚拟环境，通过 `.pth` 加载已有离线测试依赖，
使测试子进程也能找到 `openai` 等依赖。

新增检查确认：只改路由即可切换四类模型；两个入口默认共用路由；baseline
和 RSI 使用自动生成的锁；源配置在启动后变化不影响当前运行；角色配置哈希、
非路由实验条件及 Actor/Verifier 模型分离检查仍生效。两个入口的 `--dry-run`
均通过。本次未调用真实模型，也未启动 VM。

2026-10-22 在 H20 使用系统 Python 3.10.12 和已有临时依赖目录执行：

```bash
cd /data/k8s/yx/RSIAgent
PYTHONPATH=/tmp/rsi-flow-test-deps-20261022:/data/k8s/yx/RSIAgent \
PYTHONDONTWRITEBYTECODE=1 \
python3 -m pytest -q -p no:cacheprovider \
  tests/test_custom_csi_runner.py \
  tests/test_recursive_improvement_framework.py \
  tests/test_llm_route_file.py \
  tests/test_custom_evaluator_transport.py
```

结果：

```text
41 passed in 0.89s
```

临时依赖目录是当时已有的测试环境，不是正式安装方式。覆盖的内容包括：

- 波次模式、并发容量、分支释放参数和非法组合校验。
- baseline/RSI 的模型、配置哈希、评测器、用户模拟器等一致性检查。
- 默认入口和 task_089 入口的 dry-run。
- 模型请求重试时保留参数、拒绝把截断内容当作正常回复。
- 探测使用指定请求预算，以及过期或缺项探测记录的拒绝逻辑。
- 封闭评测器的凭据选择、最小请求预算及 Phase 3 实验锁传递。

模型响应和 VM 调度相关测试使用模拟对象。这 41 项通过不等于真实接口可用、Docker/KVM 可用，或完整任务已经成功。

## 当前配置仍需完成的验证

按 [运行说明](README.md) 完成以下步骤，并保存新的输出：

1. 验收正式 Python 3.12 环境中的 OSWorld 与 RSIAgent 依赖，并复跑相关测试。
2. 准备 ffprobe、宿主机内核模块和 `runtime.json` 指定的固定镜像。
3. 执行无模型 smoke，保存 `results/smoke/` 下的新记录。该步骤不调用模型或官方评测器。
4. 对将要使用的配置执行真实模型探测。Actor 文本和 Verifier JSON 请求均使用 65,536 输出预算，图片请求使用 32,768；记录应包含当前路由的 SHA-256。
5. 使用新运行名执行 baseline 与 RSI，检查 Phase 3 正式评分及匹配的运行配置。

模型探测只要求短格式回复，因此即使成功，也仅说明服务接受该请求和上下文并返回正确格式，不能证明持续生成 65,536 token 的能力。路由中 `validation` 字段的静态状态不会因探测成功自动改写；成功记录保存在 `probe.json`，运行器会校验该记录。

新结果应同时记录任务、种子、路由与锁文件、探测及 smoke 记录。当前实验是同一任务上的目标条件适应，不能据此得出未见任务泛化结论。

## 旧配置的历史结果

以下内容来自修改前的验证文档，描述的是单模型 Qwen3.7-Plus、8,192 输出上限的旧配置及其早期尝试。它们不是当前双模型、65,536 上限和恢复波次模式后的验收结果。

旧文档记载，接口适配、模型映射、Docker/QEMU、短文本、JSON、图片、流式长上下文探测和控制器上传 smoke 曾通过。非流式 Agent 请求约含 3 万至 4 万输入 token 时，GLM-5.3、GLM-5.3-Flash、GLM-5.2 和 Qwen3.7-Plus 多次超过接口空闲连接窗口。切换为流式 Qwen3.7-Plus 后，baseline 的 Actor 与 Verifier 流程完成。

第一次 RSI Phase 1 启动在调用模型或执行项目之前停止，原因是旧语料构建器动态加载时只取得 108 条任务中的 68 条。旧记录称，改为静态任务说明后，重建的 108 条语料通过了来源校验。

历史运行 `csi_task080_streamrsi_01_task_080_rsi` 记录如下：

| 阶段 | 旧文档记载 |
|---|---|
| baseline | task_080 的官方任务得分为 `0.05` |
| Phase 1 | 在预算 8 个项目中完成 6 个后饱和；官方评测调用为 0；冻结记忆 7,730 字节 |
| Phase 2 | 完成 5 个目标循环、4 次演化和 8 个练习项目；Verifier 最终为 `PASS`；官方评测调用为 0；冻结 3 个记忆文件，共 18,947 字节 |
| Phase 3 | 只读复制冻结记忆，关闭 Curriculum 和记忆更新；成功调用封闭官方评测器 1 次；任务得分为 `0.05` |
| 对照差值 | `0.05 - 0.05 = 0.00`，仅属于该历史运行 |

旧文档记录的记忆树 SHA-256：

- Phase 1：`07d9d9e1bc70f4c80f5c119cb04871a9ecfe10728f63717113ace3e424eaa67d`。
- Phase 2：`01cf7c7cfdd23fe70072cbe97824aa4d2cb46ada7bd113f14341cb0639304cea`。

该历史运行中，两个论文模型身份都映射到 Qwen3.7-Plus，且开发与最终评测任务均为 task_080。历史分数既不代表论文原模型的得分，也不证明跨任务泛化。

## 历史评分问题

旧文档报告，候选 XLSX 使用合法的 OOXML 绝对关系目标，例如 `/xl/worksheets/sheet1.xml`；当时任务评测器又无条件添加 `xl/`，最终查找 `xl/xl/worksheets/sheet1.xml`。这使单元格、图表和样式被解析为空，业务、公式结构、图表和格式得分均为零，整个 `0.05` 来自工作簿完整性。

同一记录称，重算副本仍匹配 116 项数值检查中的 74 项，因此不能把 `0.05` 理解为“工作簿没有变化”。另有一个独立的公式问题：Actor 和 Verifier 接受了 `H=E*F*G`，但 `G` 已含 ramp，封闭评测定义要求 `H=E*G`。

以上诊断未在当前 H20 副本中复核。本轮请求参数、模型分工和波次修正也不能作为这些历史评分问题已经修复的证据。

## 历史证据索引

以下路径由旧文档保留；当前副本没有这些旧运行的原始产物。本机新产生的
`results/boot_failures/` 不能替代旧运行证据。

早期失败尝试和未评分的部分 Actor 产物：

- `results/custom_csi/csi_task080_01/`
- `results/custom_csi/csi_task080_flash_01/`
- `results/custom_csi/csi_task080_capped_01/`
- `results/custom_csi/csi_task080_low_01/`
- `results/custom_csi/csi_task080_low16k_01/`
- `results/custom_csi/csi_task080_low12k_01/`
- `results/custom_csi/csi_task080_glm52_01/`
- `results/custom_csi/csi_task080_final_01/`
- `results/custom_csi/csi_task080_qwen_01/`

旧文档记载的完整 baseline 与 RSI 产物：

- `results/task_080/seed5400_csi_task080_streamctl_01_baseline/result.json`
- `results/custom_csi/csi_task080_streamrsi_01/task_080/`
- `results/recursive_improvement/csi_task080_streamrsi_01_task_080_rsi/`
- `results/task_080/seed93080_csi_task080_streamrsi_01_task_080_rsi_phase3/`

未完成或未评分的部分 baseline 产物不能与 RSI 正式得分比较。重新运行应使用新名称，不覆盖原始证据目录。
