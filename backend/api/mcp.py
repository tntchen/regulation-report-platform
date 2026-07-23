"""
MCP 服务 API
数据库 Schema 查询、只读 SQL 执行、制度检索
"""

from fastapi import APIRouter, Depends, Request
from backend.api.deps import get_tenant
from backend.mcp.database_mcp import DatabaseMCPService
from backend.mcp.regulation_rag import RegulationRAGService
from backend.services import audit_service

router = APIRouter(tags=["MCP服务"])


@router.post("/tenants/{tenant_id}/mcp/database/query_schema")
async def query_schema(tenant_id: str, table_name: str, tenant: dict = Depends(get_tenant)):
    """查询表结构"""
    db_config = tenant.get("data_sources", [{}])[0]
    mcp = DatabaseMCPService(db_config)
    return await mcp.query_schema(table_name)


@router.post("/tenants/{tenant_id}/mcp/database/execute_sql")
async def execute_sql(tenant_id: str, sql: str, request: Request, limit: int = 100,
                      tenant: dict = Depends(get_tenant)):
    """执行只读SQL（银行安全红线：仅允许 SELECT）"""
    db_config = tenant.get("data_sources", [{}])[0]
    mcp = DatabaseMCPService(db_config)
    result = await mcp.execute_sql(sql, limit)

    # SQL 执行埋点（截断 SQL 文本，避免审计膨胀）
    await audit_service.write_audit(
        action="mcp.execute_sql",
        tenant_id=tenant_id,
        user=getattr(request.state, "user", None),
        resource=db_config.get("source_id", ""),
        detail={"sql": sql[:200], "limit": limit},
        ip=request.client.host if request.client else None,
    )
    return result


@router.post("/tenants/{tenant_id}/mcp/regulation/retrieve")
async def retrieve_regulation(tenant_id: str, query: str, doc_type: str = None, top_k: int = 5, tenant: dict = Depends(get_tenant)):
    """检索制度文档"""
    rag = RegulationRAGService(tenant_id)
    return await rag.retrieve(query, doc_type, top_k)
