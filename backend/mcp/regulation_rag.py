"""
MCP服务: regulation_rag
职责: 制度文档向量检索
M3 起改为持久化实现：底层委托 services/vector_service（租户隔离的磁盘索引），
接口保持与 M1/M2 兼容（Agent 1 调用方式不变）。
首次使用且索引为空时自动注入预置制度文档（兜底，保证离线演示可用）。
"""

from typing import Dict, Any, List
from backend.services.vector_service import VectorService


class RegulationRAGService:
    """制度检索RAG服务（持久化版）"""

    def __init__(self, tenant_id: str = "T001"):
        self.tenant_id = tenant_id
        self.vector_service = VectorService(tenant_id)
        self._preset_checked = False

        # documents 属性保留（兼容旧 API 与 Demo 代码读取文档清单）
        self.documents: Dict[str, Dict[str, Any]] = {}

    async def _ensure_preset(self):
        """索引为空时注入预置制度文档（离线兜底）"""
        if self._preset_checked:
            return
        self._preset_checked = True
        if await self.vector_service.load_chunks():
            return  # 已有索引，无需兜底
        for doc_id, doc in self._preset_documents().items():
            self.documents[doc_id] = doc
            await self.vector_service.index_document(doc_id, doc["content"], doc["doc_type"], doc["title"])

    @staticmethod
    def _preset_documents() -> Dict[str, Dict[str, Any]]:
        """预置制度文档（M1 遗留的内嵌演示集）"""
        return {
            "1104_g01_housing": {
                "content": """# 1104 G01 个人贷款口径
## 个人住房贷款
- 包括新建住房和二手住房，不包括个人商用房贷款
- 公积金组合贷(product_code='P001-G')纳入住房贷款统计
- 余额为报告期末时点余额，按合同金额口径统计
- 信用卡透支余额：有溢缴款时为负数，无溢缴款时不得为负

## 关键陷阱
【严重】个人住房贷款 vs 个人商用房贷款：商用房必须剔除，不得纳入住房贷款
【中等】公积金组合贷：product_code可能为P001-G等特殊编码，应纳入住房贷款
【提示】活期存款口径：智能存款、定活两便属于活期，不是定期""",
                "doc_type": "1104",
                "title": "1104_G01_个人贷款口径"
            },
            "east_loan_contract": {
                "content": """# EAST 个人信贷业务借据信息表
## 贷款余额
- 应包括利息调整部分（即会计账面余额，含应收未收利息的资本化部分）
- 不是纯本金余额

## 逾期本金
- 按月分期还款的个人消费贷款，逾期90天以内按已逾期部分本金余额填报
- 逾期91天及以上按整笔贷款本金余额填报
- 90天是临界点

## 利率字段
- 统一格式 D20.6（小数点后6位）
- 以百分比形式填报（如 4.35% 填报为 4.350000）

## 关键陷阱
【严重】贷款余额口径：必须包含利息调整部分，不是纯本金余额
【严重】逾期本金分段：90天是分界点，前后口径完全不同
【严重】利率精度：统一 D20.6，不得使用 D20.4""",
                "doc_type": "EAST",
                "title": "EAST_信贷业务借据"
            },
            "rate_report": {
                "content": """# 利率报备 个人住房贷款
## 核心口径
- 合同利率：贷款合同中约定的利率
- 执行利率：实际执行的利率（含优惠、贴息等调整后的利率）
- LPR基准：以对应期限的LPR为定价基准

## 利率浮动区间计算
- 浮动区间 = 执行利率 - 对应期限LPR
- 利率浮动单位：基准点（BP），1BP = 0.01%

## 个人住房贷款特殊要求
- 公积金个人住房贷款：不纳入存量浮动利率贷款LPR转换统计

## 关键陷阱
【严重】公积金住房贷款：明确排除在LPR转换统计外
【中等】利率精度：所有利率字段保留至少6位小数""",
                "doc_type": "利率报备",
                "title": "利率报备_个人住房贷款"
            },
            "credit_report": {
                "content": """# 人行征信 个人信用信息报送
## 账户状态映射
- 正常：账户状态=1，当前逾期期数=0
- 逾期：账户状态=1，当前逾期期数>0
- 结清：账户状态=2

## 还款表现代码
- N：正常还款
- 1-7：逾期1-7个月
- C：结清

## 关键陷阱
【严重】账户状态与还款表现一致性：状态为"结清"时，RP段最后一个月必须为"C"
【严重】逾期天数计算：应还日期 - 实际还款日期""",
                "doc_type": "征信",
                "title": "征信_个人信用信息"
            },
            "security_compliance": {
                "content": """# 通用安全合规要求
## 信息分级
- C1：机构内部信息
- C2：可识别个人身份的信息（姓名、身份证号、手机号）
- C3：敏感信息（征信数据、账户密码）

## 报送安全要求
- C2/C3信息在日志中必须脱敏
- 身份证号：保留前3后4
- 手机号：保留前3后4
- 测试环境不得使用生产真实客户信息

## 机构权限
- 所有查询必须包含机构权限过滤（org_no）
- 跨机构数据查询必须走审批流程

## 常用代码映射
- 贷款状态: 01=正常, 02=逾期, 03=核销, 04=结清
- 五级分类: 1=正常, 2=关注, 3=次级, 4=可疑, 5=损失
- 产品类型: P001=住房贷款, P001-G=公积金组合贷, P002=消费贷, P003=经营贷, P004=商用房""",
                "doc_type": "通用安全合规",
                "title": "通用_安全合规要求"
            }
        }

    async def retrieve(self, query: str, doc_type: str = None, top_k: int = 5,
                       active_doc_ids: set = None) -> Dict[str, Any]:
        """检索制度文档（切片级，含耗时）
        一致性修复（L2-D8）：active_doc_ids 未显式传入时，默认只检索启用中的文档，
        被禁用/删除的文档不再参与 Agent 1 检索。"""
        await self._ensure_preset()
        if active_doc_ids is None:
            active_doc_ids = await self._default_active_doc_ids()
        return await self.vector_service.retrieve(query, doc_type, top_k, active_doc_ids)

    async def _default_active_doc_ids(self) -> set:
        """从元数据表读取启用中的文档 ID 集合；
        无登记文档时（纯预置/冒烟环境）返回 None 表示不过滤"""
        from sqlalchemy import select
        from backend.database import PlatformSessionLocal
        from backend.models.document import RegulationDocument

        async with PlatformSessionLocal() as session:
            result = await session.execute(
                select(RegulationDocument.id).where(
                    RegulationDocument.tenant_id == self.tenant_id,
                    RegulationDocument.is_active == True,  # noqa: E712
                    RegulationDocument.status == "indexed",
                )
            )
            active = {row[0] for row in result.all()}
            # 该租户有登记文档时才过滤（避免预置文档在元数据缺失时被误过滤）
            count = await session.execute(
                select(RegulationDocument.id).where(
                    RegulationDocument.tenant_id == self.tenant_id)
            )
            return active if count.first() else None

    async def add_document(self, doc_id: str, content: str, doc_type: str, title: str):
        """添加新文档并建立索引"""
        self.documents[doc_id] = {
            "content": content,
            "doc_type": doc_type,
            "title": title
        }
        result = await self.vector_service.index_document(doc_id, content, doc_type, title)
        return {"status": "indexed", "doc_id": doc_id, "chunk_count": result["chunk_count"]}

    async def rebuild_index(self, documents: List[Dict[str, Any]] = None):
        """重建索引；不传 documents 时重建内存登记的全部文档"""
        docs = documents
        if docs is None:
            docs = [
                {"doc_id": d_id, "content": d["content"], "doc_type": d["doc_type"], "title": d["title"]}
                for d_id, d in self.documents.items()
            ]
        result = await self.vector_service.rebuild_all(docs)
        return {"rebuilt_docs": result["rebuilt_docs"], "status": "success"}
