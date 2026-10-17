"""Run the home model through an isolated Aider installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4
from model_registry import get_model, list_models, now, patch_task, task_dir
import model_router
import model_scope
import record_protection
import task_timing
import workflow_settings

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
PYTHON = HERE / "venv" / "Scripts" / "python.exe"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--model-id", help="Panel model ID; defaults to the selected model")
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    parser.add_argument("--source", default="codex", choices=["codex", "panel", "cli"])
    parser.add_argument("--title")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--task")
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--files", nargs="+")
    parser.add_argument("--test-cmd", help="Optional explicit project test command")
    parser.add_argument("--timeout", type=int, default=0, help="可选总时限，0表示不限")
    args = parser.parse_args()
    args.timeout=task_timing.normalize(args.timeout)
    if args.list_models:
        print(json.dumps(list_models(), ensure_ascii=False, indent=2))
        return 0
    settings = get_model(args.model_id, require_enabled=not args.status)
    key = settings["api_key"] or "local-no-auth"
    if args.status:
        request = Request(settings["api_base"].rstrip("/") + "/models",
                          headers={"Authorization": "Bearer " + key})
        opener = build_opener(ProxyHandler({})) if settings.get("bypass_proxy") else build_opener()
        with opener.open(request, timeout=20) as response:
            models = json.load(response)
        print(json.dumps({"api_base": settings["api_base"], "models": models,
                          "python_exists": PYTHON.is_file()}, ensure_ascii=False, indent=2))
        return 0

    if not args.project or not args.files or bool(args.task) == bool(args.task_file):
        parser.error("Provide --project, --files, and exactly one of --task or --task-file")
    project = args.project.resolve(strict=True)
    if not project.is_dir():
        parser.error("--project must be a directory")
    files = []
    for name in args.files:
        path = (project / name).resolve()
        if not path.is_relative_to(project):
            parser.error("Selected files must be within --project")
        if path.exists() and not path.is_file():
            parser.error("Selected paths must be files")
        files.append(path)
    # The workbench record directory is control-plane evidence: never a writable
    # Aider target.  (Aider has no DSH tool guard, so this dispatch check is the
    # only boundary for that engine -- it is not OS-level isolation.)
    record_protection.validate_files([str(p.relative_to(project)) for p in files], project)
    task = args.task if args.task else args.task_file.read_text(encoding="utf-8-sig")
    task = task.strip()
    if not task:
        parser.error("Task is empty")

    # The global execution-model scope also guards a direct CLI Aider run, so the
    # panel is not the only enforcement point.  A refusal never changes the model.
    workflow = workflow_settings.snapshot()
    paid = model_router.is_paid(settings)
    model_scope.check(workflow.get("model_scope", model_scope.DEFAULT_SCOPE), paid)
    if paid:
        if not workflow.get("paid_allowed"):
            raise ValueError("付费执行未开启，请检查工作台授权设置")
        # Match the router: Aider cannot enforce the paid per-request accounting
        # and optional limits that DSH provides. A direct CLI run is no exception.
        raise ValueError("付费任务请使用 DSH，以执行逐次请求预算检查")

    run = task_dir(args.run_id) if args.run_id else HERE / "runs" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
    run.mkdir(parents=True, exist_ok=True)
    if (run / "cancelled.flag").exists():
        raise ValueError("任务已停止")
    prompt = run / "task.txt"
    prompt.write_text(task + "\nOnly modify the selected files. Briefly describe changes and any unresolved issues.\n", encoding="utf-8")
    patch_task(run, id=run.name, title=args.title or task.splitlines()[0][:80], kind="edit",
               model_id=settings["id"], model_name=settings["name"], model=settings["model"],
               project=str(project), files=[str(p.relative_to(project)) for p in files],
               status="running", phase="准备文件", source=args.source,
               started_at=now(), worker_pid=os.getpid(), test_command=args.test_cmd)
    global ACTIVE_RUN
    ACTIVE_RUN = run
    before = {str(path): digest(path) for path in files}
    env = os.environ.copy()
    # Keep these changes inside the child process; leave the parent Codex environment intact.
    for name in list(env):
        if name.upper().startswith("AIDER_") or name.upper() in {
            "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_ORGANIZATION",
            "OPENAI_ORG_ID", "OPENAI_PROJECT", "OPENAI_PROJECT_ID",
        }:
            env.pop(name, None)
    env.update({"OPENAI_API_KEY": key, "OPENAI_API_BASE": settings["api_base"],
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    hostname = urlparse(settings["api_base"]).hostname
    no_proxy = ",".join(filter(None, [env.get("NO_PROXY", ""), env.get("no_proxy", ""), hostname if settings.get("bypass_proxy") else None]))
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    model = "openai/" + settings["model"]
    model_settings = run / "model-settings.yml"
    model_settings.write_text(json.dumps([{"name": model, "extra_params": {
        "max_tokens": settings.get("max_output_tokens", 4096)}}]), encoding="utf-8")
    model_metadata = run / "model-metadata.json"
    model_metadata.write_text(json.dumps({model: {
        "max_tokens": settings.get("max_input_tokens", 32768),
        "max_input_tokens": settings.get("max_input_tokens", 32768),
        "max_output_tokens": settings.get("max_output_tokens", 4096),
        **({"input_cost_per_token": 0, "output_cost_per_token": 0} if settings.get("provider") == "local" else {}),
        "litellm_provider": "openai", "mode": "chat"}}), encoding="utf-8")
    command = [str(PYTHON), "-m", "aider", "--config", str(HERE / "aider.yml"),
               "--env-file", str(HERE / "empty.env"), "--model-settings-file", str(model_settings),
               "--model-metadata-file", str(model_metadata), "--model", model,
               "--weak-model", model, "--editor-model", model,
               "--openai-api-base", settings["api_base"], "--edit-format", "diff",
               "--no-auto-commits", "--no-dirty-commits", "--no-check-update",
               "--no-analytics", "--no-show-release-notes", "--no-show-model-warnings",
               "--no-stream", "--no-pretty", "--no-suggest-shell-commands", "--no-auto-lint",
               "--map-tokens", "0", "--timeout", "120", "--yes-always",
               "--input-history-file", str(run / "input.history"),
               "--chat-history-file", str(run / "chat.md"), "--message-file", str(prompt)]
    if args.test_cmd:
        command.extend(["--test-cmd", args.test_cmd, "--auto-test"])
    else:
        command.append("--no-auto-test")
    git = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if git.returncode:
        command.append("--no-git")
    command.extend(str(path) for path in files)
    log = run / "aider.log"
    timed_out = False
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT)
        patch_task(run, process_pid=process.pid, phase="模型执行中")
        try:
            code = process.wait(timeout=task_timing.normalize(args.timeout) or None)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True)
            process.wait()
            code = 124
            timed_out = True
    log_text = log.read_text(encoding="utf-8", errors="replace")
    compact_lines = [line for line in log_text.splitlines() if line.startswith((
        "Tokens:", "Applied edit to ", "PASS:", "FAIL:", "Error:", "Warning:"))]
    result = {"exit_code": code, "timed_out": timed_out, "model": settings["model"],
              "project": str(project), "changed_selected_files": [
                  str(path) for path in files if before[str(path)] != digest(path)],
              "test_command": args.test_cmd, "log": str(log),
              "log_summary": "\n".join(compact_lines)[-1800:]}
    if code:
        result["error_excerpt"] = log_text[-2000:]
    (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    patch_task(run, status="cancelled" if (run / "cancelled.flag").exists() else ("completed" if code == 0 else "failed"),
               phase="已停止" if (run / "cancelled.flag").exists() else ("执行完成，等待验收" if code == 0 else "执行失败"),
               ended_at=now(), exit_code=code, changed_files=result["changed_selected_files"],
               summary=result["log_summary"], error=result.get("error_excerpt", ""))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        if globals().get("ACTIVE_RUN") and not (ACTIVE_RUN / "cancelled.flag").exists():
            patch_task(ACTIVE_RUN, status="failed", phase="执行失败", ended_at=now(), error=str(exc))
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
