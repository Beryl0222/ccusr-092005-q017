"""校园口腔筛查闭环后端。"""

from .models import (
    CaseStatus,
    DentitionStage,
    FeedbackCategory,
    Guardian,
    GuardianChoice,
    QueueKind,
)
from .service import ScreeningLoopService
from .views import guardian_view, health_coverage_view, school_view

__all__ = [
    "CaseStatus",
    "DentitionStage",
    "FeedbackCategory",
    "Guardian",
    "GuardianChoice",
    "QueueKind",
    "ScreeningLoopService",
    "WalkthroughResult",
    "guardian_view",
    "health_coverage_view",
    "run_seed_walkthrough",
    "school_view",
]


def __getattr__(name: str):
    # walkthrough 延迟导入，避免 python3 -m screening.walkthrough 双重加载。
    if name in ("WalkthroughResult", "run_seed_walkthrough"):
        from . import walkthrough

        return getattr(walkthrough, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
