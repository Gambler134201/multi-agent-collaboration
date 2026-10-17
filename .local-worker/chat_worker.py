"""Isolated cancellable process for a panel analysis task."""
import argparse
import json
import sys
import model_registry as registry
from panel_server import run_chat

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run = registry.task_dir(args.run_id)
    if not (run / "cancelled.flag").exists():
        try:
            state = json.loads((run / "task.json").read_text(encoding="utf-8"))
            run_chat(run, state, registry.get_model(state["model_id"]))
        except Exception as exc:
            registry.patch_task(run, status="failed", phase="执行失败", ended_at=registry.now(), error=str(exc))
            sys.exit(1)
