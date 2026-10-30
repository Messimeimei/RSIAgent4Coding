# task_089 CSI profile

This profile has its own task protocol and lock templates. Both CSI entry points
use `custom_csi/route.json` by default, currently assigning:

- Actor, memory, and target: DeepSeek-V4-Pro
- Verifier, visual reader, and Curriculum: Qwen3.8-Max
- Sealed evaluator: Qwen3.8-Max
- User simulator: Qwen3.8-Flash

Run through custom_csi/task_089/run.py. Credentials remain in custom_csi/.env
and are never copied into this directory.

Change models only in `custom_csi/route.json`; each run snapshots that route and
generates matching baseline/evaluation locks under its results directory. No manual
lock edits or `prepare_locks.py` invocation are needed to switch models.

To use the retained profile-local route explicitly, pass
`--route-file custom_csi/task_089/route.json`. `prepare_locks.py` is only needed
when refreshing lock templates/config hashes; it reads models from the shared route.
