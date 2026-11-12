# DeepSWE 两端运行问题与方法局限核对

核查日期：2026-11-12，Asia/Shanghai。只读检查本地与 H20 的运行结果、阶段事件、检查点、隔离记录、记忆清单和实现；未启动、停止或修改任何运行任务。

## 1. 范围与结论

主结果范围为已有官方 `verifier/reward.json` 的 RSI Phase 3，包括完整作业内的 Phase 3 和独立加载记忆的重跑。共核到本地 9 次、H20 5 次，覆盖 13 个不同任务；DynamoDB 有两次评分。这个清单不是匹配预算、模型和协议的排行榜。

尚未完成 Phase 3 的运行、仅 baseline/native 的结果、取消的评估包，不纳入主结果表。为解释同一任务如何走到重跑结果，另核查其前序失败运行。没有评分的基础设施失败，不写成“官方功能测试得 0 分”；汇总文件里的 `mean: 0` 可能只是错误占位。

用户列出的七类运行问题基本成立，但缓存、Hypothesis、Git 索引应归为同一大类下的不同子问题。遗漏主要是：报告交接协议/接收竞态、权限、原生执行轮数、上下文策略差异，以及部分训练和重跑的实验口径。

“内部检查过严”主要是移植实现问题，不宜作为论文方法的固有缺点。“无限探索”和“跨任务不可迁移”都需要收窄措辞。

## 2. 已有 Phase 3 官方结果

下表 F2P 表示新增修复项，P2P 表示原有回归项。表中各次正式评分的 P2P 均全部通过。所有列出的 Phase 3，其内部 Verifier 最终均判 PASS；官方未全过的五项显示了内部判定与评分之间的差距。

| 位置 | 任务 / 运行标识 | F2P | P2P | Reward | 训练或重跑范围 |
| --- | --- | --- | --- | --- | --- |
| 本地 | participle，20261111-01 | 91/91 | 153/153 | 1 | BRS/DRS 均有隔离，training_status=partial |
| 本地 | prometheus，20261111-02 | 10/15 | 82/82 | 0 | 重启版；training_status=completed；Phase 1 达完整波次预算边界 |
| 本地 | scriggo，20261111-01 | 48/48 | 1049/1049 | 1 | partial |
| 本地 | sql-formatter，20261111-01 | 26/26 | 5709/5709 | 1 | partial |
| 本地 | superjson，20261110-01 | 80/80 | 116/116 | 1 | partial |
| 本地 | tomlkit，20261110-01 | 60/60 | 964/964 | 1 | partial |
| 本地 | wasmi，20261110-01 | 20/22 | 58/58 | 0 | partial |
| 本地 | oxvg，strict/20261110 | 4/6 | 62/62 | 0 | 单独 Phase 3；仅原始部分 BRS 的 8 个记忆文件 |
| 本地 | vitest，strict/20261110 | 56/56 | 24/24 | 1 | 单独 Phase 3；源银行为空，0 个记忆文件，属空记忆对照 |
| H20 | dateutil，phase3-20261108-01 | 65/67 | 2035/2035 | 0 | DRS cycle_005 异常后的已提交记忆，21 文件；最终执行预算 10200 秒 |
| H20 | dynamodb，phase3-20261109-01 | 37/37 | 1267/1267 | 1 | 已完成 BRS/DRS 的冻结记忆；原 Phase 3 交接失败后重跑；4h 工作预算 |
| H20 | dynamodb，phase3-batch-20261110-01 | 37/37 | 1267/1267 | 1 | 同一 23 文件银行再评估；5h 工作预算；不是另一个独立训练样本 |
| H20 | expr，phase3-batch-20261110-01 | 79/79 | 66265/66265 | 1 | 仅 Phase 1 冻结记忆，6 文件；未完成 DRS 收敛 |
| H20 | koota-pair，phase3-10h-20261111-01 | 33/38 | 172/172 | 0 | DRS 第二次目标尝试权限失败后，加载此前已提交的 19 文件银行 |

特别排除：本地 `phase3-replay/20261110-{vitest,oxvg}` 被取消，因包含人工补充记忆/提示，协议记录明确不用于原始冻结记忆方法结果。远端 `bitfun-dateutil-rsi-phase3-batch-20261110-01` 因报告权限失败，没有官方评分。

### 内部 PASS 漏检的具体项目

| 任务 | 官方未通过内容 |
| --- | --- |
| dateutil | 两个 rrule `repr` 重建测试，包括 `ByWeekday` 情形 |
| oxvg | 空组作为相邻兄弟选择器锚点、空组本身为父子选择器目标，两项保留行为 |
| wasmi | 捕获 `i32/i64` 和 `f32/f64` 局部变量，两项 core dump 测试 |
| prometheus | 状态端点及持久化状态文件、并发重载、部分应用失败后的回滚、多次重载不变量、成功后的状态/指标，五项测试 |
| koota-pair | pair/trait AND 组合、具体 pair 删除、保留其他 pair 时的删除、通配 Removed 和 Added 与 trait 语义一致性，五项测试 |

这些能证明内部验证存在假阳性；不能单凭这些结果证明某一份错误记忆是失分的直接原因。后者需要对具体记忆、检索轨迹和实现决定逐项追踪，或做记忆消融。

## 3. 对原七项运行问题的修正

1. **原生会话归档/恢复：成立，属适配层。** H20 dateutil `p2-01` 的阶段结果明确为 `Native session archive failed`。原始 RSIAgent 也有环境与私有 scratch 状态；不能把它概括成“只保存历史文本”。BitFun 需要恢复原生会话目录及其关联状态，保存聊天文本不能替代它。归档失败的实际 I/O、并发变动、复制错误与“需要归档什么”是两个层次，不应混成同一个根因。
2. **测试隐式写入引发完整性误报：成立，属适配层。** 要区分 Actor 原始提交、原始输入和一次性验证副本。pytest 的 pyc/pytest_cache、BitFun 的 `.bitfun/tmp/write_*.tmp`、ESLint 的 `.eslintcache`、构建输出，并不自动意味着原始候选被破坏。“Verifier 想删但被拒绝”不一定是每轮的实际过程；已核的多次记录是验证结束后的宿主机检查器拒绝了副本变化。原始候选最终字节一致也仍会被隔离。
3. **Hypothesis 辅助状态：成立，是第 2 项的子类。** `.hypothesis` 等状态也需要写到 scratch，而非把一切新增文件当候选破坏。测试缓存工具种类不断增加，逐个扩白名单容易继续漏项。
4. **Git 索引元数据：成立，是第 2 项的另一个子类。** Git 查询可能刷新索引的 stat 缓存，不等于暂存内容变化。原始候选索引恢复曾成功，但副本中的临时文件会触发“已有拒绝项，就不再判断索引是否仅元数据”的连带拒绝。旧修复生效与后续仍被隔离并不矛盾。
5. **异步工具状态与原执行观测格式不兼容：成立。** H20 dateutil `p2-06` 明确记录 `NoneType ... get`；保存流重放和适配文档将其对应到 `completion: null`、`exit_code: null`、有可继续进程 session 的工具返回。工具调用结束不等于底层命令退出。应跟踪运行态，不能算作成功，也不能当作执行异常或无进展。这里没有证据证明这是 BitFun 某天“新增”的能力，只能说当时适配器没有正确处理。
6. **Docker 网络分配冲突：成立，属运行环境适配。** H20 dateutil `p2-07`、`resumed-01` 有实际 IPv4 overlap。并发建环境的地址分配、重试、清理和全局锁都属于额外依赖。后续本地还出现 `Timed out waiting for RSI Docker network lock`，这是锁竞争，不等同于地址重叠；该新运行尚未出 Phase 3，不计入主结果表。
7. **长 prompt 命令行参数超限：成立，属传输适配。** H20 dateutil `p2-08` 完成首波 6 个项目后，第二波 126667 字节 prompt 经嵌套 shell 转义触发 `[Errno 7] Argument list too long: 'docker'`，发生在模型启动前。解决的是改用文件/stdin，不是增加模型窗口，也不是调整 native model-round 数。

## 4. 需要补充的实现与运行问题

### 4.1 报告交接有三类不同故障

- **报告内容/类型约束过强。** 本地 Vitest 20261110 的三份 FAIL 报告都已存在；`findings` 写成对象列表，适配层要求字符串列表，Pydantic 在 `findings.0`、`findings.1` 报 `string_type`。恢复提示只暴露 ValidationError，模型继续补内容却不改类型，耗尽恢复次数。这个 JSON schema 不是论文原报告接口要求。
- **已提交有效报告，控制器却没接收。** H20 DynamoDB 原 Phase 3 和 dateutil resumed-02 第五轮都留下三份 PASS 报告，却被标成 invalid_handoff。DynamoDB 记忆包 provenance 明确记载旧 session.py 在进程退出时没有真正重新读取校验报告、用文件存在性分类的接收缺陷。不能把所有 report recovery exhausted 都归咎于模型没写报告。
- **文件权限。** H20 koota-pair 第二次目标尝试有三次 `Invalid actor handoff (PermissionError)`；dateutil 20261110 Phase 3 批次也有六次 `Invalid verifier handoff (PermissionError)`。这是实际记录的底层类型；究竟哪一级目录或进程身份不匹配，现有脱敏异常没有给出完整路径，不能仅凭 mode 600/700 就断定具体 UID 根因。

这些底层错误最终都可能变成同一个 `RoleInfrastructureError`。总结时应保留底层类型与阶段，而不是只列外层名字。

### 4.2 Native 轮次上限与任务预算不是一回事

BitFun 在约 200/201 model rounds 后可以以 `max_rounds` 结束一段。它不表示任务完成或整个工作预算耗尽，需要同会话续跑。早期路径会与 missing-report recovery 混用，正常分段有耗尽协议修复次数的风险。DynamoDB 和多个后续真实运行的事件里有 `segment_exhausted` 与续跑记录。

### 4.3 会话连续不等于上下文策略等价

本次有分数的运行仍使用 native 上下文策略；本地相关 `reference_context_policy` 为 false，H20 重跑也未启用。BitFun 会自动压缩历史，恢复整个原生目录不能恢复压缩前模型实际看到的全部内容。

已检查到本地 Phase 3 的上下文压缩计数，例如 wasmi Actor 27、Verifier 16；scriggo Actor 54、Verifier 3；prometheus Actor 17、Verifier 8。H20 dateutil resumed-02 目标/Phase 3 结果记录中也有大量压缩。

此外，本地已用模拟请求复现：旧 BitFun 新会话初始窗口 128128，与配置 1048576 不一致；续跑才可能读取较大值。相关源码补丁和验证见 `docs/BITFUN_CONTEXT_POLICY.md`，但这不是“所有历史评分已使用修正版”的证据。提示词作为 BitFun 用户消息、处于其原生 system prompt 之下，也不同于参考实现的角色 system message。

这属于复现协议差异及证据保留风险；不能直接推断压缩导致了某个官方失败。

### 4.4 训练异常的后果和恢复范围

旧严格策略下，一支基础设施异常可以使整波未提交记忆丢失；后来本地采用隔离后部分继续策略。后者保住已提交记忆，但改变了训练实际经历，不能把 `training_status: partial` 写成完整 BRS+DRS 收敛。

已正式出分的本地 7 个整流程作业中，6 个为 partial，只有 prometheus 重启版记录为 completed。Vitest 空记忆重跑和 oxvg 部分 BRS 重跑另列，不属于完整训练成功。

支持的恢复主要是已提交 BRS 完整波边界和原生检查点修复；任意 DRS/Phase 3 进程死亡后续跑并不完整。独立冻结记忆重跑是另一场实验，不是原会话的无损续跑。

### 4.5 记忆投递与产物交付不能只看“宿主机有文件”

记忆可能有多层目录，需要递归上传并核对实际送达的文件数/哈希。H20 DynamoDB 重跑 provenance 明确列出这项适配修正；本地 strict 包有 delivery verification。现有证据不足以把历史每次失分都归因于漏上传。

异常退出后找不到 `/logs/artifacts/model.patch`，可能是正常收集步骤没走完的后果，并不自动证明导出路径写错。H20 koota-pair 的 `agent/patch/fix.patch` 存在，只能证明某份工作产物尚在；补跑官方评分仍要核对来源与 base commit，不能提前宣称一定能通过。

## 5. 方法局限：哪些已证实，哪些还是假设

### 5.1 停止机制和成本控制弱，有证据；“无限循环”无证据

DRS 没有全局项目数/总时间上限；每次目标尝试和学习角色有各自上限，5h 也不是整个 RSI 的墙钟时限。目标 PASS 后，Curriculum 仍可认为有练习价值而继续；有练习之后需要再做新目标尝试。

H20 dateutil resumed-02 从 DRS 开始至作业报错约 18 小时，进行了五次目标尝试、前四次之间共六个已完成练习，目标判定出现 PASS、FAIL、PASS、PASS，仍未正常收敛。其 DRS 起点是北京时间 11 月 7 日 00:58，作业结束为当日 19:01，不能将 UTC 事件时间与本地作业时间直接相减。koota-pair 训练总墙钟约 33 小时后发生交接异常。作为未出分的旁证，本地 skrub 到此次检查仍处于 Phase 2，做了第二次目标 PASS 后又继续新练习。

建议表述：“停止决策依赖内部模型，缺少全局成本与收益约束；可能长时间重复探索，且无法保证达到官方正确性。”不要写成已经证明程序死循环。

### 5.2 内部验证与学习反馈的可靠性是核心局限

五个正式 Phase 3 结果都显示了 PASS 假阳性，且不只是不常见的语法细节，也包括 Prometheus 事务回滚和并发、Koota 组合语义等核心要求。内部审查不接触隐藏官方测试本身是正常评测边界；问题是自己推导的验证没有充分覆盖需求，却过早判 PASS。

同样的内部反馈还驱动停止与记忆学习，因此有放大错误的结构性风险。但“某条记忆确实错误并导致某次失分”仍需专门证据，不能从最终 reward=0 自动推出。

### 5.3 课程收益未被充分约束，目标覆盖也没有保证

大量练习 PASS、记忆增加，不保证官方难点被覆盖。Dateutil 经过多轮目标与练习，Phase 3 仍漏 `repr` 重建；Prometheus 正常完成训练仍漏 5 项官方测试。可以批评课程选择的有效收益和停止价值缺少外部校准；仅凭这些记录，不能断言每个练习都无用。

### 5.4 记忆有局部适用性，但尚未证明“不可迁移”

当前本来就是同目标条件下学习，再对同目标做全新执行，不是跨仓库迁移实验。记忆混合了三类内容：具体仓库/版本/API 细节，环境命令与路径，以及通用方法。

实际文件包括 `spec-driven-implementation.md`、`model-based-property-tests.md`、`durable-file-store.md`、`verification-playbook.md`、`patterns/deferred-thunk-lifecycle.md` 等；其内容有适用条件和通用检查方法，不能都归为具体 bug 答案。`/home/user/evolution_project` 是统一的练习工作路径，该路径不会随仓库变化就失效。

更准确的结论：“记忆中有较强的任务与环境特化，跨仓库可迁移性未评估，具体路径/API/版本经验需要适用范围与失效控制。”

### 5.5 记忆规模、检索成本和巩固质量仍缺乏保证

Dateutil 重跑银行为 21 文件、540117 字节，其中 README 为 103177 字节；Koota-pair 为 19 文件、556881 字节；DynamoDB 为 23 文件、438681 字节。银行变大不等于有效能力增加，检索和重复阅读有成本，也可能引入陈旧或冲突结论。当前由 Actor 自主组织与选择检索，数量与字节数只能说明规模，不能证明质量。

可补充的研究问题：是否需要记忆而非仅更多推理预算；哪些条目实际被用到；去掉仓库细节后是否仍有效；新目标失败后应修正哪些旧条目。现有单次/非匹配运行不能回答这些因果问题。

## 6. 实验解释上必须补充的限制

1. **缺匹配消融。** 模型、预算、提示词、报告协议、检查规则、训练完整性和重跑次数不一致；不能仅对比历史 baseline 与 RSI 得出 RSI 的净提升或净退步。例如 H20 GLM 原生 wasmi 全过、本地 RSI 少两项，也不能据此把退步归因于记忆。
2. **Vitest 的特殊对照。** strict 重跑全过使用 0 文件。它说明该执行框架在这次运行无记忆也能解题，不是记忆提升的证据；同样不能证明所有任务都不需要记忆。
3. **记录泄露后继续不等于没有泄露。** 新告警策略解决误判中断，但实际读取隐藏材料的运行必须单独标记。正则命中可能只是提示词回显，不能直接当实锤泄露；也不能因为不中断就默认结果干净。
4. **训练和评测状态分开。** 源训练未完成、部分记忆评估、完整冻结记忆评估、空记忆控制、已取消人工补充包应分别列出。只保留能成功出分的运行会有选择偏差。
5. **计费/成本不完整。** 多份结果的 input/output tokens 和 cost 为 null，model-call 统计也注明可能漏掉子代理/压缩。可以报告墙钟和观察到的调用次数，不能据此给出精确 token 花费或成本收益结论。
6. **总通过率会被 P2P 数量稀释。** dateutil 的 65/67 F2P 配上 2035/2035 P2P，总通过率接近 100%；expr 的 66265 项 P2P 也占绝大多数。必须同时报告 F2P、P2P、binary reward，不能只报 partial。

## 7. 建议替换的总结结构

### 运行与移植问题

1. BitFun 原生会话持久化、归档完整性与恢复范围。
2. 执行语义适配：异步命令状态、模型原生分段、异常返回结构。
3. 报告交接：格式约束、退出接收竞态、文件权限、错误详情丢失。
4. Verifier 隔离与副作用处理：原始候选保护和验证副本写入要区分，涵盖 pytest、Hypothesis、ESLint、BitFun tmp、构建输出、Git 元数据。
5. 环境管理：Docker 网络分配、锁竞争、清理与取消。
6. 长输入传输与上下文：命令行参数上限、原生窗口/压缩策略和提示词优先级差异。
7. 审计误判与异常传播：字符串命中不是实际访问；隔离/整波放弃会改变训练经历。
8. 记忆和最终补丁的交付校验，以及失败后分数与产物的恢复口径。

### 方法与评估局限

1. 内部验证有假阳性，影响最终接受、停止和后续学习。
2. DRS 缺少全局成本收益控制，目标 PASS 后仍可能反复探索。
3. 课程多样性与练习成功不保证目标难点覆盖或实际增益。
4. 自主记忆存在特化、陈旧、错误巩固、规模膨胀和检索负担；跨任务迁移未验证。
5. 缺少匹配预算、多随机种子、无记忆/BRS-only/DRS/full 的对照，无法分离方法增益与额外计算。

## 8. 可核对的证据入口

### 本地

- `results/deepswe/jobs/rsi-{participle,scriggo,sql-formatter}-20261111-5h-01/result.json`。
- `results/deepswe/jobs/rsi-prometheus-20261111-5h-02/result.json`。
- `results/deepswe/jobs/rsi-{wasmi,superjson,tomlkit}-20261110-5h-01/result.json`。
- `results/deepswe/phase3-strict/20261110-{vitest,oxvg}/jobs/evaluation/result.json`，以及各自 README、`phase3-memory.json` 和 `verifier/reward.json`。
- 各 trial 的 `rsi/result.json`、`rsi/events.jsonl`、`rsi/phase{1,2}/phase-result.json` 和 `verifier/test-stdout.txt`。
- Vitest 原报告：`results/deepswe/jobs/rsi-vitest-20261110-5h-01/vitest-duration-sharding__DNYLZky/rsi/phase3/reviews/round-002/agent/handoffs/`。
- Prometheus 误报：`results/deepswe/jobs/rsi-prometheus-20261111-5h-01/prometheus-transactional-reload__ETBcxej/rsi/phase1/quarantine/curriculum/wave_001/segment_000/boundary-audit.json`；命中 stream 第 2296 行，是读取当前会话压缩历史后返回的禁止说明。
- 完整性案例：participle ep002、scriggo ep001、skrub ep002、sql-formatter ep001/ep003 的 `rsi/phase1/quarantine/episodes/.../integrity.json`，原始候选最终一致，副本新增 tmp/cache。
- `docs/DEEPSWE_RSI.md`、`docs/DEEPSWE_BASELINE.md`、`docs/BITFUN_CONTEXT_POLICY.md`；源码 `benchmarks/deepswe/{session,supervision,rsi_runtime,rsi_integrity,contracts}.py`。

### H20

远端结果根：`/data/k8s/yx/RSIAgent4Coding/results/deepswe/`。以下路径均在 H20，不能当成本地同名目录。

| 事项 | 远端根下的证据路径 |
| --- | --- |
| dateutil 官方 65/67 | `jobs/bitfun-dateutil-rsi-phase3-20261108-01/dateutil-rfc5545-timezone-intero__2KcDCn8/verifier/reward.json` |
| DynamoDB 第一次 37/37 | `jobs/bitfun-dynamodb-rsi-phase3-20261109-01/dynamodb-toolbox-lazy-recursive__b2sZWF5/verifier/reward.json` |
| DynamoDB 第二次 37/37 | `jobs/bitfun-dynamodb-rsi-phase3-batch-20261110-01/dynamodb-toolbox-lazy-recursive__yMaB4mr/verifier/reward.json` |
| expr 官方 79/79 | `jobs/bitfun-expr-rsi-phase3-batch-20261110-01/expr-try-catch-errors__GkfUzDm/verifier/reward.json` |
| koota-pair 官方 33/38 | `jobs/bitfun-koota-pair-rsi-phase3-10h-20261111-01/koota-pair-relation-tracking__4xKd2Qm/verifier/reward.json` |
| 四个记忆包范围 | `phase3-memory/{dateutil-resumed-02,dynamodb-lazy-recursive-01,expr-try-catch-errors-01,koota-pair-relation-tracking-01}/provenance.json` |
| dateutil 长 DRS 与交接失败 | `jobs/bitfun-dateutil-rsi-resumed-02/dateutil-rfc5545-timezone-intero__AZYJSG8/rsi/events.jsonl` 和 `rsi/phase2/target_cycles/cycle_005/reviews/round-001/agent/` |
| DynamoDB 原 Phase 3 接收失败 | `jobs/bitfun-dynamodb-expr-rsi-20261106-p1-01/dynamodb-toolbox-lazy-recursive__LTLtPkP/rsi/phase3/` |
| Koota Actor 权限 | `jobs/bitfun-koota-pair-rsi-5h-20261109-01/koota-pair-relation-tracking__Jfqrmxn/rsi/runtime/environments/ee08a8ba9710407c90d453cfc7365958/agent/checkpoint-6d917d6b-1405-4d00-a720-c60405df9941-00{2,3,4}.json` |
| dateutil Verifier 权限 | `jobs/bitfun-dateutil-rsi-phase3-batch-20261110-01/dateutil-rfc5545-timezone-intero__R69vF35/baseline/reviews/round-002/agent/checkpoint-*.json` |
| 原生归档失败 | `jobs/bitfun-dateutil-rsi-20261104-p2-01/dateutil-rfc5545-timezone-intero__Zpfk6dy/rsi/phase1/phase-result.json` |
| 异步空状态异常 | `jobs/bitfun-dateutil-rsi-20261105-p2-06/dateutil-rfc5545-timezone-intero__QtRP3ho/rsi/phase1/phase-result.json`，及适配文档的保存流重放说明 |
| 长 prompt 启动失败 | `jobs/bitfun-dateutil-rsi-20261106-p2-08/dateutil-rfc5545-timezone-intero__dkSrfxA/rsi/runtime/environments/8161e593b01b437c802358d131c40f8c/agent/checkpoint-3c3a1477-96e0-455f-8cbd-78afbd60aa4e-003.json` |

没有将检索到的 API 凭据、私有推理或完整原始轨迹复制进本报告。以上证据是运行记录及代码核对，不是对全部模型轨迹、所有记忆语句或每个官方测试正确性的逐项证明。
