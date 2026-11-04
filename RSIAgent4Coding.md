# RSIAgent 适配 Coding Agent 任务

## 一、H20 宿主机 Docker 配置情况

H20 宿主机
├── /data/k8s                    9.3 TiB 文件系统，约 5.0 TiB 可用
│   └── yx                       目前实际占用约 72 GiB
│       ├── RSIAgent             代码、配置、结果，约 756 MiB
│       └── OSWorld-V2           VM 基础镜像、venv、缓存，约 71 GiB
│
└── /data/docker                 Docker 全局数据盘，约 141 GiB 可用
    ├── overlay2                 Docker 镜像、容器可写层、boot.qcow2
    └── volumes                  容器 volume、虚拟数据盘

当前的宿主机被配置成了，DockerRootDir=/data/docker，StorageDriver=overlay2。所以不管在哪里执行 docker 的数据都会进入 宿主机里面的 /data/docker。所以其实 RSIAgent 里面的 actor 执行的环境，即
例如我并行了4个项目，这4个子任务启动的 QEMU/OSWorld 环境对应的 Docker 容器都在 /data/docker 下面：

代码、配置、结果、只读基础 VM：/data/k8s/yx
Docker 镜像、VM 可写系统盘、volume：/data/docker
计算：H20 宿主机的 CPU 和内存

## 二、Coding 任务在宿主机 H20 上如何运行（即 baseline）

一共 20 个 coding 任务，都是在宿主机上运行，且也是在 Docker 下运行，这一点和 OSWorld-V2 任务是相似的，DeepSWE 任务文件，运行结果可以保存在 /data/k8s/yx 下面，Docker 镜像和容器运行在 H20 的
/data/docker 下面。这里整个任务的调度和 OSWorld 不一样的是，不需要 QEMU 来管理虚拟机，完成通信啥的，没有这个复杂的过程。

DeepSWE 的任务由 Harbor 调度，且是在 Pier job 进程里面。这个进程会调用 H20 的 Docker daemon，然后挂载宿主机上的 uv、bitfun。之后由 bitfun 调用远程模型 API 完成任务，并把轨迹、补丁和分数写入
job_dir。

- **coding 任务描述**：

每个 coding 任务的文件夹结构如下：

deep-swe/tasks/fastapi-implicit-head-options/
├── task.toml
├── instruction.md
├── environment/
├── tests/
└── solution/

task.toml 文件描述了任务所需要的环境，如仓库版本，base commit，要使用的预构建的镜像等，镜像当中就包含了 base commit，python 等工具，项目依赖等。
然后 Harbor 的 Runner 会创建 agent 容器，里面的工作目录是 /app，然后宿主机上的 bitfun 和 uv 会只读挂载进去。

挂载完成后，Runner 会执行 bitfun，并把 instruction.md 这个任务描述文件给到 bitfun 启动，bitfun 作为 coding agent 就会开始调用远程模型来循环执行
任务：

阅读任务
→ 搜索仓库
→ 阅读实现和现有测试
→ 修改代码
→ 运行可见测试
→ 根据错误继续修改
→ 最终提交 Git commit

bitfun 完成任务后，DeepSWE 里面的 git collect 会比较 base commit 和 bitfun 提交的 HEAD，其实就是比较仓库，然后生成最终的补丁。collect
的意思就是收集 bitfun 对代码做了哪些修改，而 patch 补丁就是保存这些修改的文件。collect 是根据不同的任务自定义的收集步骤，最后通过类似
git diff 的操作，把 base commit 和 HEAD 之间的差异，保存到 model.patch 中。

有了 patch 后，隐藏的 test 代码，就可以提取里面的差异，然后在新的原始仓库中，应用 bitfun 的补丁，然后再运行测试。相当于把模型的改动
加载在和 baseline 一样的环境，然后运行测试打分。不同的任务语言有不同的测试：

- Python：pytest
- Go：go test
- TypeScript：Vitest/Jest/pnpm
- Rust：Cargo Nextest

而测试的内容包括两种，f2p 和 p2p。F2P 指的是原来失败、实现功能后应该通过的测试；P2P 指的是原来通过、修改后必须继续通过的回归测试。
只有所有 F2P 通过且所有 P2P 保持通过，最终二值 reward 才是 1。同时还会保留通过比例和失败原因。

最后的结果会写入宿主机里面的 jobs_dir 对应的 trial 目录

## 三、待测试的 20 个 Coding 的运行配置

n_attempts: 1
retry:
  max_retries: 0

表示每个任务只尝试一次。

## 四、RSIAgent baseline 适配 Coding 任务

第一个需要考虑的适配问题是被评测对象 bitfun 的适配，deepswe 的官方流程如下：

| 组件 | 作用 |
|---|---|
| DeepSWE | 提供任务描述、代码环境、隐藏测试和评分规则 |
| Pier | 创建容器、启动 Agent、收集补丁、运行官方评分、保存结果 |
| mini-swe-agent | 读取任务，调用模型，执行命令、修改代码、运行测试并提交 |
| 模型 API | 为 Coding Agent 提供推理和代码生成能力 |
| Docker | 提供执行代码和运行评分的隔离环境 |

通过 Pier 来管理整个评测过程，被评测的 agent 是 mini-swe-agent，然后再调用模型即可。整个 RSIAgent 适配 Coding 任务也可以从上面的官方流程来入手修改：

### 第一步：将 DeepSWE 的评测，修改成可以支持对 bitfun 的评测

这里面需要修改的应该是 Pier 相关的。但是不管怎么样，这一步要实现的就是让 bitfun 可以完成对 DeepSWE 的评测。修改内容如下：

| 文件 | 作用 |
|---|---|
| run_deepswe.py | 统一入口：检查配置、选择任务、启动评测 |
| benchmarks/deepswe/bitfun_agent.py | 将 Pier 的 Agent 调用接到真实 BitFun CLI，收集轨迹和日志 |
| benchmarks/deepswe/docker_environment.py | 适配模型代理、8080 端口及日志挂载，保留官方评分环境隔离 |
| benchmarks/deepswe/_vendor/bitfun_cli.py | 引入并适配上游 BitFun adapter 的基础能力和轨迹转换 |
| benchmarks/deepswe/_vendor/LICENSE、PROVENANCE.md | 上游许可证、来源及版本记录 |
| benchmarks/deepswe/__init__.py、_vendor/__init__.py | Python 包入口 |
| config/deepswe/dateutil-bitfun.yaml | 任务、模型、挂载、并发等运行配置 |
| config/deepswe/runtime.env.example | API Key 环境变量示例，不含真实密钥 |
| config/deepswe/versions.json | 固定 DeepSWE、Pier、BitFun 和适配器版本 |
| scripts/setup_deepswe.sh | 安装独立运行环境和固定版本二进制 |
| tests/deepswe/test_bitfun_agent.py、test_telemetry.py | 适配器与轨迹转换测试 |
| tools/smoke_deepswe_bitfun.py | 不调用付费模型的完整流程冒烟测试 |
| docs/DEEPSWE.md | 安装、运行、产物和验证说明 |
| .gitignore | 唯一修改的原有文件：忽略 .runtime/，并规范换行 |

其中，run_deepswe.py 主要负责各种配置，路径解析，和运行前的各种检查，最后调用 Pier 启动评测。benchmark/deepswe 下面的两个 python 文件负责适配工作，包括 Pier 适配评测 bitfun 和容器环境的适配，例如支持特殊模型端口的访问，维持独立的评分容器隔离等都是在容器环境适配这里完成。_vendor 下的四个文件主要是复用上游的 bitfun 能力。config/deepswe 下面则是每个实验的配置，可以选择一个通用的，也可以单独为每个任务配置一个 YAML 文件，同时里面的 versions.json 声明了依赖的版本信息，如 bitfun，Pier 和 deepswe 等的版本。对应用的数据集、agent 和 评测框架的版本信息。scripts/setup_deepswe.sh 则是把上面的版本声明落实为可执行环境，包括建立独立的虚拟环境，安装 Pier、bitfun 等。剩下的就是测试文件和 docs 说明文档。

在这一步完成后，可以通过 .runtime/deepswe/venv/bin/python run_deepswe.py run 加入指定的执行参数如 --taks --name 等，实现对 bitfun agent 的评测。

### 第二步：完成 RSIAgent 的 baseline 对第一步的兼容。

在第一步的基础上，把 RSIAgent 的 Actor - Verifier 的 baseline 逻辑接入进去。在这一步就需要把之前的 Actor 的逻辑进一步给抽象成，让 bitfun 充当 Actor 的角色，把之前通过发送历史对话轮调用 Actor 的方式，改成直接把命令发给 bitfun，并且由 bitfun 长程完成该任务，不再需要循环叠加传入历史对话。Verifier 也是一样。RSIAgent 本身只负责整个流程的管理，如会话连续性，终止条件等待。

具体来说，需要考虑的适配问题就是两点：控制层面的谁干活、谁判断、怎么反馈、恢复等这些和任务无关的逻辑和具体的模型调用，工作环境等完全任务相关。所以 DeepSWE 的代码任务可以直接用 RSIAgent 的工作逻辑；但是具体的模型调用，工作环境就需要修改，包括 bitfun 本身是 agent，有自己的循环，会话和 system prompt 等；和之前要求虚拟机的工作环境等，现在不需要了。

- **首先第一个是适配 bitfun** 即原来的模型 API 调用，发送历史对话消息的方式，怎么转换成调用 bitfun；以及原来的角色 prompt 是直接第一轮发送给远端 API 模型，现在要怎么发送给 bitfun，因为 bitfun 有自己的 system prompt；还有最后一个问题就是 prompt 本身的内容，怎么适配到 DeepSWE 任务上面，原来的 prompt 里面的方法层面的 prompt 可以保存，但是还有一些涉及到具体任务的需要做适配修改。

简单来说，当前 bitfun 充当 actor 的方式，就是把专门的一个 actor_prompt 传入到 bitfun 的 user message 当中，注意不是 system prompt，因为当前 bitfun 运行开始的时候，会先注入自己的 system prompt，再插入 tool calling guide 等多条提醒，actor_prompt 是通过用户消息传递给 bitfun。而之前的通过叠加历史对话来实现在同一个上下文的方式，在 bitfun 可以通过 resume 来实现同一个会话。

而 prompt 本身的修改，

- **然后第二个是适配 DeepSWE 任务的执行流程** 之前 OSWorld-V2 是在虚拟机的环境下，通过从宿主机接受模型 API 以 JSON 格式返回的 program/look/done 等命令，然后在虚拟机里面执行这些命令，并把结果返回给模型 API，最后 Actor 提交的材料是压缩包形式的修改后的材料，由 Verifier 进行读取和验证。最后的官方打分也是检查虚拟机里面的材料，并按照程序进行分权打分。

而 DeepSWE 任务是在 Docker 环境下，指定容器内有开源仓库的 base commit，然后 bitfun 需要修改仓库里面的文件，最后返回一个 patch 文件，再由官方评分器进行打分完成 f2p 和 p2p 的评测。具体的流程区别如下：

1. osworld 是启动 QUMW 的虚拟机，而 Pier 是启起 docker 容器，且里面已经有 HEAD == base commit，表示干净的环境

2. Actor 干活区别。osworld 是由 actor 每轮输出 JSON 动作，宿主机塞进虚拟机，执行完把 stdout/exit code 给到模型；deepsew 是把 instruction.md 给到提前编写好的 actor_prompts 里的 TASK 模板变量之中，然后整个 actor_prompts 给到 bifun exec 命令的输入里面后续由 bitfun 在容器里面跑完完整流程，包括读写操作等。而原来的 RSIAgent 需要负责保存 history，调用模型，解析 program/look/done 操作和执行结果并返回模型。（run_osworld.py 启动整个流程：benchmarks.osworld.task.py 启动环境，然后需要调用 llm/client.py，以及 core/loop.py 里面的逻辑实现）

3. Actor 独立检查，并提交完成材料的区别。原始的 osworld 和 deepswe 两者的提交产物，都是留在工作环境中，不会上传任何东西，提交的只是一个 JSON 内容告知完成了。osworld 的产物就是放在虚拟机里面的被修改好的文件和应用的状态，如 excle 表被修改好了；deepswe 的产物就是 docker 容器里面 /app 的仓库里面已经 commit 的代码改动。例如代码编辑后，执行了 git commit，那么改动就留在了 git 历史里面，通过执行 git diff 就可以把 patch 导出用于评测。

原本 osworld 任务，模型在 actor 完成任务后进入 verifier 之前，会要求先检查一遍，这里的检查的内容就是 actor 返回的 JSON 格式的 done 里面的 checks 字段。相对应的， bitfun 是调用它自己的工具写入 json 文件来表示结束和提交，字段如下：

{
  "kind": "actor_done",
  "candidate_head": "a3f8c2e...",        ← 我提交的 git commit 号
  "summary": "我实现了 RFC 5545 时区互操作...",
  "checks": [
    {
      "desc": "repr 必须可重建 rrule 对象",
      "probe": "cd /app && python -m pytest tests/test_rrule.py -k testRruleReprReconstructable && printf 'PASS'"
    }
  ]
}

checks 字段里面的 probe 就是需要进行自检查的内容，这个是进入 verifier 之前的一次门槛，通过了才会进入 verifier，没通过则直接把失败的信息反馈给 actor 不走 verifier。当前的两个实现，都会先完成自检查，osworld 的自检查是直接在 actor 所在的虚拟机里面执行相关命令，没通过的话 actor 可以直接返工；而 deepswe 则是重新开一个容器，检查完后销毁。

4. Verifier 检查的区别。osworld 的 verifier 需要检查的是一个正在运行的虚拟机（上面有 actor 在运行提交了结果），所以为了防止后续 verifier 破坏 actor 的提交产物，采用的是 rollback_mirror，会事先保存 checkpoint，完成检查的操作后会进行回滚。这里是在一个隔离的环境。而 deepswe 没有这种顾虑，它完全可以在一个新的容器上（目前就一共有3个独立的 容器了，分别是 actor，verifier 和这个用于检查的）创造一个新的一样的 base commit 仓库，应用 actor 提交的 patch 来进行检查。用完了再删除该容易。

两者检查的过程因为任务的不同而不同，一个检查虚拟机里面的文件，通过截图看内容等，另一个看 patch 是否有用等。但最后都返回一个 verdict 报告，里面有 PASS/FAIL/UNVERIFIED, report, findings, evidence, evidence_requests 这些字段，如果时 FAIL 会把 report 报告返回给同一个 actor 会话，补齐后再在相同的 verifier 里面检查，如此循环。

5. 官方打分的区别。osworld 最后完成了的产物，是保存在虚拟机里面的，它有一个官方的 grader 会去查看虚拟机里面的状态，例如对应任务的材料 excel 表的状态，然后按照不同的权重进行打分返回 0-100 的分数。而 deepswe 是由 Pier 的 collect 收集 docker 容器里面的材料，主要是容器里面已经提交的 diff，即 patch 文件。然后在官方的隐藏镜像环境里面，用程序进行评测得到分数。

> 上述逻辑已经同步实现，与基础的 baseline 尚不一致的地方包括：
> 1. 当前 bitfun 会提前进行上下文压缩，导致 verifier 可能丢失细节，任务越长影响越大。
> 2. actor 自检查环节。actor 给出的待检查的 probe，会被分为三类，accepted	rejections	dropped。前者是通过命令检查的，第二个是没通过命令检查的。
> 3. 独立的 witness gate 没有实现。原始逻辑会检查 Actor 的 probes 是否至少有一个独立渠道证据，如果所有 probe 都只是重新读取自己产生的文件，系统会一次性要求 Actor 增加独立 witness。

### 第三步：在第二步的基础上引入 RSIAgent 的概念。

这一步需要考虑的是怎么把 Curriculum 的概念引入，这个需要一个单独的 bitfun 会话负责完整，并生成对应的训练材料；然后需要修改之前 Actor 做题的逻辑，也是单独开一个 bitfun 会话，在会话里面完成连续的作答并最后给出交付的材料。这里需要修改的就是 Actor 相关的 prompt，把之前适配桌面操作相关的修改掉，包括允许模型做的操作，和返回格式的要求等，都需要重新修改。最后是 Verifier 的 prompt 也需要修改，包括指定返回的判断类型，需要告诉 bitfun 返回 PASS 还是 FAIL 还是 UNVERIFIER，除此之外，Verifier 的回滚问题也需要适配，在 Verifier 进行检查之前需要保存 Actor 的结果，同时 Verifier 与 Actor 之间的返工交互也要做好，如果 Verifier 觉得需要继续做的话，怎么把 Verifier 的结果给到 Actor 再让 Actor 接着干活。

- 第四步：还有需要考虑的就是，最后得到了冻结的记忆，应该怎么保存和加载。包括 BRS 和 DRS 两个阶段得到的记忆怎么保存，最后怎么在 phase2 开头和最后的 phase3 阶段加载给 bitfun 让他读取记忆完成任务。