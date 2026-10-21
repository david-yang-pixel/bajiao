from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class AnalysisUnavailable(RuntimeError):
    """Safe, user-facing diagnostic without provider bodies or credentials."""


class AnalysisBusy(RuntimeError):
    pass


class SwingJudgment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selected: StrictBool
    reason: str = Field(min_length=8, max_length=1200)
    invalidation: str = Field(min_length=8, max_length=600)
    risks: list[Annotated[str, Field(min_length=2, max_length=400)]] = Field(min_length=1, max_length=8)
    source_ids: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(min_length=1, max_length=20)
