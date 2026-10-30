#!/usr/bin/env python3
"""检查 Docker 镜像能否开出 QEMU 虚拟机，并且虚拟机能否断网"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
import uuid

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]   # RSIAgent 作为根目录
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from custom_csi.run import (
    HERE,
    RESULTS,
    RUNTIME,    # 读取镜像摘要文件：runtime.json
    _environment,
    _read_json,
    _write_json,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=HERE / ".env")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--boot-timeout", type=int, default=600,
                        help="每次等待截图接口的秒数；短诊断可设为 120")
    parser.add_argument("--boot-attempts", type=int, choices=(1, 2), default=2,
                        help="最多启动次数；短诊断可设为 1")
    args = parser.parse_args(argv)
    if args.boot_timeout <= 0:
        parser.error("--boot-timeout 必须为正整数")

    # 指定冒烟测试的输出路径
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or RESULTS / "smoke" / f"csi_null_seal_{stamp}.json")
    output = output.expanduser().resolve(strict=False)
    if not output.is_relative_to(RESULTS.resolve()):
        raise RuntimeError("smoke receipt must stay under results/")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite smoke receipt: {output}")

    # 构建环境变量表
    environment = _environment(args.env_file.expanduser().resolve(strict=False))
    for name in (
            "RSIAGENT_ROOT", "OSWORLD_ROOT", "RSIAGENT_ENV_FILE",
            "RSIAGENT_LLM_ROUTE_FILE", "OSWORLD_FILE_BASE_URL",
            "WEBSITE_HOST_SUFFIX", "RSIAGENT_EVAL_MIN_OUTPUT_TOKENS",
            "RSIAGENT_DISABLE_DIRECT_DOCKER_PUSH", "RSIAGENT_DOCKER_IMAGE",
            "NO_PROXY", "no_proxy"):
        os.environ[name] = environment[name]
    os.environ["RSIAGENT_VM_OWNER"] = "csi-null-seal-" + uuid.uuid4().hex
    os.environ["RSIAGENT_OSWORLD_CACHE_DIR"] = str(
        output.parent / (output.stem + "_cache"))

    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    from benchmarks.osworld.runtime import _install_paths

    _install_paths()    # 检查 OSWorld-V2 文件夹是否存在，并加载
    prepare_checkpointable_docker_provider(
        "rollback_mirror", boot_timeout=args.boot_timeout,
        boot_attempts=args.boot_attempts)   # 打补丁
    from benchmarks.osworld.setup import _boot_vm, _close, _reset_null_vm_sealed
    from explore.target_isolation import TargetIsolationSeal

    desktop = None
    seal = TargetIsolationSeal()
    try:
        print("[smoke] 正在启动 Docker/QEMU VM；等待桌面截图接口"
              f"（每次 {args.boot_timeout} 秒，最多 {args.boot_attempts} 次启动；另需采集诊断）。",
              flush=True)
        print("[smoke] 启动超时会保存容器日志、宿主机与容器内 HTTP 检查、网络规则、QEMU 状态和启动画面至 "
              + os.environ.get("RSIAGENT_BOOT_DIAGNOSTICS_DIR", str(REPOSITORY_ROOT / "results/boot_failures")),
              flush=True)

        desktop, vm = _boot_vm()    # 起容器（docker），再启动 QEMU 虚拟机
        print("[smoke] VM 已就绪；开始空任务重置与网络隔离检查。", flush=True)
        
        # 在外层 Docker 容器中，尝试对虚拟机重置为没有具体任务的基础环境，并检查虚拟机是否断网
        result = _reset_null_vm_sealed(
            vm, "冒烟测试是否断网", seal)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "null seal failed"))

        # 上面的检查成功，则写入
        runtime = _read_json(RUNTIME)
        receipt = {
            "schema_version": 1,
            "status": "passed",
            "kind": "csi_osworld_null_egress_smoke",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "runtime_path": str(RUNTIME),
            "runtime_sha256": hashlib.sha256(RUNTIME.read_bytes()).hexdigest(),
            "outer_image": runtime["outer_image"],
            "host_kernel_modules": runtime["host_kernel_modules"],
            "egress": result["egress"],
            "llm_calls": 0,
            "official_evaluator_calls": 0,
        }
        _write_json(output, receipt)
        print(output)
        return 0
    finally:
        if desktop is not None:
            _close(desktop)
        seal.close()


if __name__ == "__main__":
    raise SystemExit(main())
