# BitFun 论文上下文策略适配

本实现是可选择启用的 BitFun 0.2.15 源码补丁，支持 RSIAgent 参考实现的角色差异与 WORK LOG 折叠机制。默认配置仍使用原生模式。未运行真实模型或 DeepSWE 评分实验，也未更改生产模型服务。

## 原因与依据

原版新建 CLI 会话的 `SessionConfig.max_context_tokens` 默认是 128128；执行引擎克隆会话时，取这个值与模型配置窗口的较小值。CLI 收到模型轮次事件后才更新会话窗口，来不及影响当前整段执行。配置输出 65536、预留 10000 时，原生压缩触发预算只有 **52592 token**。恢复会话才可能读取到配置的更大窗口。本地模拟请求已复现原版新会话 128128、恢复后 1048576 的差异。

此外，原生 summary 与参考 WORK LOG 不同；单独关闭 `enable_context_compression` 仍留下紧急截断路径。因此，仅调大窗口或关掉自动摘要不能复现论文策略。已有 dateutil 日志中的频繁压缩能证明发生了历史改写，不能据此断言它导致了 Verifier 的错误判断。

论文 `docs/RSIAgent.pdf` 第 21–22 页描述会话连续性/隔离，第 28 页列出输出预算。具体折叠参数以参考源码为准：RSIAgent 提交 `4d7ef43770dda341fccd9a5743db92407687c615` 的 `core/loop.py`、`core/actor.py` 与 `config/roles/*.yaml`。BitFun 基线提交及源码归档 SHA256 固定在 [manifest](../patches/bitfun/context-policy.json)。

## 已实现行为

| 角色 | 启用 `reference_context_policy: true` 后 |
| --- | --- |
| 目标 Actor（baseline、DRS target、Phase 3） | `work_log` |
| 练习 Actor | `full_history` |
| Verifier（目标和练习） | `full_history` |
| Curriculum | `full_history` |
| 同一 Actor 的后续学习 | 延续该会话已持久化的策略 |

策略跟随角色；交换 Actor/Verifier 的模型后会重新分配策略。不同角色的配置使用独立副本。

`work_log` 的默认参数：最近最多 80 组，最近历史字符预算 500000，至少保留 6 组；高水位 900000 字符、低水位 500000 字符；每次折叠最旧的完整 10 组；WORK LOG 上限 500000 字符（参考实现的截断说明另计）。超过高水位才启动折叠，逐批降到低水位或不再满足完整批次条件。摘要使用当前 Actor 模型、参考摘要 prompt、`temperature=0`，不提供工具；每条折叠输入取前 1500 + 后 2500 字符。空摘要使用参考 digest 回退，API 错误直接失败。任务原文固定保留，最近历史原样保留，完整原始轨迹保存在会话 turn 文件。

Coding 的一组定义为一次 assistant 模型轮次及其连续 tool results；多个工具调用与对应结果一起折叠，避免拆开 API 配对。这是对参考 action/observation 的接口适配。参考 prompt 被原样纳入补丁。

`full_history` 禁止上下文摘要、溢出后的原生压缩恢复和紧急删旧消息。两种严格策略都在请求前检查输入预算（窗口减输出预算和 10000 预留），不足则明确返回 `context_capacity_exceeded`，保留历史。估算不是服务端 tokenizer 的精确计数；服务端仍可能报超窗，此时也不自动删历史。严格模式保留历史图片，超过 provider 图片限制或无法重建图片时失败。

策略和 WORK LOG 状态进入会话持久化数据；恢复时保持已有策略。旧的 native/legacy 会话不能直接迁移到严格策略，必须新建会话，以免已经丢失的历史被误称为完整。手动 compact 在严格策略下也被拒绝。新会话在执行引擎读取前同步配置窗口，修复首轮 128128 限制。

## 构建

[补丁](../patches/bitfun/context-policy.patch)包含 11 个上游文件的变更。构建脚本从固定归档解压全新目录、验证哈希、应用补丁、使用 Cargo.lock 构建，输出独立二进制和来源凭据。需要 Python 3.10+（带 tar 安全提取支持）、Git、Rust 工具链、C 编译器和 Cargo 依赖。

```bash
python3 tools/build_bitfun_context_policy.py --test
```

默认 `dev` 构建用于功能验证；可指定 `--profile release-fast` 或 `--profile release`。有缓存时传 `--offline --archive PATH`。当前环境工具链安装在项目私有目录，可复用：

```bash
CARGO_HOME="$PWD/.runtime/bitfun-paper/cargo" \
RUSTUP_HOME="$PWD/.runtime/bitfun-paper/rustup" \
python3 tools/build_bitfun_context_policy.py \
  --cargo .runtime/bitfun-paper/cargo/bin/cargo \
  --archive .runtime/bitfun-paper/downloads/source.tar.gz --offline --test
```

产物为 `.runtime/bitfun-paper/bin/bitfun` 与 `bitfun.receipt.json`。原来的 `.runtime/deepswe/bin/bitfun` 保持原样。来源凭据绑定源码提交、补丁和二进制哈希，是本地构建审计记录，不是第三方签名。preflight 仅在显式启用论文策略且凭据匹配时接受这个不同于原版的二进制。

## 启用

可以复制现有 baseline/RSI YAML，将 BitFun bind mount 的 `source` 改为上述独立产物，并在 agent `kwargs` 中加入 `reference_context_policy: true`。角色映射由控制器自动写入 `ai.context_policy`。

推荐用配置生成器，显式提供每个启用模型的**已确认服务端总窗口**：

```bash
.runtime/deepswe/venv/bin/python tools/configure_bitfun_context_policy.py \
  --base config/deepswe/baseline-bitfun.yaml \
  --output /tmp/baseline-paper-context.yaml \
  --model-window 'DeepSeek-V4.1-Flash-1017=已确认的整数token数'
```

上面的中文占位符须替换为实际整数。RSI 多模型配置对每个模型 ID 重复 `--model-window`。生成器不覆盖现有文件，也不启动任务。之后可执行 `.runtime/deepswe/venv/bin/python run_deepswe.py check --mode baseline --config /tmp/baseline-paper-context.yaml`；RSI 使用 `--mode rsi`。

**不能把 YAML 写成 1M 当作服务端支持 1M 的证据。** 原参考参数以字符计，而窗口以 token 计；262144 token 的服务可能在触达 900000 字符前就需要停止。要逐参数复现应使用容量足够的模型服务；调整水位能适配较小窗口，但必须在实验中声明参数变化，不能称为原参数复现。补丁不会自动扩大模型服务容量。

直接使用 BitFun 时，在 `app.json` 的 `ai.context_policy` 中设置 `{"mode":"full_history"}` 或 `{"mode":"work_log"}`；省略时为 `native`。自定义水位放在 `work_log` 对象内，字段名与上述 manifest 所对应源码相同。

## 验证与边界

`tools/smoke_bitfun_context_policy.py` 启动 localhost 模拟 API，用真实 BitFun 执行固定的打印命令，不依赖 Docker/GPU。它覆盖首轮/恢复窗口、20 轮完整历史、28 轮 WORK LOG、多次折叠、实际请求的温度和工具字段、任务/最近历史、策略恢复、空摘要回退、容量不足、服务端错误、旧会话迁移拒绝：

```bash
python3 tools/smoke_bitfun_context_policy.py \
  --binary .runtime/bitfun-paper/bin/bitfun \
  --output /tmp/bitfun-context-smoke-new-directory
```

WORK LOG 冒烟测试故意缩小水位以快速触发；Rust 测试验证默认参数、字符窗口、Unicode 和批次边界。Python 测试验证角色分配、旧二进制拒绝、构建哈希绑定和配置生成。

当前验证结果：从干净源码归档应用补丁并构建 CLI 成功；相关 Python 测试 106 项通过、Rust 测试 9 项通过、最终二进制的端到端断言 11 项通过，core boundary 检查通过。完整 `cargo check --workspace` 因桌面端 `glib-sys` 需要系统 `pkg-config/glib` 而未通过；没有为此变更系统依赖。测试产物位于 `.runtime/bitfun-paper/`：`build-reproducible.log`、`python-tests-final.log`、`smoke-final/receipt.json`、`core-boundaries.log`、`workspace-check.log`。

本次验证针对文本编码工作流的上下文策略。BitFun 原生工具输出自身的长度限制、provider 对 reasoning 的编码差异、系统提示优先级和图像摘要方式仍与原始 harness 不同；这不等于完整复现整个论文。未经真实模型实验，不能声称修复了误判或提升了分数。
