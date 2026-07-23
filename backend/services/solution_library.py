"""
历史方案库服务（范围 D）

职责：
1) 任务 completed 时沉淀方案案例（record_case_from_state）：
   从任务 outputs 提取映射终态摘要 + 门禁结果 + 勾稽结果，落库 solution_cases。
2) 相似方案推荐（recommend）：
   同租户成功案例，同场景包相似度 1.0 / 同类型报表 0.6，
   按相似度降序 + 创建时间倒序返回 Top-N。

Demo 深度：纯 SQL 查询 + 规则相似度，足够单机演示。
"""

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.database import PlatformSessionLocal
from backend.models.solution_case import SolutionCase
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 相似度打分：同场景包 / 同类型报表（report_type 相同）
SIMILARITY_SAME_PACK = 1.0
SIMILARITY_SAME_REPORT_TYPE = 0.6

# 默认场景包（与 report_pack_service 对齐，避免循环依赖，冗余一个常量）
DEFAULT_PACK_ID = "G01"


def _extract_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    """从任务终态提取案例摘要：映射终态 + 门禁结果 + 勾稽结果"""
    outputs = state.get("outputs") or {}
    summary: Dict[str, Any] = {}

    # 报表类型 / 场景包（Agent 1 输出优先，回退任务配置）
    reg = outputs.get("regulation_parser") or {}
    config = state.get("report_config") or {}
    summary["report_pack_id"] = reg.get("report_pack_id") or config.get("report_pack_id") or DEFAULT_PACK_ID
    summary["report_type"] = config.get("report_type") or ""
    summary["report_code"] = config.get("report_code") or ""

    # 映射终态摘要：映射建议数量与目标表
    suggestions = reg.get("mapping_suggestions") or []
    summary["mapping"] = {
        "target_table": config.get("target_table") or reg.get("target_table") or "",
        "suggestion_count": len(suggestions),
        "high_confidence_count": sum(1 for m in suggestions
                                     if isinstance(m, dict) and (m.get("confidence") or 0) >= 0.85),
    }

    # 门禁结果（Agent 3）
    gate = outputs.get("quality_gate") or {}
    summary["gate_result"] = gate.get("gate_result", "")

    # 测试验证（Agent 4）
    verify = outputs.get("test_verify") or {}
    summary["test_verify"] = {
        "critical_fail": bool(verify.get("critical_fail", False)),
        "pass_count": verify.get("pass_count"),
    }

    # 勾稽结果（Agent 5 数字孪生）
    twin = outputs.get("digital_twin") or {}
    recon = twin.get("reconciliation") or twin.get("reconciliation_result") or {}
    summary["reconciliation"] = recon if isinstance(recon, dict) else {}

    return summary


async def record_case_from_state(state: Dict[str, Any]) -> Optional[str]:
    """任务 completed 时沉淀方案案例；同一任务只沉淀一次（幂等）。
    返回案例 ID；state 信息不足时返回 None。"""
    if (state or {}).get("status") != "completed":
        return None
    task_id = state.get("task_id")
    tenant_id = state.get("tenant_id")
    if not task_id or not tenant_id:
        return None

    summary = _extract_summary(state)

    async with PlatformSessionLocal() as session:
        existing = (await session.execute(
            select(SolutionCase).where(SolutionCase.task_id == task_id)
        )).scalars().first()
        if existing:
            return existing.id

        case = SolutionCase(
            id=f"SC_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            report_pack_id=summary.get("report_pack_id") or DEFAULT_PACK_ID,
            task_id=task_id,
            summary=summary,
            status="completed",
            created_by=(state.get("report_config") or {}).get("created_by") or "system",
        )
        session.add(case)
        await session.commit()
        logger.info("方案案例沉淀: 任务 %s → 案例 %s (pack=%s)", task_id, case.id, case.report_pack_id)
        return case.id


def _case_to_dict(case: SolutionCase, similarity: float) -> Dict[str, Any]:
    """ORM 行 → 推荐结果字典（API 契约字段）"""
    return {
        "task_id": case.task_id,
        "report_pack_id": case.report_pack_id,
        "status": case.status,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "similarity": similarity,
        "summary": case.summary or {},
    }


async def recommend(tenant_id: str, report_pack_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """推荐相似历史方案

    同租户成功案例中：同场景包 → 相似度 1.0；同类型报表（report_type 相同）→ 0.6；
    其余不推荐。按相似度降序、创建时间倒序，取 Top-N。
    """
    async with PlatformSessionLocal() as session:
        rows = (await session.execute(
            select(SolutionCase).where(
                SolutionCase.tenant_id == tenant_id,
                SolutionCase.status == "completed",
            ).order_by(SolutionCase.created_at.desc())
        )).scalars().all()

    # 目标包类型：查不到包时只推荐同包案例
    target_type = ""
    try:
        from backend.services import report_pack_service
        pack = await report_pack_service.get_pack_safe(report_pack_id)
        target_type = (pack or {}).get("report_type") or ""
    except Exception as e:
        logger.warning("读取场景包 %s 失败，仅推荐同包案例: %s", report_pack_id, e)

    scored: List[Dict[str, Any]] = []
    for case in rows:
        if case.report_pack_id == report_pack_id:
            scored.append(_case_to_dict(case, SIMILARITY_SAME_PACK))
        elif target_type and (case.summary or {}).get("report_type") == target_type:
            scored.append(_case_to_dict(case, SIMILARITY_SAME_REPORT_TYPE))

    # 相似度降序 + 创建时间倒序（ISO 字符串可直接比较）
    scored.sort(key=lambda x: (x["similarity"], x["created_at"] or ""), reverse=True)
    return scored[:limit]
