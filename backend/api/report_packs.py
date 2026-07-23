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
