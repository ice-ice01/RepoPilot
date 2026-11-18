"""Reconcile historical CI evidence with the latest synchronized run, without embeddings."""
from datetime import datetime, timezone
from uuid import UUID

from app.db.models import Repository, WorkflowRun


def run_order(run):
    # Creation orders separate runs; updated_at only orders attempts of the same run.
    value = run.created_at or run.updated_at or datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value, run.github_run_id or 0, run.run_attempt or 0


def run_scope(run):
    workflow = run.workflow_id or run.workflow_path or run.name
    return (str(run.repo_id), workflow, run.head_branch,
            run.event, tuple(sorted(run.pull_request_numbers or [])))


def has_known_scope(run):
    return bool((run.workflow_id or run.workflow_path) and
                (run.head_branch or run.pull_request_numbers))


def latest_workflow_runs(runs):
    runs = list(runs)
    known_names = {(str(run.repo_id), run.name) for run in runs if has_known_scope(run)}
    latest = {}
    for run in sorted(runs, key=run_order, reverse=True):
        # Unscoped legacy history is not an additional current workflow alongside
        # newly synchronized scoped records. Explicit history retrieval still keeps it.
        if not has_known_scope(run) and (str(run.repo_id), run.name) in known_names:
            continue
        latest.setdefault(run_scope(run), run)
    return sorted(latest.values(), key=run_order, reverse=True)


def latest_pr_runs(runs, pr):
    related = [run for run in runs if (
        pr.number in run.pull_request_numbers if run.pull_request_numbers else
        bool(run.head_branch and run.head_branch == pr.head_branch)
    )]
    return latest_workflow_runs(related)


def run_summary(run):
    return {
        "run_id": str(run.id), "github_run_id": run.github_run_id,
        "name": run.name, "workflow_id": run.workflow_id,
        "branch": run.head_branch, "head_sha": run.head_sha,
        "run_attempt": run.run_attempt, "status": run.status,
        "conclusion": run.conclusion, "html_url": run.html_url,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def ci_evidence_state(run, runs, synced_at=None):
    # Legacy rows with no scope cannot establish that another run supersedes them.
    known_scope = has_known_scope(run)
    candidates = [item for item in runs if run_scope(item) == run_scope(run)] if known_scope else [run]
    latest = max(candidates, key=run_order)
    completed = [item for item in candidates if item.status == "completed"]
    latest_completed = max(completed, key=run_order) if completed else None
    if run.conclusion == "success":
        state = "successful_rerun"
    elif latest.id != run.id and latest.status == "completed" and latest.conclusion == "success":
        state = "superseded_by_success"
    elif latest.id != run.id:
        state = "historical_failure"
    else:
        state = ("latest_failure" if run.conclusion == "failure" else "pending_rerun") if known_scope else "scope_unknown"
    note = f"历史 CI 证据：运行 {run.github_run_id or run.id}，当次结论 {run.conclusion or run.status}。"
    note += f" 工作流：{run.name}；分支：{run.head_branch or '未知'}；提交：{run.head_sha or '未知'}。"
    if state == "superseded_by_success":
        note += f" 同一工作流/分支范围后续运行 {latest.github_run_id or latest.id} 已通过；旧日志仅供故障经验参考，不能据此认定当前仍失败。"
    elif state == "successful_rerun":
        note += " 该运行已重跑成功；检索到的旧失败日志已失效。"
    else:
        note += f" 最新已同步运行结论：{latest.conclusion or latest.status}。"
        if not known_scope:
            note += " 缺少工作流/分支关联信息，无法确认当前状态，请重新同步仓库。"
        else:
            note += " 历史日志不能证明最新提交仍有相同故障。"
    note += " 此状态来自最近一次同步，不代表刚刚查询了 GitHub。"
    if latest.html_url:
        note += f" 最新运行链接：{latest.html_url}。"
    return {
        "ci_evidence_state": state, "ci_status_note": note,
        "latest_run": run_summary(latest),
        "latest_completed_run": run_summary(latest_completed) if latest_completed else None,
        "ci_synced_at": synced_at.isoformat() if synced_at else None,
    }


def annotate_ci_results(db, repo_id, items):
    ci_items = [item for item in items if item.get("source_type") == "workflow_run"]
    if not ci_items:
        return items
    repo_uuid = UUID(str(repo_id))
    runs = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_uuid).all()
    by_id = {str(run.id): run for run in runs}
    repo = db.get(Repository, repo_uuid)
    for item in ci_items:
        meta = dict(item.get("metadata") or {})
        run = by_id.get(str(item.get("source_id")))
        if run:
            meta.update(ci_evidence_state(run, runs, repo.last_sync_at if repo else None))
        else:
            meta.update(ci_evidence_state="source_unavailable", ci_status_note="历史 CI 来源已不可核对，无法确认当前状态。")
        item["metadata"] = meta
    return items
