"""
制度版本 Diff + 新旧逻辑回归 API
- POST /tenants/{tid}/regulations/diff：两份制度文档（向量库文档）按 Markdown 标题做结构化 diff
- POST /tenants/{tid}/twin/regression：同一演示数据集上执行新旧两版转换 SQL，量化口径差异
两个端点均挂鉴权（get_tenant）+ 审计（regulation.diff / twin.regression）。
路由前缀 /v1（由 main.py 统一注册，try/except 兜底）。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from backend.agents.digital_twin import DigitalTwinAgent
from backend.api.deps import get_tenant, get_current_user
from backend.database import PlatformSessionLocal
from backend.models.document import RegulationDocument
from backend.services import audit_service, report_pack_service, regulation_diff

# 独立导出，前缀由路由注册方统一指定
diff_router = APIRouter(tags=["制度Diff与回归"])


async def _read_doc_content(tenant_id: str, doc_id: str) -> dict:
    """读取文档元数据与正文内容；不存在/非本租户 404，文件缺失 422"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument).where(RegulationDocument.id == doc_id)
        )
        doc = result.scalar_one_or_none()
    if not doc or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail=f"文档不存在: {doc_id}")
    try:
        with open(doc.file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, TypeError):
        raise HTTPException(status_code=422, detail=f"文档文件不可读: {doc.filename}")
    return {"id": doc.id, "filename": doc.filename, "version": doc.version,
            "content": content}


@diff_router.post("/tenants/{tenant_id}/regulations/diff")
async def regulations_diff(tenant_id: str, payload: dict, request: Request,
                           tenant: dict = Depends(get_tenant),
                           current_user: dict = Depends(get_current_user)):
    """制度版本对比：{doc_id_old, doc_id_new} → 结构化 diff + 受影响口径关键词"""
    doc_id_old = (payload or {}).get("doc_id_old")
    doc_id_new = (payload or {}).get("doc_id_new")
    if not doc_id_old or not doc_id_new:
        raise HTTPException(status_code=422, detail="缺少必填字段: doc_id_old / doc_id_new")
    if doc_id_old == doc_id_new:
        raise HTTPException(status_code=422, detail="新旧文档不能是同一份")

    old_doc = await _read_doc_content(tenant_id, doc_id_old)
    new_doc = await _read_doc_content(tenant_id, doc_id_new)

    result = regulation_diff.compare_documents(old_doc["content"], new_doc["content"])

    await audit_service.write_audit(
        action="regulation.diff",
        tenant_id=tenant_id,
        user=current_user,
        resource=f"{doc_id_old} -> {doc_id_new}",
        detail={
            "old": old_doc["filename"], "new": new_doc["filename"],
            "added": len(result["added_sections"]),
            "removed": len(result["removed_sections"]),
            "changed": len(result["changed_sections"]),
        },
        ip=request.client.host if request.client else None,
    )

    return {
        "doc_old": {"id": old_doc["id"], "filename": old_doc["filename"],
                    "version": old_doc["version"]},
        "doc_new": {"id": new_doc["id"], "filename": new_doc["filename"],
                    "version": new_doc["version"]},
        **result,
    }


@diff_router.post("/tenants/{tenant_id}/twin/regression")
async def twin_regression(tenant_id: str, payload: dict, request: Request,
                          tenant: dict = Depends(get_tenant),
                          current_user: dict = Depends(get_current_user)):
    """新旧逻辑回归：{report_pack_id, sql_old, sql_new} → 差异量化结果"""
    report_pack_id = (payload or {}).get("report_pack_id")
    sql_old = (payload or {}).get("sql_old")
    sql_new = (payload or {}).get("sql_new")
    if not report_pack_id or not sql_old or not sql_new:
        raise HTTPException(status_code=422,
                            detail="缺少必填字段: report_pack_id / sql_old / sql_new")

    pack = await report_pack_service.get_pack(report_pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail=f"场景包不存在: {report_pack_id}")

    agent = DigitalTwinAgent()
    try:
        result = await agent.run_regression(pack, sql_old, sql_new)
    except PermissionError as e:
        # SQL 护栏拒绝：对调用方返回 422 + 脱敏原因
        raise HTTPException(status_code=422, detail=f"SQL 未通过只读校验: {e}")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"回归执行失败: {e}")

    await audit_service.write_audit(
        action="twin.regression",
        tenant_id=tenant_id,
        user=current_user,
        resource=report_pack_id,
        detail={"diff_amount": result["diff_amount"], "diff_rate": result["diff_rate"],
                "old_total": result["old_total"], "new_total": result["new_total"]},
        ip=request.client.host if request.client else None,
    )

    return result
