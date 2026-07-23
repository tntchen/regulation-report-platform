"""
监管接口文件导出 API（范围B：监管接口文件输出）
- POST /tenants/{tid}/tasks/{task_id}/export-interface-file  生成 TXT/XML 接口文件（审计 task.export）
- GET  /tenants/{tid}/tasks/{task_id}/exports                已生成文件列表
- GET  /tenants/{tid}/tasks/{task_id}/exports/{file_name}    文件下载（文件名白名单防穿越）
任务非 completed → 409；路由前缀 /v1（由 main.py 注册挂载）。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from backend.api.deps import get_tenant, get_current_user
from backend.services import audit_service, task_service, interface_file_service

# 独立导出，前缀由路由注册方统一指定
export_router = APIRouter(tags=["监管接口文件"])

# 下载媒体类型映射（按导出格式）
_MEDIA_TYPES = {"txt": "text/plain; charset=utf-8", "xml": "application/xml; charset=utf-8"}


async def _load_task_checked(tenant_id: str, task_id: str) -> dict:
    """任务归属与状态校验：不存在/非本租户 404；非 completed 409"""
    task_state = await task_service.get_task_state(task_id)
    if not task_state or task_state.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task_state.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task_state.get('status')}，仅 completed 任务可导出接口文件",
        )
    return task_state


@export_router.post("/tenants/{tenant_id}/tasks/{task_id}/export-interface-file")
async def export_interface_file(tenant_id: str, task_id: str, payload: dict,
                                request: Request,
                                tenant: dict = Depends(get_tenant),
                                current_user: dict = Depends(get_current_user)):
    """生成监管接口文件（TXT/XML），写审计 task.export

    入参 {format: txt|xml}；返回 {file_name, format, row_count, preview}
    """
    task_state = await _load_task_checked(tenant_id, task_id)

    fmt = (payload or {}).get("format", "txt")
    try:
        result = await interface_file_service.generate_interface_file(
            task_id, task_state, fmt)
    except ValueError as e:
        # 格式非法 / 表名非法 / 结果表不存在 → 客户端错误
        raise HTTPException(status_code=400, detail=str(e))

    await audit_service.write_audit(
        action="task.export",
        tenant_id=tenant_id,
        user=current_user,
        resource=task_id,
        detail={"file_name": result["file_name"], "format": result["format"],
                "row_count": result["row_count"]},
        ip=request.client.host if request.client else None,
    )

    return {
        "file_name": result["file_name"],
        "format": result["format"],
        "row_count": result["row_count"],
        "preview": result["preview"],
    }


@export_router.get("/tenants/{tenant_id}/tasks/{task_id}/exports")
async def list_export_files(tenant_id: str, task_id: str,
                            tenant: dict = Depends(get_tenant)):
    """列出任务已生成的接口文件"""
    await _load_task_checked(tenant_id, task_id)
    files = interface_file_service.list_export_files(task_id)
    return {"task_id": task_id, "total": len(files), "files": files}


@export_router.get("/tenants/{tenant_id}/tasks/{task_id}/exports/{file_name}")
async def download_export_file(tenant_id: str, task_id: str, file_name: str,
                               tenant: dict = Depends(get_tenant)):
    """下载接口文件（文件名白名单校验，防路径穿越）"""
    await _load_task_checked(tenant_id, task_id)

    path = interface_file_service.resolve_export_file(task_id, file_name)
    if not path:
        # 非法文件名与不存在统一 404（不暴露目录结构信息）
        raise HTTPException(status_code=404, detail="导出文件不存在或文件名非法")

    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(ext, "application/octet-stream"),
        filename=file_name,
    )
