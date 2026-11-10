"""Host-only quarantine receipts; isolated work never becomes learning evidence."""
from pathlib import Path

from .practice_loop import PracticeInfrastructureError, _atomic_json, _manifest, _read_memory_tree


def require_memory(path, expected):
    if _read_memory_tree(str(path)) != expected:
        raise PracticeInfrastructureError("Trusted memory changed outside a committed learning update")


def isolate_artifacts(lineage, source, error, *, stage, trusted_memory, project_index=None, error_details=None):
    lineage, source = Path(lineage), Path(source)
    relative = source.relative_to(lineage)
    if (not relative.parts or relative.parts[0] not in {"episodes", "curriculum", "waves", "target_cycles", "evolution_cycles"}
            or ".." in relative.parts or source.is_symlink()):
        raise PracticeInfrastructureError("Invalid project isolation source")
    destination = lineage / "quarantine" / relative
    if destination.exists() or destination.is_symlink():
        raise PracticeInfrastructureError("Project isolation destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        source.rename(destination)
    else:
        destination.mkdir()
    receipt = {
        "schema_version": 1, "status": "isolated", "stage": stage,
        "project_index": project_index, "error_type": type(error).__name__,
        "artifacts": str(destination), "source_artifacts": str(source), "memory_promoted": False,
        "trusted_memory_manifest": _manifest(trusted_memory), "terminal_outcome": None,
    }
    if error_details is not None:
        receipt["error_details"] = error_details[-16000:]
    _atomic_json(destination / "isolation.json", receipt)
    return receipt
