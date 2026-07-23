"""
向量库维护 API
制度文档上传、列表、重建索引、检索测试
"""

import os
import uuid
from fastapi import APIRouter, Depends, UploadFile, File, Form
from backend.api.deps import get_tenant
from backend.config import settings
from backend.mcp.regulation_rag import RegulationRAGService

router = APIRouter(tags=["向量库维护"])


@router.post("/tenants/{tenant_id}/regulations/documents")
async def upload_document(
    tenant_id: str,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    tenant: dict = Depends(get_tenant)
):
    """上传制度文档"""
    doc_id = str(uuid.uuid4())

    # 保存文件
    upload_dir = f"{settings.upload_dir}/{tenant_id}/regulations"
    os.makedirs(upload_dir, exist_ok=True)
    file_path = f"{upload_dir}/{doc_id}_{file.filename}"

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    # 解析内容（简化版）
    try:
        text_content = content.decode("utf-8")
    except UnicodeDecodeError:
        text_content = f"[Binary file: {file.filename}]"

    # 添加到RAG
    rag = RegulationRAGService(tenant_id)
    await rag.add_document(doc_id, text_content, doc_type, file.filename)

    return {
        "doc_id": doc_id,
        "filename": file.filename,
        "doc_type": doc_type,
        "size": len(content),
        "status": "indexed",
        "chunk_count": 1
    }


@router.get("/tenants/{tenant_id}/regulations/documents")
async def list_documents(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """列出制度文档"""
    rag = RegulationRAGService(tenant_id)

    documents = []
    for doc_id, doc in rag.documents.items():
        documents.append({
            "id": doc_id,
            "filename": doc["title"] + ".txt",
            "doc_type": doc["doc_type"],
            "size": len(doc["content"]),
            "status": "indexed",
            "chunk_count": 1
        })

    return {
        "total": len(documents),
        "documents": documents
    }


@router.post("/tenants/{tenant_id}/regulations/reindex")
async def reindex_all(tenant_id: str, tenant: dict = Depends(get_tenant)):
    """重建全部索引"""
    rag = RegulationRAGService(tenant_id)
    result = await rag.rebuild_index()

    return {
        "status": "success",
        "rebuilt_docs": result["rebuilt_docs"],
        "message": f"成功重建 {result['rebuilt_docs']} 个文档的索引"
    }


@router.post("/tenants/{tenant_id}/regulations/retrieval-test")
async def retrieval_test(tenant_id: str, query: str, top_k: int = 5, tenant: dict = Depends(get_tenant)):
    """检索测试"""
    rag = RegulationRAGService(tenant_id)
    return await rag.retrieve(query, top_k=top_k)
