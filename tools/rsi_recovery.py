#!/usr/bin/env python3
"""Inspect faults, check stopped-job resumability, or retry live checkpoint repair."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.deepswe.rsi_checkpoint import archive_info, atomic_json


def inspect(trial):
    root = Path(trial) / "rsi"
    result_path = root / "result.json"
    report = {"trial": str(Path(trial).resolve()),
              "result": json.loads(result_path.read_text()) if result_path.exists() else None,
              "sessions": [], "integrity_checks": [], "infrastructure_errors": []}
    trial_result = Path(trial) / 'result.json'
    if trial_result.exists():
        record = json.loads(trial_result.read_text())
        report['trial_exception'] = record.get('exception_info')
        report['official_verifier_result'] = record.get('verifier_result')
    for pattern in ('**/branch_exception.json', '**/phase_exception.json',
                    '**/operation-errors.jsonl', '**/cleanup-errors.jsonl', '**/docker-lifecycle.jsonl'):
        for path in sorted(Path(trial).glob(pattern)):
            records = ([json.loads(line) for line in path.read_text().splitlines()]
                       if path.suffix == '.jsonl' else [json.loads(path.read_text())])
            if path.name == 'docker-lifecycle.jsonl':
                records = [r for r in records if r.get('event') != 'started']
            if records:
                report['infrastructure_errors'].append({'path': str(path), 'records': records})
    # Session launch errors used to be lost from the higher-level outcome. Read
    # bounded diagnostic records, never raw model requests or process arguments.
    for path in sorted((root / 'runtime/environments').glob('*/agent/checkpoint-*.json')):
        record = json.loads(path.read_text())
        if record.get('error'):
            report['infrastructure_errors'].append({'path': str(path), 'records': [{
                key: record.get(key) for key in ('session_id', 'call', 'status', 'error')}]})
    for path in sorted(root.glob("phase*/**/integrity.json")):
        record = json.loads(path.read_text())
        report["integrity_checks"].append({"path": str(path), **{key: record.get(key)
            for key in ("status", "changed", "missing", "unexpected", "removed", "error")}})
    for path in sorted((root / "runtime/private-sessions").glob("*.recovery.json")):
        status = json.loads(path.read_text())
        status["heartbeat_age_sec"] = round(time.time() - status["heartbeat"], 1)
        manifest = path.with_name(status["session_id"] + ".json")
        if manifest.exists():
            record = json.loads(manifest.read_text())
            try:
                if Path(record["archive"]).name != record["archive"]:
                    raise ValueError("Invalid archive path")
                actual = archive_info(path.parent / record["archive"], status["session_id"])
                valid = all(record.get(key) == value for key, value in actual.items())
                status["last_committed_checkpoint"] = {
                    "call": record["call"], "valid": valid,
                    "covers_current_call": valid and record["call"] == status["call"]}
            except Exception as error:
                status["last_committed_checkpoint"] = {"valid": False, "error": str(error)}
        status["events"] = str(path.with_name(status["session_id"] + ".events.jsonl"))
        report["sessions"].append(status)
    return report


def retry(trial, session_id):
    if not session_id or Path(session_id).name != session_id:
        raise ValueError("Specify the exact session ID from inspect")
    root = Path(trial) / "rsi/runtime/private-sessions"
    status = json.loads((root / f"{session_id}.recovery.json").read_text())
    if status["status"] != "waiting_for_checkpoint_repair" or time.time() - status["heartbeat"] > 15:
        raise ValueError("Retry requires a live controller waiting for checkpoint repair; "
                         "this command cannot restart a dead RSI process")
    path = root / f"{session_id}.retry.json"
    atomic_json(path, {"incident": status["incident"], "requested_at": time.time()})
    return {"requested": True, "session_id": session_id,
            "effect": "Retry checkpoint only; do not rerun the model or change the verdict"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "retry", "resume-check"])
    parser.add_argument("trial", type=Path)
    parser.add_argument("--session")
    args = parser.parse_args()
    try:
        if args.action == "resume-check":
            from benchmarks.deepswe.rsi_resume import inspect_resume, resume_summary
            result = resume_summary(inspect_resume(args.trial))
        else:
            result = inspect(args.trial) if args.action == "inspect" else retry(args.trial, args.session)
    except Exception as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
