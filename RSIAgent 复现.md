# RSIAgent 复现说明

## 运行结果

| 任务序号 | 任务描述 | baseline | RSI | 论文结果（baseline/RSI） | 备注 | token 消耗 | 耗时 |
|---|---|---:|---|---|---|---|---|
| T080 | WPS 表格修复 | 5 | 67.06（78.95/55.17） | 5；68.69（68.69/68.69） | 官方评测器存在路径处理问题，会额外添加 /xl 路径前缀。本次 baseline 触发该问题，RSI 保存的 Excel 未触发。修复评测路径问题后，baseline 重评分为 62.29/100，表中仍保留原始得分 5。RSI 分数为两次冻结记忆评测的平均值。 | 暂缺准确统计，待从 OpenCsiTool 获取调用用量 | baseline：约 1 小时 13 分 08 秒。<br>RSI 三阶段合计：20 小时 40 分 43 秒，其中 BRS 11 小时 10 分 49 秒、DRS 8 小时 22 分 57 秒、首次正式评测 1 小时 06 分 55 秒。<br>另一次复用冻结记忆的评测：约 1 小时 41 分 18 秒。 |
| T089 | 浏览器演示文稿修复 | 67 | 69（71/67） | 67.50；81.25（79.50/83.00） | RSI 比本次 baseline 提高 4 分。BRS 完成两波共 12 道练习，10 PASS、2 FAIL；DRS 完成一次补充练习，原任务第二次尝试通过。最终内部 Verifier 判 PASS，官方评测仍在侧栏样式和正文布局比例上扣分。 | 暂缺准确统计，待从 OpenCsiTool 获取调用用量 | baseline：约 1 小时 45 分 40 秒。<br>RSI 三阶段合计：22 小时 56 分 19 秒，其中 BRS 16 小时 23 分 18 秒、DRS 4 小时 39 分 25 秒、正式评测 1 小时 53 分 34 秒。 |

## 运行结果说明

第二个任务的效果之所以没有比 baseline 好多少，主要有3个原因：

1. BRS 给出的练习任务有很多，但是没有针对具体的难点。baseline 本身在运行的过程中，下面几点自己就被他的 verifier 指出问题并修复了

- 服务正常启动；
- 点击侧栏、前后翻页、左右键导航正常；
- 标题和内容能随页面更新；
- 动画检查通过

所以涉及这些功能的官方打分，baseline 都拿到了。而 BRS 的练习中，又练了服务启动与持久化、导航、动画、动态需求检查等。这些练习可能让操作更熟练、更可靠，但在这一次测试中，对应分数 baseline 已经拿满了，继续加强也没有额外分数。剩下的拉开分数的是布局和样式，而 RSI 在这部分只取得很小的改善。说明**训练内容和最终需要突破的短板，没有完全对上**。

1. RSI 的 verifier 放宽了问题的检查。在两次最终的 phase3 阶段，verifier 都发现了两个问题，例如发现小窗口下图片被裁切和字号和颜色存在差异，但是却觉得不重要没问题。导致
actor 没有返回去修改这些问题，导致丢分。

3. 最终保存的记忆中，并不适用最终的官方评测。在冻结的记忆中，有下面一段成功经验：

> 文字列与图片列采用 1fr / 0.85fr，间距 360px，实测列宽比约 1.18，符合参考截图。

然后后面 phase3 阶段 actor 也用来这个方法，但是这个方法是错误的，不符合官方评测要求的。（需要注意的是，因为参考截图本身的要求，和官方评测的要求就是不一致，这算是一个 bug，但是
也说明如果总结了不正确的经验，会影响最终评测）

## 一、运行环境说明 

* **外层 Docker 镜像提供运行环境**

整个 OSWorld-V2 任务是在宿主机的 Docker 镜像内运行的。这个 Docker 镜像负责提供 “运行虚拟机的机房”，Docker 里面主要包含：
QEMU，虚拟机启动脚本，VNC、HTTP controller，ebtables 等网络隔离工具，KVM 设备接入。Docker 镜像启动后产生一个 Docker container，
负责启动和管理里面的虚拟机。

具体来说，Docker 是负责把上面提到的 QEMU，网络和控制服务一起包装起来，真正负责启动虚拟机的是 QEMU。

* **内层 qcow2 镜像提供具体的 OSWorld-V2 任务需要的条件**

.qcow2 是一块虚拟硬盘，里面包含了 Ubuntu 操作系统，/dev/sda3 ext4 文件系统，WPS、Chrome 等桌面软件，OSWorld 需要的桌面环境等。
当 Docker 里面的 QEMU 启动时，相当于给虚拟机插入了这块硬盘，然后启动 Ubuntu。

* **镜像与实例的关系**

上面提到的 Docker 和 qcow2 都是镜像，两个镜像相当于是模板，一个是创建 Docker 容器的模板，一个是创建 Ubuntu 虚拟机的模板。

Docker 镜像  → 启动后变成 Docker 容器
qcow2 镜像  → 启动后变成一台 Ubuntu 虚拟机

每次运行，最外层的宿主机在 tmux 中执行 run.py，这个 python 进程运行在 H20 宿主机上。在进入需要桌面环境的阶段后，python 进程创建 DesktopEnv：

provider=docker
os_type=Ubuntu
screen=1920x1080
volume_size=60 GB

DesktopEnv 请求宿主机的 Docker daemon 启动，得到容器，在启动容器的时候，同时传入了 /dev/kvm，发布网络等端口，挂在只读的 qcow2 盘。这个盘是只读的，
正常运行不会修改它，而是进入临时可写盘。

启动容器后，容器中的 QEMU 启动虚拟机，模拟主板，磁盘，网卡和显示设备，kvm 则是让虚拟机的 cpu 指令直接在宿主机的 cpu 上运行。接着准备好了这些虚拟机的
硬件后，启动 Ubuntu 操作系统，上面有 WPS，Chrome 等需要的软件。

* **controller 承上启下**
在准备好了虚拟机和操作系统后，宿主机上的 python 进程会通过 Docker 端口连接 guest controller ，也就是连接虚拟机的控制器，包括：

5000  OSWorld controller
8006  VNC/WebSocket
8080  VLC相关接口
9222  Chrome remote debugging

对于具体的任务 T080 而言，OSWorld 进程会读取任务 json，通过 controller 把任务的材料上传到虚拟机的桌面，并启动 WPS 等操作。而后续与模型进行交互的过程，也
是通过 controller 来完成，模型返回的脚本，会被传入到虚拟机，通过这些 controller 执行，然后执行结果再返回给模型。这里的 controller 就是虚拟机上的远程控制服务
和宿主机上调用这些服务的代码。

可以把 controller 理解为客户端和服务端两部分。H20 宿主机是客户端，通过 python 向虚拟机发送 HTTP 请求；而虚拟机 Ubuntu guest 则是服务端，接受 HTTP 请求后，
会在虚拟机上面启动对应的 subprocess，并返回结果。这里发送的 HTTP 请求，实际上是根据模型给出的程序大小决定，如果小于 32 KB 就直接把模型代码编码成 Base64，并随机
生成临时文件名，发送给 guest 的 controller；如果比较大的话，就流式传输到虚拟机上面，校验后再执行。

具体的 controller 执行过程，则是由虚拟机创建 subprocess，等结果完成后收集 stdout/stderr/returncode，并最后用 HTTP 返回 json 内容给宿主机。宿主机解码后，把
执行结果整理成新的会话格式，先保存在本地的会话记录中，然后连同之前的对话历史一起用来发起下一次的模型 API 调用。

## 二、baseline 运行踩坑

1. guest  volume expansion 报错

在运行 custom_csi/smoke_runtime.py 的时候，出现了不健康的 VM 启动，存储或者是 controller 状态。导致正式运行开始 baseline 之前就报错了。

* 解决方法：直接重新启动一个新的实例，即重新跑 custom_csi/smoke_runtime.py 脚本，通过冒烟测试后就说明环境没有问题。之后重新运行的 baseline 则
  顺利通过了。

## 三、rsi 运行踩坑

0. 部分任务所需要的软件等有所缺失。
   
OSWorld-V2 官方里面的评测，是采用 “基础镜像+每个任务自动初始化”的方式。官方的镜像里面没有安装所有任务需要的东西，而是每次启动一个任务的时候，根据基础镜像
得到一个临时的虚拟机，然后任务脚本里面会安装任务需要的软件、材料等。

例如任务 T106 要求虚拟机里面有 3D Slicer 这个软件，官方代码是会自动安装，但是我尝试过安装超时了。

1. 缺少 corpus。

第一次运行 rsi 的时候，在通过了模型 api 检测和 phase1 的 preflight后，报错：

FileNotFoundError:data/k8s/yx/RSIAgent/results/explore

这个问题是因为，phase1 阶段默认需要 results/explore/corpus_shingles.json。它是由 OSWorld-V2 的任务指令生成，供宿主机检查准备写入记忆的内容是否混入其他任务的题目。
并且需要运行前生成。README (RSIAgent/README.md:216) 要求在运行前执行 tools/build_p2_corpus.py。当时连父目录 results/explore 都不存在，因此错误显示的是目录路径。

* 解决方法：在运行所有任务之前，先执行生成这个 corpus 的代码即可。

2. geust 文件系统故障

第二次运行的时候，也是通过了模型 api 检测和 phase1 的 preflight后，报错：

guest filesystem staging fault; fallback used=False
aborting Verifier phase on trusted sandbox failure

这次报错是长时间运行中 VM 文件系统 remount 为只读，导致 Verifier 无法 staging/cleanup。Actor 产生的部分 branch 虽然得到 PASS/FAIL，
但整个 wave 因基础设施异常没有提交任何 memory。具体报错原因不太清楚，表层现象就是在运行 BRS 的第一轮广度搜索的时候，子任务运行完成后的
Verifier 想要写入文件却发现 VM 变成了只读。

* 解决方法：重新启动任务。前面可能也是环境不干净，重新启动后就完成了（实际上是官方镜像有问题，在 BRS 阶段有可能碰见镜像里损坏的文件就会有这个问题）

3. Actor 输入超限

这次任务运行成功了， 成功执行到 BRS 的第一轮，生成了8个子任务，但是第一个子任务 project 01 的输入长度是 1145137 token，在经过连续重试后，报错
并写入了 branch_errors，导致即使后续子任务完成，这一轮结果产生的 memory 也不会被加入冻结的记忆里面。

这个问题产生的原因是，一个 actor 在解决任务的时候，每一轮对话都会加上历史轮的对话记录导致每次输入长度越来越长。而官方代码的历史压缩触发条件是
对话轮数达到一定次数才触发，这里的问题是达到指定轮数前，最新一轮的输入长度就超限了所以报错。主要是程序执行结果返回了很长的一个表格打印结果。

* 解决方法：在 yx/RSIAgent/config/roles/actor.yaml 里面增加了一个参数 trace_context_max_chars，这个是官方代码支持的，但是官方没用
（官方代码注释里面也强调可能会出现这个输入超限的问题，但是代码没限制）所以我加入了这个官方的参数，让后面练习 actor 收到的输入有长度限制。

4. 只关闭 tmux 里面的程序不会完全终止 rsi  进程，会浪费模型调用和机器资源

我上面说的我自己打断了运行，只是 ctrl + c，并没有完全终止进程。需要检查 pyhon 的进程，再 kill 掉。

5. 虚拟机 guest 根文件系统变成了只读，导致 verifier 检查阶段 5 个子任务有 3 个都出现了这个问题，其中第五个在第一次重试后成功了，而第二个和第三个则是重试后仍然失败。由于
项目脚本的一次 wave 不允许有任何子任务失败，所以第二个和第三个任务的 verifier 阶段的失败，导致整个 wave1 失败，phase1 也失败。

经过诊断后发现，是 O0SWorld-V2 官方版本清单指定的基础镜像里面有一个损坏的文件 inode，虚拟器访问正常文件没有异常，但是仅对哪个坏的文件执行一次 stat，内核立刻报 inode #1214912 ... checksum invalid，
随后将 /dev/sda3 改成只读。

简单来说，项目使用的 OSWorld 基础虚拟机镜像里，存在一个损坏的文件系统 inode。访问它，会让虚拟机的整个根分区变成只读。基础镜像是：

/data/k8s/yx/OSWorld-V2/docker_vm_data/osworld-v2-ubuntu-x86.qcow2。

整条链路是：

Verifier 大范围搜索文件
→ 搜索碰到损坏的 inode
→ ext4 检测到校验错误
→ 为保护文件系统，将根分区改成只读
→ 执行框架无法删除 /tmp 下的脚本和日志
→ 判定为基础设施故障，终止练习

* 解决办法：创建一个官方镜像的副本并完成修复，然后为了不修改太多地方，把原来默认用的镜像路径，通过软链接到修复后的镜像。

6. 模型调用，隐藏推理超出了输出预算限制。RSIAgent 论文默认允许的 max_tokens 是 65536，这里包括了隐藏推理和最终可见回复，都属于本轮生成过程，共同消耗这部分输出预算。
这就导致，GLM-5.3 模型的隐藏推理已经用了 64611 token 额度，剩下的正式回复加上去就超出输出上限，导致被截断。所以使用模型的时候，其推理过程也是需要限制的，或者干脆不
使用输出这么长推理的模型。

7. 基础镜像缺少扩容工具，导致任务初始化的时候用 apt 可能冲突报错。基础镜像里面没有扩容工具，而每次运行都会临时安装扩容工具。前面几次任务运行没报错，但是 T106 任务就报错
了，也是很恶心的。

* 解决方法，继续更新一个基础镜像的副本，把扩容工具装进去。也就是提前把需要的基础镜像里的东西都尽量安装好，这样可以节省所有任务的时间和减少报错。

8. 审计误报问题。RSIAgent 在 phase1 会有防泄漏的审计拦停操作，它命中了 task_1 和 0.68，以为练习内容里面泄露了题目。具体来说：
审计规则 (yx/RSIAgent/tools/exam_fence.py:335)扫描整个 transcript，只要同一任务的“字符串常量＋另一个常量”同时出现就报警，没有判断两者的语义关系，因而把文件名示例和音量系数拼成了“泄漏证据”。

论文代码具体的审计过程如下：

首先论文针对 OSWorld 这个任务，扫描评分源码 OSWorld-V2/evaluation_examples/task_class/task_*.py，并从中提取任务需要保护的字符如：

- 两位以上小数的浮点数；
- 四位以上整数；
- 看起来像任务内容的字符串；
- 受保护任务的文件名、字段名、特殊短语。

把这些写入到一个 json 词表中，例如当前 085 的词表 results/audit/fence/grader_constants.json 中有：0.010 0.68 0.78 take_1 take_2。在得到了这个词表以后，框架会审计 Agent 的运行产物，
递归扫描例如 txt，json 等文件，这里不仅是模型最终回答被审计，整个保存的轨迹和程序输出都会被检查。所以这一步相当于是防止泄露。

但是论文代码的问题很多，包括它判断的是所有受保护任务的词表，即任务a 如果出现了任务b 的词表里面的字符，也会被判断是泄露了。以及具体的审计的逻辑，没有排除掉正常运行而非泄密的字符，也会导致误判。

* 解决方法：把过于敏感的规则文件 /data/k8s/yx/RSIAgent/tools/exam_fence.py 进行修改，结合前面3个任务的错误经验进行规则的放宽。

9. DRS 阶段无生成子任务数限制，导致运行时间过长。在执行 T106 任务的时候，DRS 阶段一直生成的子任务执行失败，导致模型不断生成新的项目，耗时过长。

## 四、历史压缩的操作说明

简单来说，是当达到了触发压缩的条件的时候，会额外调用同一个模型，让它把较早的对话整理成 WORK LOG，然后用 “摘要+最近的原始对话” 继续做题。

先看看历史压缩的相关配置：

| 参数 | 具体含义 |
|---|---|
| history_keep_pairs: 80 | 开启压缩，最近最多 80 轮对话保留原文。设为 0 就关闭整个压缩机制 |
| keep_chars: 500000 | 从最新对话往前选，尽量保留不超过 50 万字符的最近对话；因此实际可能只保留 20 轮，而非固定 80 轮 |
| ctx_high_water: 900000 | “已有摘要＋尚未折叠的历史”超过 90 万字符时，尝试启动压缩 |
| ctx_low_water: 500000 | 压缩启动后，尽量把上述大小降到 50 万字符以内 |
| fold_batch: 10 | 每次摘要调用，处理最早的 10 轮可压缩对话 |
| worklog_max_chars: 500000 | 生成的摘要最多保留约 50 万字符，超过后从末尾裁掉 |

数字指的是字符数而不是 token 数。每次准备调用模型前，RSIAgent 的 loop.py 都会检查：

1. history_keep_pairs 是否大于0。用于判断是否要开启压缩。
2. 已有摘要+尚未折叠的历史是否大于 ctx_high_water。用于判断什么时候开始压缩。
3. 去掉必须保留原文的最近的历史对话后，剩下几轮 fold_batch 即最旧对话可以压缩。用于判断压缩几轮对话。

按照上面的配置，具体的触发压缩的条件是，已有摘要+历史对话超过 90 万字符，且除去最近需要保留原文的对话，还剩至少 10 轮对话。其中最近需要保留原文的对话数，是由
keep_chars 和 history_keep_pairs 两个参数决定。根据 history_keep_pairs 决定保留轮数，但是如果每轮字符很多，可能达不到 80 轮就提前终止开始压缩了。

## 五、具体的运行过程

以 `20261027_01_task_080_rsi` 运行为例说明，两个主要目录为：

```text
运行配置、接口探测和阶段日志：
/data/k8s/yx/RSIAgent/results/custom_csi/20261027_01/

三个阶段的会话、练习、记忆和结果：
/data/k8s/yx/RSIAgent/results/recursive_improvement/20261027_01_task_080_rsi/
```

下面提到的 `phase1/`、`phase2/`、`phase3/` 均相对于第二个目录。

完整流程是：

```text
校验配置、生成本次配置快照、探测模型接口
  → Phase 1 / BRS：Curriculum 出一波题目和材料
  → 多个独立 Actor 做题，各自交给独立 Verifier 检查
  → 整波验证结束后，按项目顺序由各 Actor 提炼、校正并提交记忆
  → 冻结 Phase 1 记忆
  → Phase 2 / DRS：带记忆尝试原始 t080，验证并学习
  → Curriculum 决定是否补充练习；每次练习后更新记忆，再尝试原始任务
  → 达到停止条件，冻结 Phase 2 记忆
  → Phase 3：在重新初始化的任务环境中，用冻结记忆做原始 t080
  → Actor / Verifier 流程结束后，调用官方评测器得到分数
```

### 1. 校验运行配置，并进行模型接口测试

入口是 [custom_csi/run.py](RSIAgent/custom_csi/run.py)。它先检查 baseline 与 RSI 的模型、角色配置哈希、评测器、用户模拟器、任务发布版本和记忆策略是否匹配，再为本次运行保存配置快照。

配置和探测记录在：

```text
results/custom_csi/20261027_01/
├── plan.json
├── probe.json
├── config/
│   ├── route.json
│   ├── baseline.lock.json
│   └── evaluation.lock.json
└── task_080/
    ├── direction.md
    ├── protocol.json
    ├── 1_phase1_preflight.log
    ├── 1_phase1.log
    ├── 2_phase2_preflight.log
    ├── 2_phase2.log
    ├── 3_phase3_preflight.log
    └── 3_phase3.log
```

路由文件把框架中的规范模型名映射到实际模型：`z-ai/glm-5.3` 对应 DeepSeek-V4-Pro，`moonshotai/kimi-k3` 对应 Qwen3.8-Max。

`_probe()` 进行三次相互独立、没有历史消息的能力探测。可以把它们理解成三个单轮逻辑会话，但不是在服务器上创建三个持久聊天窗口：

| 探测 | 实际模型 | 输入和预期输出 | 请求输出上限 |
|---|---|---|---:|
| 长上下文文本 | DeepSeek-V4-Pro | 2,200 行合成文本，要求回复 `CSI_TEXT_READY` | 65,536 token |
| 长上下文 JSON | Qwen3.8-Max | 同类合成文本，要求返回 `{"status":"CSI_JSON_READY"}` | 65,536 token |
| 图片输入 | Qwen3.8-Max | 16×16 的合成图片，要求回复 `CSI_IMAGE_READY` | 32,768 token |

后续所有长会话，主要由客户端维护：`llm/client.py` 每次将 `system + 历史消息 + 当前输入` 重新组成请求；模型返回回复，框架解析动作并执行，再把执行反馈放入下一次请求。
因此，在 OpenCsiTool 中往往能看到同一逻辑会话的输入 token 随历史增长。

### 2. Qwen3.8-Max 作为 Curriculum，生成 Phase 1 的练习波次

Phase 1 的目标是围绕 t080 所需能力进行广度探索。Curriculum 看到任务描述、当前记忆和已有波次结果，自己探查空白练习环境、设计项目并生成材料。

本次第一波 Curriculum 的完整记录在：

```text
phase1/curriculum/wave_001/segment_000/
├── transcript.json
├── result.json
├── iter_01/
├── iter_02/
└── ...
```

`transcript.json` 包含 `system` 和按顺序排列的 `messages`。每个 `iter_**` 是一次交互的落盘记录，常见文件如下：

| 文件 | 含义 |
|---|---|
| `turn.txt` | 这一轮模型返回的文本，包括计划、解释以及动作 JSON |
| `program.py` / `program.sh` | 从模型回复中提取、实际交给虚拟机执行的程序 |
| `trace.txt` | 程序输出和退出状态等反馈 |
| `trace_meta.json` | 退出码、执行时长、超时和基础设施故障标记 |
| `look` 相关记录 | 看图请求及返回的视觉观察，具体文件随动作类型而异 |
| `transcript.json` | 当前会话段归档的系统提示词和消息历史；不是只有最后一轮 |

本次 [Curriculum 结果](RSIAgent/results/recursive_improvement/20261027_01_task_080_rsi/phase1/curriculum/wave_001/segment_000/result.json) 记录 **26 轮、25 次程序执行、1 次看图，耗时约 68 分 49 秒**。

Curriculum 使用 Qwen 自身的视觉能力，`look` 获取的图片观察接回它自己的消息历史。DeepSeek Actor 的看图则使用配置中的 Qwen 视觉读取器，返回文字观察后再让 DeepSeek 继续；这两种看图调用方式要区分。

模型在虚拟机 guest 中准备 `/home/user/evolution_wave/` 下的各项目材料，并发布 `/home/user/phase1_wave.json`。RSIAgent 框架读取、检查发布内容，把正式决策保存为：

```text
phase1/waves/wave_001/curriculum_decision.json
```

这个文件的 `decision` 为 `WAVE`，里面的字段 `rationale` 解释为什么设计这一组练习，`projects` 列出每个项目的 `id` 和 `instruction`。本次只有一波，共 **10 道题**，练习覆盖了备忘录理解、业务规则迁移、公式和缓存值、样式、图表、WPS 操作等不同能力。

RSIAgent 框架将每道题拆到 `phase1/episodes/ep001` 至 `ep010`：`project.md` 是项目指令，`fixtures/materials.tgz` 是 Curriculum 创建的输入材料，旁边的 manifest 等文件用于检查材料完整性。它们是新生成的练习项目，不是直接把原始 t080 的 Excel 和 PDF 发给 Actor。

### 3. DeepSeek-V4-Pro 作为 Actor，独立完成每道练习

每个项目分配独立的 Actor 逻辑会话和隔离的 VM 环境。框架把该项目的 `fixtures` 即任务材料恢复到 guest 的 `/home/user/evolution_project`，再附上本波开始时的记忆。

第一道练习的完整 Actor 会话在：

```text
phase1/episodes/ep001/actor/attempt_001/segment_000/transcript.json
```

第一轮请求包括 Actor 的 system prompt、具体项目指令、项目目录、交付约束、记忆目录说明及当前文件清单。Actor 自己决定是否打开某个记忆文件；框架不是每轮都把所有记忆全文塞进提示词。第一波的记忆为空，因此本次十个 Actor 做题时看到的都是同一份空记忆快照。

`--parallelism 2` 表示最多同时执行两个练习分支，即使 ep010 比 ep001 晚启动，它的做题阶段仍使用波次开始时的空记忆。真正的记忆合并要等整波验证结束。

Actor 每轮输出程序或看图请求，框架在 guest 中执行并返回证据，Actor 再根据历史继续工作。本次 ep001 的 Actor 归档有 **34 个 user/assistant 消息对**。最后它发布包含 `STATUS: SUBMIT` 的交付说明，框架保存：

```text
phase1/episodes/ep001/
├── project.md
├── fixtures/materials.tgz                  # 做题前的输入材料
├── actor/attempt_001/segment_000/          # 做题会话和逐轮记录
├── candidates/cycle_001/materials.tgz      # 提交后的项目文件快照
├── handoffs/actor_001.md                   # Actor 的交付说明
└── actor_execution_evidence/cycle_001/     # 程序、执行反馈等证据归档
```

`candidates` 保存的是项目文件，不是整个虚拟机，也不是全部聊天内容。`actor_execution_evidence` 另行保留执行程序、返回 trace 等证据；`fixtures` 与 `candidates` 分别回答“给了什么材料”和“最后提交了什么”。

### 4. Qwen3.8-Max 作为独立 Verifier 检查候选结果

Verifier 新建自己的逻辑会话，第一轮得到验证角色的 system prompt、题目要求、候选项目位置、证据边界和报告提交方式。它不会直接沿用 Actor 的聊天历史，也不能把 Actor 自述“已完成”当作验证结论。
它和官方评测器的区别在于，他是模型根据题目本身的要求和作答做出的判断，并没有官方的打分逻辑，只会根据题目要求判断是否完成。

ep001 的 Verifier 会话在更深一层目录：

```text
phase1/episodes/ep001/verifier/cycle_001/
  verifier_agent/inspection_001/segment_000/transcript.json
```

这次 ep001 的 Verifier 归档有 **27 个 user/assistant 消息对**。它通过专用 `verifier-report` 动作提交完整报告，报告包含独立一行 `VERDICT: PASS` 或 `VERDICT: FAIL`；框架将报告保存到 `handoffs/verifier_001.md`。

这里的 PASS/FAIL 是内部练习验证结果，不是官方 benchmark 得分，也不保证 Verifier 自身绝对正确。本次波次模式为每道题记录一个验证周期，**FAIL 后仍可作为失败经验进入记忆学习，不要求把同一道题一直重做至 PASS**。

本次十道题的实际结果如下，来源是各 `episodes/ep***/outcome.json`：

| 项目 | 项目 ID | 练习重点 | Verifier 结论 |
|---|---|---|---|
| ep001 | `gtm-channel-model-analog` | 依据备忘录综合修复渠道经营模型 | PASS |
| ep002 | `gtm-territory-restructure` | 分组维度和角色相关业务公式迁移 | PASS |
| ep003 | `scorecard-format-conventions` | 数字格式、汇总行、标题和图表规范 | PASS |
| ep004 | `summary-kpi-formulas` | 跨表 KPI 和公式生成的汇总说明 | PASS |
| ep005 | `plan-recalc-cached-values` | 保留公式并通过计算引擎写入缓存值 | PASS |
| ep006 | `broken-reference-surgical-repair` | 修复断裂引用并保留工作簿其他功能 | PASS |
| ep007 | `memo-spec-extraction` | 提取备忘录规则、缺陷映射和预期计算 | FAIL |
| ep008 | `wps-gui-edit-and-save` | 通过 WPS 图形界面编辑并保存 | FAIL |
| ep009 | `render-visual-audit` | 通过渲染发现和修复视觉问题 | PASS |
| ep010 | `multi-dim-stress-model` | 多维、大表规模下的业务迁移和结构保留 | PASS |

两道 FAIL 的报告分别认为：ep007 的交付程序依赖 `openpyxl`，在恢复后的候选环境中无法直接运行；ep008 的提交文件未提供符合要求的 WPS GUI 编辑保存证据，Verifier 据此否决了执行方式。这里记录的是 **Verifier 的判断**，尤其不能把重置后缺少应用运行痕迹直接等同于独立证明 Actor 从未执行过这些动作。

这与前面“guest 只读、传输失败、Verifier 无法运行”的 `infra` 故障不同。可信的 FAIL 是可学习结果；分支抛出基础设施或边界异常、没有可靠完成验证时，整波不能越过验证屏障去提交部分学习结果。此前“任意子任务失败就导致整波失败”应理解为这种执行异常，不能理解为“不允许题目判 FAIL”。

### 5. 整波验证结束后，由原 Actor 提炼和校正记忆

本次 10 个分支全部得到 PASS/FAIL 后，框架才开始按 **ep001 → ep010** 的顺序提交记忆。最后一个分支 ep008 于 10 月 27 日 **21:13:43** 验证结束；第一份分支记忆于 **21:17:56** 提交，最后一份于 **22:21:57** 提交。

记忆不是由 Curriculum 或 Verifier 代写，而是由**做过这道题的同一个 DeepSeek Actor 继续原会话**生成。主要分两步：

1. `memory_distillation`：RSIAgent 框架的模型会话 loop 追加本题最终 PASS/FAIL 和完整 Verifier 报告，让 Actor 结合自己的操作历史、候选文件和证据，提炼可复用的操作方法及失败教训。
2. `memory_reconciliation`：继续同一会话，检查整个虚拟机里面的 `~/.memory`，修正矛盾、过时规则、没有证据支持的因果解释和过度概括，不只是机械追加一篇总结。然后再加到 `~/.memory` 里面。

第一道题的记录在：

```text
phase1/episodes/ep001/
├── memory_distillation/segment_000/transcript.json
├── memory_reconciliation/segment_000/transcript.json
├── memory_before_commit/
├── memory_after_commit/
├── outcome.json
└── outcome.md
```

这里有直接的会话连续性证据：ep001 做题归档为 **68 条消息**，提炼阶段为 **72 条**，校正阶段为 **78 条**；后一份消息历史以前一份为完整前缀，新增内容以 `NEXT PHASE` 引导。因此，文件夹变成 `memory_distillation` 不表示模型已经忘掉前面的做题过程。

Actor 将记忆写成虚拟机 guest `~/.memory` 下的文件。框架拉回这些文件，进行边界和语料审计、记录哈希及变更，再原子更新宿主机 `phase1/memory/`，并在 `phase1/memory_journal/` 保留每次提交的归档。

### 6. Phase 1 结束并冻结记忆

本次 `project_budget=8`，但 Curriculum 一次发布了 10 道题。预算只在完整波次结束时检查，不会做到第 8 道就截掉同波剩余项目。因此实际完成 **1 波、10 个项目，8 PASS、2 FAIL**，最终状态为 `budget_exhausted`。这里的 `budget_exhausted` 是到达正常预算边界，不是崩溃；之后 Phase 2 正常启动。Phase 1 最终冻结 **10 个记忆文件，共 67,983 字节**：

```text
phase1/memory/          # 广度探索期间依次提交的记忆
phase1/memory_journal/  # 提交历史
phase1/checkpoints/     # 项目边界上的记忆检查点
phase1/memory_frozen/   # 交给 Phase 2 的冻结版本
phase1/result.json     # 阶段汇总
```

本次 Phase 1 的官方评测调用数为 **0**。这时拥有的是练习经验，尚未得到 t080 的官方分数。

### 7. Phase 2：带着记忆做原始 t080，并根据结果安排深化练习

Phase 2 开始使用原始目标任务，在新的虚拟机加载任务材料，把 Phase 1 的冻结记忆复制成当前阶段可更新的工作记忆。

本次 Phase 2 的主要目录是：

```text
phase2/task_080/attempt_83080/
├── active_memory/
├── target_cycles/
│   ├── cycle_001/
│   │   ├── transcript.json
│   │   ├── verifier_agent/
│   │   └── terminal_learning/
│   └── cycle_002/
├── evolution_cycles/
│   ├── cycle_001/
│   │   ├── trigger/outcome.json
│   │   ├── curriculum/turn_001/
│   │   ├── curriculum/turn_002/
│   │   ├── curriculum/turn_003/
│   │   ├── episodes/ep001/
│   │   ├── episodes/ep002/
│   │   └── curriculum_ready_for_retry.md
│   └── cycle_002/
│       └── curriculum/turn_004/
├── events.jsonl
├── memory_frozen/
└── result.json
```

`83080` 是本次 Phase 2 的种子，不是练习编号。`target_cycles` 保存的是带着记忆做原始任务的结果，`evolution_cycles` 保存的是 curriculum 决定展开的课程决策和补充练习
后生成新的补充练习 + actor 执行验证并更新记忆的过程。

本次实际发生的顺序如下：

| 顺序 | 实际操作和结果 | 对记忆的影响 |
|---|---|---|
| 1 | 带 10 个文件的记忆第一次尝试原始 t080，Verifier 判 PASS | 同一个 Actor 做终结学习，记忆增至 11 个文件 |
| 2 | Curriculum 检查目标报告、Actor 的学习诊断和记忆，决定仍有值得测试的薄弱点 | 开始一轮补充练习，而不是立即结束 |
| 3 | ep001：修复 FY27 Partner Channel 模型，Verifier 判 FAIL | 记录辅助表的标题行与数据行样式颠倒问题，记忆增至 12 个文件 |
| 4 | ep002：修复 FY27 Service and Support 模型，Verifier 判 PASS | 吸收后续实践经验，记忆增至 13 个文件 |
| 5 | Curriculum 返回 `DECISION: READY_FOR_TARGET` | 带更新后的记忆重新尝试原始 t080 |
| 6 | 第二次目标尝试再次得到 PASS，Actor 再做一次终结学习 | 文件数仍为 13，内容继续更新 |
| 7 | Curriculum 再审查一次，没有发布新练习，返回 `READY_FOR_TARGET` | 满足 `verifier_pass_curriculum_ready`，结束 Phase 2 |

第一次目标已经 PASS 仍继续练习，是因为本次停止策略为 `curriculum_review`：即即使是 PASS 后也允许 Curriculum 选择有信息价值的对照练习。它不是“只在目标失败时才运行 DRS”，也不是“第一次 PASS 就停”，而是由 Curriculum 的模型自己决定。

Phase 2 每次得到可信的目标或练习结果后，原 Actor 都先执行 `memory_distillation`、`memory_reconciliation`，再生成 `learning_diagnosis.md`。诊断描述尝试了什么、证据支持什么、还有哪些不确定点，供 Curriculum 决定下一步。

例如第一次目标学习的证据在：

```text
phase2/task_080/attempt_83080/target_cycles/cycle_001/terminal_learning/
├── memory_distillation/
├── memory_reconciliation/
├── learning_diagnosis/
├── learning_diagnosis.md
└── outcome.json
```

与 Phase 1 的整波延迟合并不同，**Phase 2 是顺序学习**：第一道补充练习更新记忆后，第二道就能使用这些新经验；之后的目标重试也使用最新记忆。

最终统计是 **2 次目标尝试、2 次目标学习更新、1 次包含练习的演化、2 个补充练习**。

### 8. 冻结 Phase 2 记忆，Phase 3 重新做任务并正式评分

Phase 2 结束时生成 **13 个记忆文件，共 100,424 字节**，包括目标修复经验、补充练习的失败教训和成功步骤。最终记忆树 SHA-256 为：

```text
fd2a034730b0b0b0f1aace3379666d2de2e20cec460bea8cb533d3563dfc171b
```

框架把记忆复制到 `phase2/memory_frozen/`，随后再复制到 `phase3/memory_frozen/`，并写入 `phase3/memory_lock.json`。Phase 3 检查记忆与冻结版本一致，在评测结束后再检查宿主机冻结记忆未变化。

Phase 3 会重新初始化原始 t080 的环境和材料，启动新的目标 Actor / Verifier 会话，不沿用 Phase 2 已经修好的工作簿。跨阶段传过去的是记忆文件，不是前一次的成品或完整聊天上下文。Actor 根据记忆文件清单自主读取内容并完成任务；Curriculum 和记忆更新关闭，宿主机记忆不回写。

本次最终任务结果不在 `phase3/` 里堆放所有逐轮记录，而是保存在：

```text
results/task_080/seed93080_20261027_01_task_080_rsi_phase3/
├── transcript.json
├── iter_01/
├── ...
├── verifier_agent/
└── result.json
```

Actor / Verifier 流程结束后，才调用封闭的官方任务评测器。本次成功调用一次，得分为 **0.7895381816719933，即 78.95/100**，同时汇总到 [phase3/result.json](RSIAgent/results/recursive_improvement/20261027_01_task_080_rsi/phase3/result.json)。

### 9. 本次完整流程的实际耗时和后续结果

| 阶段 | 开始 | 结束 | 阶段墙钟耗时 |
|---|---|---|---|
| Phase 1：BRS | 10 月 27 日 11:11:09 | 10 月 27 日 22:21:58 | 11 小时 10 分 49 秒 |
| Phase 2：DRS | 10 月 27 日 22:21:59 | 10 月 28 日 06:44:56 | 8 小时 22 分 57 秒 |
| Phase 3：冻结记忆评测 | 10 月 28 日 06:44:57 | 10 月 28 日 07:51:52 | 1 小时 6 分 55 秒 |
| 三阶段端到端 | 10 月 27 日 11:11:09 | 10 月 28 日 07:51:52 | **20 小时 40 分 43 秒** |

总时间包含阶段内的环境、交互和收尾等待，不包含启动 Phase 1 之前的接口探测，也不包含另外运行的 baseline。不能把单个 Actor 的 `wall_secs` 当作整个 RSI 的耗时，也不能把并发分支的时间简单相加当作实际等待时间。

之后又用同一份冻结记忆运行了 `seed93081_20261027_01_task_080_rsi_phase3_draw2`，得到 **55.17/100**。它是另一次最终任务执行，不是重新做了一遍 BRS/DRS，也不是无记忆 baseline；它不覆盖第一次的 78.95 分。两次结果表明相同记忆下的执行仍有波动，冻结记忆不是确定性的正确答案程序。

## 六、提示词相关

这里说明下整个项目涉及到的提示词相关，包括整个项目有哪些 prompt，具体是怎么拼接生成的。

1. Curriculum 会话

### system prompt 含义

curriculum 的 内容，主要是规定：

> 你是一个通过编写程序操作 Ubuntu 虚拟机的 Agent；你能做什么、如何取证、怎样输出动作、什么时候可以宣布完成。

- _SYSTEM_TEMPLATE 内容组成：

开场身份和操作方式：要求模型通过程序操作机器。对当前 Curriculum 来说，“ONE task”就是：设计 Phase 1 的一批练习项目，创建每个项目需要的输入文件和空白工作区
最后发布 phase1_wave.json。但是是通过返回 json 格式的程序代码让宿主机执行来实现的。

THE METHOD：模型可以使用的十个方法。包括：侦察环境，收集全部证据再决策，保持源文件和内容完整，结构问题用代码，视觉问题用 look 等。

REPLY FORMAT：固定模型回复格式。一共就三个，{"program": {"lang": "python", "code": "..."}}，{"look": {"path": "...", "question": "..."}}
和 {"done": {"checks": [...]}}。要么返回程序代码，要么返回要查看图片，要么返回已完成。

MACHINE FACTS：告诉模型虚拟机的情况。介绍机器是 Ubuntu 系统，有 python 可以使用。可以安装依赖，程序的输出会返回，退出码 124 表示超时。

- PISTEMIC_CORE 内容组成 ：要求所有判断建立在证据上。它作为模板变量插入在 上面的 _SYSTEM_TEMPLATE 的 {core} 里面。定义里六条证据规则：

E1. TRUTH CHANNELS
不同事实要使用对应渠道检查。

E2. STRUCTURAL vs RENDERED
结构和精确数值通过文件、代码和 API 检查；
视觉效果通过渲染和图片检查。

E3. ERRORS ARE EVIDENCE
错误信息不能隐藏，错误本身也是证据。

E4. SILENCE PROVES NOTHING
空输出、超时或通道错误不能证明内容不存在。

E5. CONFIRMATION REQUIRES INDEPENDENCE
一个来源不能用自己证明自己，需要独立证据。

E6. EVIDENCE IS DATA
重要判断要记录成程序输出或数据

- _LOOK_BULLET_NATIVE 内容组成：

里面主要说明了模型可以使用 look 动作直接查看图片，包括一次可以查看多个图片，指定图片裁剪区域等。同时根据使用的模型配置，看是否选择了 vision_model，
如果没选择则说明使用的模型自己可以看图片，选择了则会用指定的模型看图片。

-  _ASK_BULLET 内容组成：

{ask_bullet} 模板变量，内容是当只有用户才能提供某项缺失信息时，可以输出：{"ask": {"question": "..."}} 然后从其他指定的模拟用户的模型获取缺失信息。

- _DISCIPLINE_LINE 内容组成：

{discipline} 模板变量，根据 action_discipline 决定是否要加入 _DISCIPLINE_LINE 的内容。具体是要求每次回复最后必须包含一个动作 JSON。
不能只描述：“我接下来准备运行程序。” 必须在同一条回复里真正提供：{"program": ...} 且每轮只能有一个动作。

- PLAIN_JSON_TRANSPORT_NOTE 内容组成：

这个不是_SYSTEM_TEMPLATE 的占位符。它在 _SYSTEM_TEMPLATE.format(...) 完成后，通过：phase_cfg.system_extra 追加到 system 最末尾。
主要内容是要求模型不要使用模型 API 的原生 tool/function call。program、look、done 必须作为普通 assistant 文本中的 JSON 输出。
RSIAgent 会自己从文本中解析这个 JSON。不要输出 tool tags，不要输出 tool_choice 字段。

### system prompt 生成过程

yx/RSIAgent/results/recursive_improvement/20261027_01_task_080_rsi/phase1/curriculum/wave_001/segment_000/transcript.json，下面存放的是完整的
一次课程生成的会话。

curriculum 的会话内容主要由 curriculum.yaml 这个配置文件决定，因为其 prompt 的拼接受配置影响，不同的运行配置决定了选择拼接的 prompt 片段。
以上面的会话里面的 system prompt 为例，其构成如下：

_SYSTEM_TEMPLATE
  ├─ {core}
  │    = EPISTEMIC_CORE
  │
  ├─ {look_bullet}
  │    = _LOOK_BULLET_NATIVE
  │
  ├─ {ask_bullet}
  │    = 空字符串
  │
  └─ {discipline}
       = _DISCIPLINE_LINE

+ "\n\n"
+ PLAIN_JSON_TRANSPORT_NOTE

其中 _SYSTEM_TEMPLATE 就是 yx/RSIAgent/core/actor.py 里面指定的 system prompt 变量，里面有不同的占位符。在这一次的运行当中，look_bullet 是根据
使用的模型是否要自己处理图片，如果不是就传入，表示用另一个模型看图片。ask_bullet 控制模型能否向模拟用户提问。discipline 控制是否插入“每轮必须执行一个
action”的强约束，也就是每一轮必须有且只能有一个 action。

当前 Curriculum 能用的 action 是：

program
look
done

最后添加的 PLAIN_JSON_TRANSPORT_NOTE 是 JSON 动作传输说明，它要求模型把动作 json 直接写在普通的 assistant 回复文本中，不要输出原生的 API 工具调用标签
或者 tool_choice 字段，只输出普通的 json 对象。即：

{
  "program": {
    "lang": "python",
    "code": "print('hello')"
  }
}

不能是：


{
  "tool_choice": "python",
  "arguments": {
    "code": "print('hello')"
  }
}

或者

tool_call:
  name: python
  arguments: ...

最后得到的给 curriculum 的 system prompt 如下：


base = _SYSTEM_TEMPLATE.format(
    core=EPISTEMIC_CORE,
    look_bullet=_LOOK_BULLET_NATIVE,
    ask_bullet="",
    discipline=_DISCIPLINE_LINE,
)

system = (
    base
    + "\n\n"
    + PLAIN_JSON_TRANSPORT_NOTE
)

### user prompt 含义

### user prompt 生成过程

yx/RSIAgent/results/recursive_improvement/20261027_01_task_080_rsi/phase1/curriculum/wave_001/segment_000/transcript.json，里面第一个 user prompt。

其整体结构可以概括如下：

first_user_prompt = (
    "TASK:\n"
    + curriculum_template(
        target_direction=T080任务文本,
        seed=根据研究类型选择的固定文本,
        prior=上一波结果或默认文本,
        wave_root="/home/user/evolution_wave",
        handoff_path="/home/user/phase1_wave.json",
    )
    + "\n\n"
    + "你现在处于一个新的虚拟机环境，请先侦察并给出 PLAN..."
)

外围用一个 TASK 告诉模型 具体的任务是什么，然后通过 yx/RSIAgent/explore/charter.py 最后的 phase1_wave_curriculum_charter() 函数，提供一个固定的 Curriculum
模板把 target_direction，seed，prior，wave_root 和 handoff_path 拼接起来。最后再加入一段固定的外部环境提示内容，大意是告诉模型当前是一个新的虚拟机，需要先侦察
先输出 PLAN。

**固定的 Curriculum*模板**内容组成：

1. 角色身份：告诉模型你是持久 Curriculum Agent，每个项目会创建新的 Actor 和 Verifier。

2. 权限边界：告诉模型你是搜索控制器，不是 solver，不是 grader。不能访问官方 evaluator、隐藏检查和分数

3. 项目选择原则：告诉模型你可以自主决定一个 wave 有多少项目。项目要提高可复用能力或检验不确定性。同一 wave 的项目相互独立。

4. 项目文件要求：告诉模型在 {wave_root}/<id>/evolution_project 创建项目输入。不能预先放入完整答案。

5. 输出格式：告诉模型把 JSON 写入 {handoff_path}。JSON 必须是：

{
  "decision": "WAVE",
  "rationale": "...",
  "projects": [
    {
      "id": "...",
      "instruction": "..."
    }
  ]
}

也允许：SATURATED 和 STALLED。

**target_direction** 模板变量的内容，是在运行的时候，把 config/target_queries/task_080.md 这个任务说明文件复制到 results/custom_csi/20261027_01/task_080/direction.md
然后把里面的内容给到这个模板变量，告诉模型任务文本是什么。

**seed** 模板变量是根据不同的运行条件，给模型不同的探索模板，

if target_query_conditioned:
    seed = "具体目标只作为搜索方向，请生成前置、变体、对比和压力测试项目；不能直接尝试原始目标。"
else:
    seed = "这是一个任务分布，请在这个分布里进行广泛探索。"

这里的 seed 主要用来区分有具体的提升目标的任务和留出任务泛化

**prior** 模板变量主要是根据是否有上一轮生成子任务的结果来生成 prior。如果是第一次生成 wave（一轮子任务），那么这个模板变量就被：“no earlier Phase-1 wave has completed“
填充，如果是之后的 wave，就会把第一轮生成的情况传递进去。

**剩余两个路径模板变量** 里面主要用来告诉模型，在虚拟机的那个位置创建每个项目的输入文件和最后的 wave 决策写在哪里。其实就是分别存储具体的子任务的文件夹在哪里，以及最后生成
的这一轮的任务汇总 json 文件保存在哪里。