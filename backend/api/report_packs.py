"""
场景包管理 API
GET 列表/详情：租户成员可读；POST/PUT：仅 admin，写审计（report_pack.create/update）。
路由前缀 /v1/tenants/{tenant_id}/report-packs（由协调者在 main.py 统一注册挂载）。
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.deps import get_tenant, get_current_user
from backend.services import report_pack_service, audit_service

# 独立导出，前缀由路由注册方统一指定
report_packs_router = APIRouter(tags=["场景包管理"])


def _require_admin(user: dict):
    """场景包写操作仅 admin 角色（与租户管理一致的 Demo 简化 RBAC）"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅 admin 角色可执行该操作")


@report_packs_router.get("/tenants/{tenant_id}/report-packs")
async def list_report_packs(tenant_id: str, status: str = None,
                            tenant: dict = Depends(get_tenant)):
    """列出场景包（租户成员可读；status 可过滤 active/draft/disabled）"""
    packs = await report_pack_service.list_packs(status=status)
    return {
        "total": len(packs),
        "report_packs": [
            {
                "id": p["id"],
                "report_name": p["report_name"],
                "report_type": p["report_type"],
                "target_table": p["target_table"],
                "status": p["status"],
                "source_tables": p["source_tables"],
                "trap_refs": p["trap_refs"],
            }
            for p in packs
        ],
    }


@report_packs_router.get("/tenants/{tenant_id}/report-packs/{pack_id}")
async def get_report_pack(tenant_id: str, pack_id: str,
                          tenant: dict = Depends(get_tenant)):
    """场景包详情（含目标结构/勾稽规则/检索关键词）"""
    pack = await report_pack_service.get_pack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="场景包不存在")
    return pack


@report_packs_router.get("/tenants/{tenant_id}/report-packs/{pack_id}/profile")
async def profile_pack_source_table(tenant_id: str, pack_id: str, table: str,
                                    request: Request,
                                    tenant: dict = Depends(get_tenant),
                                    current_user: dict = Depends(get_current_user)):
    """数据探查：对场景包指定源表执行全字段画像（租户成员可读）

    - table 必须在包 source_tables 白名单内（防任意表探测），否则 400
    - 画像复用 profiling_service.profile_column（只读通道 + 标识符白名单 + 缓存）
    - 成功画像写审计 report_pack.profile
    """
    pack = await report_pack_service.get_pack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="场景包不存在")

    if not table or table not in (pack.get("source_tables") or []):
        raise HTTPException(
            status_code=400,
            detail=f"表 {table!r} 不在场景包 {pack_id} 的源表白名单内",
        )

    # 表结构（拿列名与数据类型），画像逐列聚合
    from backend.mcp.database_mcp import DatabaseMCPService
    from backend.services.profiling_service import ProfilingService

    db_mcp = DatabaseMCPService({"db_type": "sqlite_demo"})
    schema = await db_mcp.query_schema(table)
    profiling = ProfilingService(db_mcp)

    columns = []
    for col in schema["columns"]:
        p = await profiling.profile_column(table, col["column_name"])
        if p.get("error"):
            raise HTTPException(status_code=502,
                                detail=f"字段 {col['column_name']} 画像失败: {p['error']}")
        columns.append({
            "column_name": col["column_name"],
            "data_type": col["data_type"],
            "null_rate": p["null_rate"],
            "distinct_count": p["distinct_count"],
            "sample_values": p["sample_values"],
            "format_pattern": p["format_pattern"],
            "enum_values": p["enum_values"],
            "total_rows": p["total_rows"],
        })

    await audit_service.write_audit(
        action="report_pack.profile",
        tenant_id=tenant_id,
        user=current_user,
        resource=pack_id,
        detail={"table": table, "column_count": len(columns)},
        ip=request.client.host if request.client else None,
    )

    return {"table": table, "columns": columns}


@report_packs_router.post("/tenants/{tenant_id}/report-packs")
async def create_report_pack(tenant_id: str, pack_data: dict, request: Request,
                             tenant: dict = Depends(get_tenant),
                             current_user: dict = Depends(get_current_user)):
    """创建场景包（admin）；ID 冲突 409"""
    _require_admin(current_user)

    for field in ("id", "report_name", "report_type", "target_table"):
        if not pack_data.get(field):
            raise HTTPException(status_code=422, detail=f"缺少必填字段: {field}")

    row = await report_pack_service.create_pack(pack_data,
                                                created_by=current_user.get("username"))
    if not row:
        raise HTTPException(status_code=409, detail=f"场景包 {pack_data['id']} 已存在")

    await audit_service.write_audit(
        action="report_pack.create",
        tenant_id=tenant_id,
        user=current_user,
        resource=row.id,
        detail={"report_name": row.report_name, "report_type": row.report_type,
                "target_table": row.target_table},
        ip=request.client.host if request.client else None,
    )

    return await report_pack_service.get_pack(row.id)


@report_packs_router.put("/tenants/{tenant_id}/report-packs/{pack_id}")
async def update_report_pack(tenant_id: str, pack_id: str, updates: dict, request: Request,
                             tenant: dict = Depends(get_tenant),
                             current_user: dict = Depends(get_current_user)):
    """更新场景包（admin，部分字段更新）；不存在 404"""
    _require_admin(current_user)

    if "id" in updates and updates["id"] != pack_id:
        raise HTTPException(status_code=422, detail="场景包 ID 不可修改")

    row = await report_pack_service.update_pack(pack_id, updates)
    if not row:
        raise HTTPException(status_code=404, detail="场景包不存在")

    await audit_service.write_audit(
        action="report_pack.update",
        tenant_id=tenant_id,
        user=current_user,
        resource=pack_id,
        detail={"updated_fields": sorted(k for k in updates.keys() if k != "id")},
        ip=request.client.host if request.client else None,
    )

    return await report_pack_service.get_pack(pack_id)
