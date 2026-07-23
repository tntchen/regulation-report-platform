"""
报送台账服务
- generate_ledger：按 active 场景包批量生成当月台账（幂等：唯一约束 (tenant, pack, period)，已存在跳过）
- 截止期规则：月报 = 次月 5 日 23:59:59（deadline_day 可配）
- 状态机：pending →（bind_task）→ in_progress →（submit）→ submitted；
  超 deadline 且未 submitted → overdue（查询时懒计算，不写库）
"""

import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.database import PlatformSessionLocal
from backend.models.report_pack import ReportPack
from backend.models.submission_ledger import SubmissionLedger
from backend.models.task import Task
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 月报截止日规则：次月第 N 天 23:59:59（可配；Demo 深度内置常量）
MONTHLY_DEADLINE_DAY = 5

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

VALID_STATUS = {"pending", "in_progress", "submitted", "overdue"}


def validate_period(period: str) -> bool:
    """period 必须是 YYYY-MM 格式"""
    return bool(period and _PERIOD_RE.match(period))


def compute_deadline(period: str, deadline_day: int = MONTHLY_DEADLINE_DAY) -> datetime:
    """月报截止期：period 次月 deadline_day 日 23:59:59（12 月跨年到次年 1 月）"""
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return datetime(year, month, min(deadline_day, 28) if deadline_day > 28 else deadline_day,
                    23, 59, 59)


def _lazy_status(row: SubmissionLedger, now: Optional[datetime] = None) -> str:
    """懒计算逾期：当前时间超 deadline 且未 submitted → overdue"""
    status = row.status or "pending"
    if status == "submitted":
        return status
    now = now or datetime.now()
    if row.deadline and now > row.deadline:
        return "overdue"
    return status


def _entry_to_dict(row: SubmissionLedger, now: Optional[datetime] = None) -> Dict[str, Any]:
    """ORM 行 → 台账条目字典（含懒计算 status 与 days_left）"""
    now = now or datetime.now()
    days_left = (row.deadline - now).days if row.deadline else None
    return {
        "id": row.id,
        "report_pack_id": row.report_pack_id,
        "report_name": row.report_name,
        "period": row.period,
        "deadline": row.deadline.isoformat() if row.deadline else None,
        "status": _lazy_status(row, now),
        "task_id": row.task_id,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "days_left": days_left,
    }


async def generate_ledger(tenant_id: str, period: str,
                          deadline_day: int = MONTHLY_DEADLINE_DAY) -> Dict[str, Any]:
    """按全部 active 场景包批量生成当月台账（幂等：已存在条目跳过，重复调用返回 skipped）"""
    if not validate_period(period):
        raise ValueError(f"period 格式非法: {period!r}，应为 YYYY-MM")

    deadline = compute_deadline(period, deadline_day)
    created, skipped = 0, 0
    async with PlatformSessionLocal() as session:
        packs = (await session.execute(
            select(ReportPack).where(ReportPack.status == "active").order_by(ReportPack.id)
        )).scalars().all()

        for pack in packs:
            # 幂等检查：同租户同包同期间已存在则跳过
            exists = (await session.execute(
                select(SubmissionLedger.id).where(
                    SubmissionLedger.tenant_id == tenant_id,
                    SubmissionLedger.report_pack_id == pack.id,
                    SubmissionLedger.period == period,
                )
            )).first()
            if exists:
                skipped += 1
                continue
            row = SubmissionLedger(
                id=uuid.uuid4().hex[:16],
                tenant_id=tenant_id,
                report_pack_id=pack.id,
                report_name=pack.report_name,
                period=period,
                deadline=deadline,
                status="pending",
            )
            session.add(row)
            try:
                await session.flush()
                created += 1
            except IntegrityError:
                # 并发重复生成兜底：唯一约束冲突视为已存在
                await session.rollback()
                skipped += 1
        await session.commit()

    logger.info("台账生成 tenant=%s period=%s created=%d skipped=%d",
                tenant_id, period, created, skipped)
    return {"period": period, "created": created, "skipped": skipped}


async def list_ledger(tenant_id: str, period: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出台账条目（可选按 period 过滤），status 懒计算 overdue"""
    async with PlatformSessionLocal() as session:
        stmt = select(SubmissionLedger).where(
            SubmissionLedger.tenant_id == tenant_id
        ).order_by(SubmissionLedger.report_pack_id)
        if period:
            stmt = stmt.where(SubmissionLedger.period == period)
        rows = (await session.execute(stmt)).scalars().all()
    now = datetime.now()
    return [_entry_to_dict(r, now) for r in rows]


async def get_entry(entry_id: str, tenant_id: str) -> Optional[SubmissionLedger]:
    """按 ID + 租户取台账条目（越租户不可见）；不存在返回 None"""
    async with PlatformSessionLocal() as session:
        row = await session.get(SubmissionLedger, entry_id)
        if not row or row.tenant_id != tenant_id:
            return None
        # detach 前取字段，避免跨 session 懒加载问题（无关联对象，直接返回行即可）
        await session.refresh(row)
        return row


async def bind_task(entry_id: str, tenant_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    """绑定生成任务：任务必须存在且属于同租户；pending 条目升级为 in_progress"""
    async with PlatformSessionLocal() as session:
        row = await session.get(SubmissionLedger, entry_id)
        if not row or row.tenant_id != tenant_id:
            return None
        if row.status == "submitted":
            raise ValueError("已报送条目不可再绑定任务")
        task = await session.get(Task, task_id)
        if not task or task.tenant_id != tenant_id:
            raise LookupError(f"任务 {task_id} 不存在或不属于该租户")
        row.task_id = task_id
        if row.status == "pending":
            row.status = "in_progress"
        await session.commit()
        return _entry_to_dict(row)


async def submit_entry(entry_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    """报送完成：置 submitted + submitted_at；重复报送幂等返回当前状态"""
    async with PlatformSessionLocal() as session:
        row = await session.get(SubmissionLedger, entry_id)
        if not row or row.tenant_id != tenant_id:
            return None
        if row.status != "submitted":
            row.status = "submitted"
            row.submitted_at = datetime.now()
            await session.commit()
        return _entry_to_dict(row)
