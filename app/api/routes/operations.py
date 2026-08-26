"""运行状态和显式维护 Controller。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.contracts.schemas import IndexStatusOut, UsageOut
from app.core.container import operations_application_service
from app.db import get_db

router = APIRouter(tags=["operations"])


@router.get("/index/status", response_model=IndexStatusOut)
def index_status(db: Session = Depends(get_db)) -> dict:
    """返回搜索后端健康状态和持久化一致性指标。"""

    return operations_application_service().index_status(db)


@router.post("/admin/indexes/reconcile")
def reconcile_index(repair: bool = False, db: Session = Depends(get_db)) -> dict:
    """比较事实表与搜索投影，并按需修复漂移。"""

    return operations_application_service().reconcile_index(db, repair=repair)


@router.get("/models/usage", response_model=list[UsageOut])
def model_usage(db: Session = Depends(get_db)) -> list[dict]:
    """返回本地路由配额估算；最终账单仍以供应商统计为准。"""

    return operations_application_service().model_usage(db)


@router.post("/internal/diagnostics/qwen")
def diagnostics_notice() -> dict:
    """阻止带付费凭证的诊断能力经 HTTP 暴露。"""

    return {
        "status": "disabled-over-http",
        "message": (
            "Run `qwen-diagnostics` locally so credentials and paid diagnostics "
            "are not exposed via HTTP."
        ),
    }
