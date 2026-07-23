"""
映射工作台 API（HITL 人工确认映射）
契约见 docs/映射工作台与场景包设计方案.md §2.5：

- GET  任务映射清单（含 evidence 与源字段画像）
- POST confirm / modify / reject / needs-etl：单条映射处理（全挂鉴权 + 审计）
- POST confirm-all：全部确认 → 校验终态 → 任务恢复 queued，worker 断点续跑 codegen
- GET  mapping-assets：历史映射资产库
- 确认/修改后的映射沉淀进 mapping_assets（复用则 use_count+1）

依赖说明：field_mappings / mapping_assets 模型与 profiling_service 属范围B，
此处全部延迟导入；依赖未就绪时返回 503 而不影响主应用启动。
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from backend.api.deps import get_tenant
from backend.database import PlatformSessionLocal
from backend.services import task_service, audit_service

router = APIRouter(tags=["映射工作台"])

# 映射终态：confirm-all 放行所需的终态集合（unmapped/rejected 必须处理，见设计方案 §2.4）
TERMINAL_OK = ("confirmed", "modified", "needs_etl")
# 必须人工处理、否则阻断 confirm-all 的状态
BLOCKING_STATUS = ("unmapped", "rejected")


# ============================================
# 内部工具
# ============================================
def _models():
    """延迟加载范围B的模型（并行开发期间可能尚未就绪）"""
    try:
        from backend.models.field_mapping import FieldMapping
        from backend.models.mapping_asset import MappingAsset
        return FieldMapping, MappingAsset
    except Exception:
        raise HTTPException(status_code=503, detail="映射功能依赖（范围B模型）尚未就绪")


def _mapping_to_json(m, profile: Optional[dict] = None,
                     caliber_text: str = "",
                     candidates: Optional[list] = None) -> dict:
    """FieldMapping → API 响应（含 evidence、画像、口径提示与候选列表）"""
    return {
        "id": m.id,
        "task_id": m.task_id,
        "report_pack_id": m.report_pack_id,
        "target_field": m.target_field,
        "source_table": m.source_table,
        "source_field": m.source_field,
        "transform_rule": m.transform_rule,
        "confidence": m.confidence,
        "evidence": m.evidence or {},
        "profile": profile,  # 源字段画像（best-effort，失败为 None）
        "caliber_text": caliber_text or "",     # 口径提示（来自场景包 target_schema）
        "candidates": candidates or [],          # 候选源字段（[0]=当前选中，其余为备选）
        "status": m.status,
        "confirmed_by": m.confirmed_by,
        "confirmed_at": m.confirmed_at.isoformat() if m.confirmed_at else None,
    }


async def _load_pack_context(pack_ids) -> tuple:
    """加载场景包上下文：{(pack_id, target_field): caliber_text} 与 {pack_id: source_tables}

    caliber_text 不落 field_mappings，权威来源是场景包 target_schema；失败降级为空。
    """
    caliber_map: dict = {}
    pack_tables: dict = {}
    try:
        from backend.models.report_pack import ReportPack
        async with PlatformSessionLocal() as session:
            rows = (await session.execute(
                select(ReportPack).where(ReportPack.id.in_(list(pack_ids)))
            )).scalars().all()
        for pack in rows:
            pack_tables[pack.id] = pack.source_tables or []
            for spec in pack.target_schema or []:
                field = spec.get("field") if isinstance(spec, dict) else None
                if field:
                    caliber_map[(pack.id, field)] = spec.get("caliber_text") or ""
    except Exception:
        pass
    return caliber_map, pack_tables


async def _expand_candidate_pool(pack_tables: dict) -> list:
    """按场景包 source_tables 拉取源表 schema 并展开候选 [{table, column, ...}]

    候选列表不落库（推断时即时展开），此处按同一路径 best-effort 重建；
    任一表查询失败跳过该表，整体失败返回空（不影响清单主响应）。
    """
    try:
        from backend.mcp.database_mcp import DatabaseMCPService
        from backend.services.mapping_engine import MappingEngine
    except Exception:
        return []
    tables = sorted({t for ts in pack_tables.values() for t in (ts or [])})
    if not tables:
        return []
    db_mcp = DatabaseMCPService({"db_type": "sqlite_demo"})
    schemas = {}
    for table in tables:
        try:
            schemas[table] = await db_mcp.query_schema(table_name=table)
        except Exception:
            continue
    try:
        return MappingEngine._expand_candidates(schemas, tables)
    except Exception:
        return []


# 本地名称通道引擎（不触发 embedding 模型加载；embed_fn 仅为占位，名称通道不调用）
_local_name_engine = None


def _name_engine():
    global _local_name_engine
    if _local_name_engine is None:
        from backend.services.mapping_engine import MappingEngine
        _local_name_engine = MappingEngine(embed_fn=lambda text: [])
    return _local_name_engine


def _rank_candidates(item: dict, pool: list, limit: int = 4) -> list:
    """当前选中置顶 + 备选候选（按名称通道相似度排序，best-effort 重算）

    说明：field_mappings 只持久化最优候选的融合分，备选候选未落库；
    备选 confidence 为本地名称相似度重算分，仅供前端"其他候选"展示参考。
    """
    current = None
    if item.get("source_table") and item.get("source_field"):
        current = {"source_table": item["source_table"],
                   "source_field": item["source_field"],
                   "confidence": item.get("confidence") or 0.0}
    scored = []
    try:
        engine = _name_engine()
        for cand in pool:
            if current and cand["table"] == current["source_table"] \
                    and cand["column"] == current["source_field"]:
                continue
            score = engine._name_score(item.get("target_field") or "",
                                       item.get("caliber_text") or "",
                                       cand["column"])
            scored.append({"source_table": cand["table"],
                           "source_field": cand["column"],
                           "confidence": score})
    except Exception:
        scored = []
    scored.sort(key=lambda c: -c["confidence"])
    head = [current] if current else []
    return (head + scored)[:limit]


async def _load_profile(table: Optional[str], column: Optional[str]) -> Optional[dict]:
    """源字段画像（best-effort：profiling_service 未就绪或查询失败时返回 None）"""
    if not table or not column:
        return None
    try:
        from backend.services.profiling_service import ProfilingService
        # 默认走 SQLite 演示数据集只读通道（服务内部自带缓存与标识符白名单）
        return await ProfilingService().profile_column(table, column)
    except Exception:
        return None


async def _get_task_or_404(tenant_id: str, task_id: str) -> dict:
    """任务存在性与租户归属校验"""
    state = await task_service.get_task_state(task_id)
    if not state or state.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return state


async def _get_mapping_or_404(task_id: str, mapping_id: str):
    """单条映射查询（带任务归属校验）"""
    FieldMapping, _ = _models()
    async with PlatformSessionLocal() as session:
        m = await session.get(FieldMapping, mapping_id)
        if not m or m.task_id != task_id:
            raise HTTPException(status_code=404, detail="映射不存在")
        # 带出会话外使用所需字段（避免懒加载）
        return {
            "id": m.id, "task_id": m.task_id, "report_pack_id": m.report_pack_id,
            "target_field": m.target_field, "source_table": m.source_table,
            "source_field": m.source_field, "transform_rule": m.transform_rule,
            "status": m.status,
        }


async def _upsert_mapping_asset(report_pack_id: str, target_field: str,
                                source_table: Optional[str], source_field: Optional[str],
                                transform_rule: str, username: str):
    """确认的映射沉淀为历史映射资产；同键已存在则 use_count+1"""
    _, MappingAsset = _models()
    async with PlatformSessionLocal() as session:
        existing = (await session.execute(
            select(MappingAsset).where(
                MappingAsset.report_pack_id == report_pack_id,
                MappingAsset.target_field == target_field,
                MappingAsset.source_table == (source_table or ""),
                MappingAsset.source_field == (source_field or ""),
            )
        )).scalars().first()
        now = datetime.now()
        if existing:
            existing.use_count = (existing.use_count or 0) + 1
            existing.transform_rule = transform_rule
            existing.last_confirmed_by = username
            existing.last_confirmed_at = now
        else:
            session.add(MappingAsset(
                id=f"MA_{uuid.uuid4().hex[:12]}",
                report_pack_id=report_pack_id,
                target_field=target_field,
                source_table=source_table or "",
                source_field=source_field or "",
                transform_rule=transform_rule,
                use_count=1,
                last_confirmed_by=username,
                last_confirmed_at=now,
            ))
        await session.commit()


async def _apply_mapping_update(mapping_id: str, new_status: str, username: str,
                                source_table=None, source_field=None, transform_rule=None):
    """单条映射状态/内容更新，返回更新后的字段快照"""
    FieldMapping, _ = _models()
    async with PlatformSessionLocal() as session:
        m = await session.get(FieldMapping, mapping_id)
        if not m:
            raise HTTPException(status_code=404, detail="映射不存在")
        if source_table is not None:
            m.source_table = source_table
        if source_field is not None:
            m.source_field = source_field
        if transform_rule is not None:
            m.transform_rule = transform_rule
        m.status = new_status
        m.confirmed_by = username
        m.confirmed_at = datetime.now()
        if hasattr(m, "updated_at"):
            m.updated_at = datetime.now()
        await session.commit()
        return {
            "id": m.id, "report_pack_id": m.report_pack_id, "target_field": m.target_field,
            "source_table": m.source_table, "source_field": m.source_field,
            "transform_rule": m.transform_rule, "status": m.status,
        }


# ============================================
# 映射清单
# ============================================
@router.get("/tenants/{tenant_id}/tasks/{task_id}/mappings")
async def list_task_mappings(tenant_id: str, task_id: str,
                             tenant: dict = Depends(get_tenant)):
    """任务映射清单（含五通道 evidence 与源字段画像，供映射工作台渲染）"""
    FieldMapping, _ = _models()
    await _get_task_or_404(tenant_id, task_id)
    async with PlatformSessionLocal() as session:
        rows = (await session.execute(
            select(FieldMapping).where(FieldMapping.task_id == task_id)
        )).scalars().all()
        items = [_mapping_to_json(m) for m in rows]
    # 口径提示（场景包 target_schema）+ 候选列表（best-effort 重建，失败为空）
    pack_ids = {i["report_pack_id"] for i in items if i["report_pack_id"]}
    caliber_map, pack_tables = await _load_pack_context(pack_ids)
    pool = await _expand_candidate_pool(pack_tables)
    for item in items:
        item["caliber_text"] = caliber_map.get(
            (item["report_pack_id"], item["target_field"]), "")
        item["candidates"] = _rank_candidates(item, pool)
    # 画像逐个 best-effort 补齐（候选源表采样，不进 DB 会话）
    for item in items:
        item["profile"] = await _load_profile(item["source_table"], item["source_field"])
    confirmed = sum(1 for i in items if i["status"] in TERMINAL_OK)
    return {
        "task_id": task_id,
        "total": len(items),
        "confirmed": confirmed,
        "mappings": items,
    }


# ============================================
# 单条映射操作
# ============================================
@router.post("/tenants/{tenant_id}/tasks/{task_id}/mappings/{mapping_id}/confirm")
async def confirm_mapping(tenant_id: str, task_id: str, mapping_id: str,
                          request: Request, body: dict = None,
                          tenant: dict = Depends(get_tenant)):
    """确认单条映射（可附带修正后的 transform_rule）→ 沉淀 mapping_assets"""
    user = getattr(request.state, "user", None) or {}
    await _get_task_or_404(tenant_id, task_id)
    await _get_mapping_or_404(task_id, mapping_id)

    body = body or {}
    updated = await _apply_mapping_update(
        mapping_id, "confirmed", user.get("username", ""),
        transform_rule=body.get("transform_rule"))
    await _upsert_mapping_asset(
        updated["report_pack_id"], updated["target_field"],
        updated["source_table"], updated["source_field"],
        updated["transform_rule"] or "DIRECT", user.get("username", ""))

    await audit_service.write_audit(
        action="mapping.confirm", tenant_id=tenant_id, user=user or None,
        resource=mapping_id,
        detail={"task_id": task_id, "target_field": updated["target_field"]},
        ip=request.client.host if request.client else None)
    return {"mapping_id": mapping_id, "status": "confirmed", "message": "映射已确认"}


@router.post("/tenants/{tenant_id}/tasks/{task_id}/mappings/{mapping_id}/modify")
async def modify_mapping(tenant_id: str, task_id: str, mapping_id: str,
                         request: Request, body: dict,
                         tenant: dict = Depends(get_tenant)):
    """修改映射（指定新的源表/源字段/转换规则）→ 沉淀 mapping_assets"""
    user = getattr(request.state, "user", None) or {}
    await _get_task_or_404(tenant_id, task_id)
    await _get_mapping_or_404(task_id, mapping_id)

    if not body.get("source_table") or not body.get("source_field"):
        raise HTTPException(status_code=422, detail="modify 需要提供 source_table 与 source_field")
    updated = await _apply_mapping_update(
        mapping_id, "modified", user.get("username", ""),
        source_table=body["source_table"], source_field=body["source_field"],
        transform_rule=body.get("transform_rule") or "DIRECT")
    await _upsert_mapping_asset(
        updated["report_pack_id"], updated["target_field"],
        updated["source_table"], updated["source_field"],
        updated["transform_rule"] or "DIRECT", user.get("username", ""))

    await audit_service.write_audit(
        action="mapping.modify", tenant_id=tenant_id, user=user or None,
        resource=mapping_id,
        detail={"task_id": task_id, "target_field": updated["target_field"],
                "source_table": updated["source_table"], "source_field": updated["source_field"]},
        ip=request.client.host if request.client else None)
    return {"mapping_id": mapping_id, "status": "modified", "message": "映射已修改"}


@router.post("/tenants/{tenant_id}/tasks/{task_id}/mappings/{mapping_id}/reject")
async def reject_mapping(tenant_id: str, task_id: str, mapping_id: str,
                         request: Request,
                         tenant: dict = Depends(get_tenant)):
    """拒绝 AI 推断（阻断态：confirm-all 前必须另行处理）"""
    user = getattr(request.state, "user", None) or {}
    await _get_task_or_404(tenant_id, task_id)
    await _get_mapping_or_404(task_id, mapping_id)
    updated = await _apply_mapping_update(mapping_id, "rejected", user.get("username", ""))

    await audit_service.write_audit(
        action="mapping.reject", tenant_id=tenant_id, user=user or None,
        resource=mapping_id,
        detail={"task_id": task_id, "target_field": updated["target_field"]},
        ip=request.client.host if request.client else None)
    return {"mapping_id": mapping_id, "status": "rejected", "message": "映射已拒绝"}


@router.post("/tenants/{tenant_id}/tasks/{task_id}/mappings/{mapping_id}/needs-etl")
async def needs_etl_mapping(tenant_id: str, task_id: str, mapping_id: str,
                            request: Request, body: dict = None,
                            tenant: dict = Depends(get_tenant)):
    """标记需 ETL 加工（终态，不阻断 confirm-all；可附带加工说明作为 transform_rule）"""
    user = getattr(request.state, "user", None) or {}
    await _get_task_or_404(tenant_id, task_id)
    await _get_mapping_or_404(task_id, mapping_id)
    body = body or {}
    updated = await _apply_mapping_update(
        mapping_id, "needs_etl", user.get("username", ""),
        transform_rule=body.get("transform_rule"))

    await audit_service.write_audit(
        action="mapping.needs_etl", tenant_id=tenant_id, user=user or None,
        resource=mapping_id,
        detail={"task_id": task_id, "target_field": updated["target_field"]},
        ip=request.client.host if request.client else None)
    return {"mapping_id": mapping_id, "status": "needs_etl", "message": "已标记需 ETL 加工"}


# ============================================
# 全部确认 → 任务恢复
# ============================================
@router.post("/tenants/{tenant_id}/tasks/{task_id}/mappings/confirm-all")
async def confirm_all_mappings(tenant_id: str, task_id: str,
                               request: Request,
                               tenant: dict = Depends(get_tenant)):
    """全部确认并恢复执行：

    1. 剩余 ai_inferred 映射自动置为 confirmed 并沉淀 mapping_assets；
    2. 校验全部 target_field 有终态（unmapped/rejected 必须处理，否则 409）；
    3. 任务置回 queued（断点 checkpoint.next=["codegen"] 保留），worker 断点续跑。
    """
    FieldMapping, _ = _models()
    user = getattr(request.state, "user", None) or {}
    state = await _get_task_or_404(tenant_id, task_id)

    if state.get("status") != "waiting_confirmation":
        raise HTTPException(status_code=409,
                            detail=f"任务状态为 {state.get('status')}，仅 waiting_confirmation 可确认恢复")

    username = user.get("username", "")
    auto_confirmed = 0
    async with PlatformSessionLocal() as session:
        rows = (await session.execute(
            select(FieldMapping).where(FieldMapping.task_id == task_id)
        )).scalars().all()
        if not rows:
            raise HTTPException(status_code=409, detail="任务无映射记录，无法确认恢复")

        # 阻断校验：unmapped / rejected 必须先处理
        blocking = [
            {"id": m.id, "target_field": m.target_field, "status": m.status}
            for m in rows if m.status in BLOCKING_STATUS
        ]
        if blocking:
            raise HTTPException(
                status_code=409,
                detail={"message": "存在未处理的 unmapped/rejected 映射，请先处理",
                        "blocking": blocking})

        # 剩余 ai_inferred 自动确认 + 资产沉淀
        now = datetime.now()
        for m in rows:
            if m.status == "ai_inferred":
                m.status = "confirmed"
                m.confirmed_by = username
                m.confirmed_at = now
                if hasattr(m, "updated_at"):
                    m.updated_at = now
                auto_confirmed += 1
        # commit 前先取快照（commit 后属性过期，避免二次查询）
        snapshots = [
            {"report_pack_id": m.report_pack_id, "target_field": m.target_field,
             "source_table": m.source_table, "source_field": m.source_field,
             "transform_rule": m.transform_rule or "DIRECT"}
            for m in rows
        ]
        await session.commit()

    for s in snapshots:
        await _upsert_mapping_asset(s["report_pack_id"], s["target_field"],
                                    s["source_table"], s["source_field"],
                                    s["transform_rule"], username)

    # 恢复任务：queued + 断点保留（worker 从 codegen 续跑，复用 D4-5 机制）
    checkpoint = state.get("checkpoint") or {}
    state["status"] = "queued"
    state["checkpoint"] = {
        "completed": checkpoint.get("completed", ["regulation_parser"]),
        "next": checkpoint.get("next") or ["codegen"],
    }
    await task_service.save_task_state(state)

    await audit_service.write_audit(
        action="mapping.confirm_all", tenant_id=tenant_id, user=user or None,
        resource=task_id,
        detail={"total": len(snapshots), "auto_confirmed": auto_confirmed},
        ip=request.client.host if request.client else None)
    return {
        "task_id": task_id,
        "status": "queued",
        "total_mappings": len(snapshots),
        "auto_confirmed": auto_confirmed,
        "message": "映射已全部确认，任务已恢复排队，将从 codegen 断点续跑",
    }


# ============================================
# 历史映射资产库
# ============================================
@router.get("/tenants/{tenant_id}/mapping-assets")
async def list_mapping_assets(tenant_id: str, report_pack_id: Optional[str] = None,
                              tenant: dict = Depends(get_tenant)):
    """历史映射资产库（可按场景包过滤，按复用次数倒序）"""
    _, MappingAsset = _models()
    async with PlatformSessionLocal() as session:
        stmt = select(MappingAsset)
        if report_pack_id:
            stmt = stmt.where(MappingAsset.report_pack_id == report_pack_id)
        stmt = stmt.order_by(MappingAsset.use_count.desc())
        rows = (await session.execute(stmt)).scalars().all()
    return {
        "total": len(rows),
        "assets": [
            {
                "id": r.id,
                "report_pack_id": r.report_pack_id,
                "target_field": r.target_field,
                "source_table": r.source_table,
                "source_field": r.source_field,
                "transform_rule": r.transform_rule,
                "use_count": r.use_count,
                "last_confirmed_by": r.last_confirmed_by,
                "last_confirmed_at": r.last_confirmed_at.isoformat() if r.last_confirmed_at else None,
            }
            for r in rows
        ],
    }
