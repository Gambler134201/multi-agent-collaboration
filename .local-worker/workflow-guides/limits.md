# Authorization and optional limits

Global `model_scope` restricts execution models independently of these limits: `paid_only` does not grant paid permission; `local_only` forbids paid candidates. Scope changes apply to new dispatches and queued task startup, without cancelling or switching an already running task. Preserve the user's selected scope and all existing permission/limit switches.

Monetary and request-count ceilings are optional. enforce_paid_budget=false disables task/day money caps; enforce_model_call_limit=false disables the call-count ceiling. Task total runtime is unlimited by default (timeout=0); do not automatically add a positive --timeout merely because a task is long. Only an explicitly chosen positive timeout imposes a task deadline. Connection/request inactivity handling and manual cancellation remain, as do paid authorization, observed costs and balance checks. Do not silently re-enable or raise a user's limits.
