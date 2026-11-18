"""独立的教学 API；只处理内存数据，不挂到正式应用、不使用生产配置。"""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from python_basics import classify_issue


app = FastAPI(title="Python 学习 API")


class AnalyzeRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    labels: list[str] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    category: Literal["Bug", "Documentation", "Feature"]
    title: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    # 这里没有异步 I/O，用普通 def 即可。
    return AnalyzeResponse(
        category=classify_issue(payload.title, payload.labels),
        title=payload.title,
    )
