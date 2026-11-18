from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.analysis import CIDebugOutput


class WorkflowRunResponse(BaseModel):
    id: UUID
    repo_id: UUID
    github_run_id: int | None = None
    workflow_id: int | None = None
    workflow_path: str | None = None
    head_branch: str | None = None
    head_sha: str | None = None
    event: str | None = None
    run_attempt: int | None = None
    pull_request_numbers: list[int] | None = None
    name: str
    status: str
    conclusion: str | None = None
    html_url: str | None = None
    jobs: list[dict] = []
    logs_text: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class CIAnalysisResponse(BaseModel):
    analysis_id: UUID | str
    result: CIDebugOutput
