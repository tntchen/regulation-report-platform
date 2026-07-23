"""
向量库维护 API
文档上传/列表/详情/启停/删除、索引重建、索引状态、检索测试、统计、索引日志
文档元数据落 SQLite（regulation_documents / index_logs 表），
向量索引落租户独立目录（data/tenants/{tenant_id}/vectors/）。
"""

import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from sqlalchemy import select

from backend.api.deps import get_tenant
from backend.config import settings
from backend.database import PlatformSessionLocal
from backend.models.document import RegulationDocument
from backend.models.regulation import IndexLog, RetrievalFeedback
from backend.services import audit_service
from backend.services.document_service import parse_document
from backend.services.vector_service import VectorService

router = APIRouter(tags=["向量库维护"])


def _audit_user(request: Request):
    """从 request.state 取当前用户（由 get_current_user 写入）"""
    return getattr(request.state, "user", None)


def _client_ip(request: Request):
    return request.client.host if request.client else None


# ============================================
# 内部工具
# ============================================
async def _log_index(tenant_id: str, operation: str, doc_id: str = None,
                     doc_name: str = None, status: str = "success",
                     message: str = "", duration_ms: int = 0):
    """写入索引日志"""
    async with PlatformSessionLocal() as session:
        session.add(IndexLog(
            tenant_id=tenant_id, operation=operation, doc_id=doc_id,
            doc_name=doc_name, status=status, message=message, duration_ms=duration_ms
        ))
        await session.commit()


async def _get_doc_or_404(tenant_id: str, doc_id: str) -> RegulationDocument:
    """查询文档元数据，不存在则 404"""
    async with PlatformSessionLocal() as session:
        doc = await session.get(RegulationDocument, doc_id)
        if not doc or doc.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="文档不存在")
        # 将字段读出为 dict，避免 session 关闭后懒加载
        return {
            "id": doc.id, "tenant_id": doc.tenant_id, "filename": doc.filename,
            "doc_type": doc.doc_type, "file_path": doc.file_path, "size": doc.size,
            "status": doc.status, "chunk_count": doc.chunk_count,
            "vector_count": doc.vector_count, "uploaded_at": doc.uploaded_at,
            "indexed_at": doc.indexed_at, "version": doc.version, "is_active": doc.is_active
        }


async def _active_doc_ids(tenant_id: str) -> set:
    """获取当前启用状态的文档 ID 集合"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument.id).where(
                RegulationDocument.tenant_id == tenant_id,
                RegulationDocument.is_active == True,  # noqa: E712
                RegulationDocument.status == "indexed"
            )
        )
        return {row[0] for row in result.all()}


async def _index_one_document(tenant_id: str, doc: dict) -> int:
    """对单个文档执行索引，更新元数据状态，返回切片数"""
    vs = VectorService(tenant_id)
    start = datetime.now()

    # 更新状态为 indexing
    async with PlatformSessionLocal() as session:
        row = await session.get(RegulationDocument, doc["id"])
        row.status = "indexing"
        await session.commit()

    try:
        with open(doc["file_path"], "r", encoding="utf-8") as f:
            content = f.read()
        title = os.path.splitext(doc["filename"])[0]
        result = await vs.index_document(doc["id"], content, doc["doc_type"], title)
        duration = int((datetime.now() - start).total_seconds() * 1000)

        async with PlatformSessionLocal() as session:
            row = await session.get(RegulationDocument, doc["id"])
            row.status = "indexed"
            row.chunk_count = result["chunk_count"]
            row.vector_count = result["chunk_count"]
            row.indexed_at = datetime.now()
            row.index_duration_ms = duration
            await session.commit()

        await _log_index(tenant_id, "index", doc["id"], doc["filename"],
                         "success", f"切片 {result['chunk_count']} 个", duration)
        return result["chunk_count"]
    except Exception as e:
        async with PlatformSessionLocal() as session:
            row = await session.get(RegulationDocument, doc["id"])
            row.status = "failed"
            await session.commit()
        await _log_index(tenant_id, "index", doc["id"], doc["filename"], "failed", str(e))
        raise


# ============================================
# 文档管理
# ============================================
@router.post("/tenants/{tenant_id}/regulations/documents")
async def upload_document(
    tenant_id: str,
    request: Request,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    tenant: dict = Depends(get_tenant)
):
    """上传制度文档：保存 → 解析 → 登记元数据 → 自动索引"""
    content = await file.read()
    if len(content) > settings.max_upload_size:
        raise HTTPException(status_code=413, detail="文件超过大小限制")

    # 解析文档内容
    try:
        text_content, fmt = parse_document(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=415, detail=str(e))

    doc_id = str(uuid.uuid4())
    current_user = _audit_user(request) or {}
    uploader = current_user.get("username", "unknown")

    # 保存文件（统一存解析后的文本，保证索引可读）
    upload_dir = os.path.join(settings.upload_dir, tenant_id, "regulations")
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, f"{doc_id}_{file.filename}")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(text_content)

    # 登记元数据（uploaded_by 记录真实操作人）
    async with PlatformSessionLocal() as session:
        session.add(RegulationDocument(
            id=doc_id, tenant_id=tenant_id, filename=file.filename,
            doc_type=doc_type, file_path=file_path, size=len(content),
            status="uploaded", uploaded_by=uploader
        ))
        await session.commit()
    await _log_index(tenant_id, "upload", doc_id, file.filename, "success",
                     f"格式 {fmt}，{len(content)} 字节")

    # 自动索引
    doc = await _get_doc_or_404(tenant_id, doc_id)
    chunk_count = await _index_one_document(tenant_id, doc)

    # 文档上传埋点
    await audit_service.write_audit(
        action="document.upload",
        tenant_id=tenant_id,
        user=current_user or None,
        resource=f"{doc_id} {file.filename}",
        detail={"doc_type": doc_type, "size": len(content),
                "format": fmt, "chunk_count": chunk_count},
        ip=_client_ip(request),
    )

    return {
        "doc_id": doc_id,
        "filename": file.filename,
        "doc_type": doc_type,
        "size": len(content),
        "status": "indexed",
        "chunk_count": chunk_count
    }


@router.get("/tenants/{tenant_id}/regulations/documents")
async def list_documents(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """列出制度文档"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument).where(RegulationDocument.tenant_id == tenant_id)
        )
        docs = result.scalars().all()

    return {
        "total": len(docs),
        "documents": [
            {
                "id": d.id, "filename": d.filename, "doc_type": d.doc_type,
                "size": d.size, "status": d.status, "chunk_count": d.chunk_count,
                "is_active": d.is_active, "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None
            }
            for d in docs
        ]
    }


@router.get("/tenants/{tenant_id}/regulations/documents/{doc_id}")
async def get_document_detail(tenant_id: str, doc_id: str, tenant: dict = Depends(get_tenant)):
    """获取文档详情（元数据 + 内容预览 + 切片信息）"""
    doc = await _get_doc_or_404(tenant_id, doc_id)

    preview = ""
    if doc["file_path"] and os.path.exists(doc["file_path"]):
        with open(doc["file_path"], "r", encoding="utf-8") as f:
            preview = f.read(1000)

    vs = VectorService(tenant_id)
    chunks = [c for c in vs.load_chunks() if c["doc_id"] == doc_id]

    return {
        **doc,
        "uploaded_at": doc["uploaded_at"].isoformat() if doc["uploaded_at"] else None,
        "indexed_at": doc["indexed_at"].isoformat() if doc["indexed_at"] else None,
        "preview": preview,
        "chunks": [
            {"chunk_id": c["chunk_id"], "chunk_index": c["chunk_index"],
             "length": len(c["content"])}
            for c in chunks
        ]
    }


@router.put("/tenants/{tenant_id}/regulations/documents/{doc_id}")
async def update_document(tenant_id: str, doc_id: str, payload: dict, request: Request,
                          tenant: dict = Depends(get_tenant)):
    """更新文档（启用/禁用）"""
    doc = await _get_doc_or_404(tenant_id, doc_id)
    is_active = payload.get("is_active")
    async with PlatformSessionLocal() as session:
        row = await session.get(RegulationDocument, doc_id)
        if is_active is not None:
            row.is_active = bool(is_active)
        await session.commit()

        # 启用/禁用埋点
        await audit_service.write_audit(
            action="document.disable" if is_active is False else "document.enable",
            tenant_id=tenant_id,
            user=_audit_user(request),
            resource=f"{doc_id} {doc['filename']}",
            detail={"is_active": row.is_active},
            ip=_client_ip(request),
        )
        return {"doc_id": doc_id, "is_active": row.is_active, "status": row.status}


@router.delete("/tenants/{tenant_id}/regulations/documents/{doc_id}")
async def delete_document(tenant_id: str, doc_id: str, request: Request,
                          tenant: dict = Depends(get_tenant)):
    """删除文档：移除向量索引 + 文件 + 元数据"""
    doc = await _get_doc_or_404(tenant_id, doc_id)

    vs = VectorService(tenant_id)
    removed_chunks = await vs.remove_document(doc_id)

    if doc["file_path"] and os.path.exists(doc["file_path"]):
        os.remove(doc["file_path"])

    async with PlatformSessionLocal() as session:
        row = await session.get(RegulationDocument, doc_id)
        await session.delete(row)
        await session.commit()

    await _log_index(tenant_id, "delete", doc_id, doc["filename"], "success",
                     f"移除切片 {removed_chunks} 个")

    # 文档删除埋点
    await audit_service.write_audit(
        action="document.delete",
        tenant_id=tenant_id,
        user=_audit_user(request),
        resource=f"{doc_id} {doc['filename']}",
        detail={"removed_chunks": removed_chunks},
        ip=_client_ip(request),
    )
    return {"doc_id": doc_id, "deleted": True, "removed_chunks": removed_chunks}


# ============================================
# 索引管理
# ============================================
@router.post("/tenants/{tenant_id}/regulations/reindex")
async def reindex_all(tenant_id: str, request: Request, tenant: dict = Depends(get_tenant)):
    """重建全部索引（仅启用状态的文档）"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument).where(
                RegulationDocument.tenant_id == tenant_id,
                RegulationDocument.is_active == True  # noqa: E712
            )
        )
        docs = result.scalars().all()
        doc_dicts = [{"id": d.id, "filename": d.filename, "doc_type": d.doc_type,
                      "file_path": d.file_path} for d in docs]

    start = datetime.now()
    total_chunks = 0
    failed = 0
    for doc in doc_dicts:
        try:
            total_chunks += await _index_one_document(tenant_id, doc)
        except Exception:
            failed += 1

    duration = int((datetime.now() - start).total_seconds() * 1000)
    await _log_index(tenant_id, "reindex_all", None, "全部文档",
                     "success" if failed == 0 else "failed",
                     f"重建 {len(doc_dicts) - failed}/{len(doc_dicts)} 个文档", duration)

    # 全量重建埋点
    await audit_service.write_audit(
        action="regulations.reindex",
        tenant_id=tenant_id,
        user=_audit_user(request),
        resource="全部文档",
        detail={"rebuilt_docs": len(doc_dicts) - failed, "failed_docs": failed,
                "total_chunks": total_chunks, "duration_ms": duration},
        ip=_client_ip(request),
        result="success" if failed == 0 else "fail",
    )

    return {
        "status": "success" if failed == 0 else "partial_failed",
        "rebuilt_docs": len(doc_dicts) - failed,
        "failed_docs": failed,
        "total_chunks": total_chunks,
        "message": f"成功重建 {len(doc_dicts) - failed} 个文档的索引"
    }


@router.post("/tenants/{tenant_id}/regulations/documents/{doc_id}/reindex")
async def reindex_one(tenant_id: str, doc_id: str, request: Request,
                      tenant: dict = Depends(get_tenant)):
    """重建单个文档索引"""
    doc = await _get_doc_or_404(tenant_id, doc_id)
    chunk_count = await _index_one_document(tenant_id, doc)

    # 单文档重建埋点
    await audit_service.write_audit(
        action="document.reindex",
        tenant_id=tenant_id,
        user=_audit_user(request),
        resource=f"{doc_id} {doc['filename']}",
        detail={"chunk_count": chunk_count},
        ip=_client_ip(request),
    )
    return {"doc_id": doc_id, "status": "indexed", "chunk_count": chunk_count}


@router.get("/tenants/{tenant_id}/regulations/index-status")
async def index_status(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """获取索引状态概览"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument).where(RegulationDocument.tenant_id == tenant_id)
        )
        docs = result.scalars().all()

    by_status = {}
    for d in docs:
        by_status[d.status] = by_status.get(d.status, 0) + 1

    vs = VectorService(tenant_id)
    return {
        "total_docs": len(docs),
        "by_status": by_status,
        "total_chunks": vs.stats()["chunk_count"],
        "healthy": by_status.get("failed", 0) == 0
    }


# ============================================
# 检索测试与反馈
# ============================================
@router.post("/tenants/{tenant_id}/regulations/retrieval-test")
async def retrieval_test(tenant_id: str, query: str, top_k: int = 5,
                         doc_type: Optional[str] = None,
                         tenant: dict = Depends(get_tenant)):
    """检索测试：返回 Top-K（排名/相关度/文档名/匹配片段/耗时）"""
    vs = VectorService(tenant_id)
    active_ids = await _active_doc_ids(tenant_id)
    # 无任何登记文档时（纯演示环境），不过滤
    if not active_ids:
        active_ids = None
    result = vs.retrieve(query, doc_type=doc_type, top_k=top_k, active_doc_ids=active_ids)

    return {
        "query": query,
        "top_k": top_k,
        "elapsed_ms": result["elapsed_ms"],
        "total_found": result["total_found"],
        "results": [
            {"rank": i + 1, **r} for i, r in enumerate(result["results"])
        ]
    }


@router.post("/tenants/{tenant_id}/regulations/retrieval-feedback")
async def retrieval_feedback(tenant_id: str, payload: dict,
                             tenant: dict = Depends(get_tenant)):
    """提交检索反馈"""
    async with PlatformSessionLocal() as session:
        session.add(RetrievalFeedback(
            tenant_id=tenant_id,
            query=payload.get("query", ""),
            result_rank=payload.get("result_rank"),
            is_accurate=payload.get("is_accurate"),
            comment=payload.get("comment")
        ))
        await session.commit()
    return {"status": "recorded"}


# ============================================
# 统计与日志
# ============================================
@router.get("/tenants/{tenant_id}/regulations/stats")
async def get_stats(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """向量库统计（文档数/向量数/状态分布）"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(RegulationDocument).where(RegulationDocument.tenant_id == tenant_id)
        )
        docs = result.scalars().all()

    vs = VectorService(tenant_id)
    vstats = vs.stats()

    by_status = {}
    for d in docs:
        by_status[d.status] = by_status.get(d.status, 0) + 1

    return {
        "tenant_id": tenant_id,
        "doc_count": len(docs),
        "active_docs": sum(1 for d in docs if d.is_active),
        "by_status": by_status,
        "chunk_count": vstats["chunk_count"],
        "vector_count": vstats["vector_count"],
        "vector_dimension": vstats["vector_dimension"],
        "storage_dir": vstats["storage_dir"]
    }


@router.get("/tenants/{tenant_id}/regulations/index-logs")
async def get_index_logs(tenant_id: str, limit: int = 20,
                         tenant: dict = Depends(get_tenant)):
    """获取索引日志（最近 N 条）"""
    async with PlatformSessionLocal() as session:
        result = await session.execute(
            select(IndexLog).where(IndexLog.tenant_id == tenant_id)
            .order_by(IndexLog.created_at.desc()).limit(limit)
        )
        logs = result.scalars().all()

    return {
        "total": len(logs),
        "logs": [
            {
                "time": l.created_at.isoformat() if l.created_at else None,
                "operation": l.operation, "doc_name": l.doc_name,
                "status": l.status, "message": l.message,
                "duration_ms": l.duration_ms
            }
            for l in logs
        ]
    }
