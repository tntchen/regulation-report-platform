"""
业务术语词典服务（范围 E）

提供词条 CRUD + 幂等内置种子 + 面向映射引擎的查询接口。
内置种子为零售信贷演示库（loan_contract 实际字段）常用术语，
field_hints 可含演示库不存在的字段（如 loan_balance / repay_type），
由引擎侧按候选 schema 实际过滤后才加分。
"""

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.database import PlatformSessionLocal
from backend.models.term_dict import TermDict
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# ============================================
# 内置零售信贷术语（全局词条，幂等种子）
# ============================================
BUILTIN_TERMS: List[Dict[str, Any]] = [
    # 产品类
    {"term": "按揭", "aliases": ["房贷", "个人住房贷款"],
     "field_hints": ["principal_balance", "loan_balance"], "category": "产品"},
    {"term": "消费贷", "aliases": ["个人消费贷款"],
     "field_hints": ["product_code"], "category": "产品"},
    {"term": "车贷", "aliases": ["汽车贷款", "个人汽车贷款"],
     "field_hints": ["product_code"], "category": "产品"},
    {"term": "经营贷", "aliases": ["经营贷款", "个人经营贷款"],
     "field_hints": ["product_code"], "category": "产品"},
    # 余额/金额类
    {"term": "贷款余额", "aliases": ["贷款剩余本金"],
     "field_hints": ["principal_balance", "interest_capitalized", "loan_balance"], "category": "余额"},
    {"term": "本金余额", "aliases": ["剩余本金"],
     "field_hints": ["principal_balance"], "category": "余额"},
    {"term": "资本化利息", "aliases": ["利息资本化"],
     "field_hints": ["interest_capitalized"], "category": "余额"},
    {"term": "贷款金额", "aliases": ["放款金额", "合同金额"],
     "field_hints": ["loan_amount"], "category": "金额"},
    # 质量类
    {"term": "逾期", "aliases": ["逾期贷款"],
     "field_hints": ["overdue_days", "loan_status"], "category": "质量"},
    {"term": "逾期天数", "aliases": ["逾期时长"],
     "field_hints": ["overdue_days"], "category": "质量"},
    {"term": "五级分类", "aliases": ["正常", "关注", "次级", "可疑", "损失", "资产质量分类"],
     "field_hints": ["five_classify"], "category": "质量"},
    {"term": "贷款状态", "aliases": ["借据状态"],
     "field_hints": ["loan_status"], "category": "质量"},
    # 还款方式/日期类（repay_type 演示库暂缺，用于验证 schema 过滤）
    {"term": "月供", "aliases": ["月还款额"],
     "field_hints": ["repay_type", "repay_date"], "category": "还款"},
    {"term": "等额本息", "aliases": [],
     "field_hints": ["repay_type"], "category": "还款"},
    {"term": "等额本金", "aliases": [],
     "field_hints": ["repay_type"], "category": "还款"},
    {"term": "应还日期", "aliases": ["还款日", "还款日期"],
     "field_hints": ["repay_date"], "category": "还款"},
    # 利率类
    {"term": "LPR", "aliases": ["贷款市场报价利率", "基准利率"],
     "field_hints": ["execute_rate"], "category": "利率"},
    {"term": "利率", "aliases": ["执行利率", "贷款利率"],
     "field_hints": ["execute_rate"], "category": "利率"},
    # 标识类
    {"term": "合同编号", "aliases": ["借据号", "借款合同编号"],
     "field_hints": ["contract_no"], "category": "标识"},
    {"term": "客户号", "aliases": ["客户ID", "客户编号"],
     "field_hints": ["cust_id"], "category": "标识"},
    {"term": "机构号", "aliases": ["网点号", "机构编号"],
     "field_hints": ["org_no"], "category": "标识"},
    {"term": "业务日期", "aliases": ["数据日期", "报送日期"],
     "field_hints": ["biz_date"], "category": "标识"},
]


def _row_to_dict(row: TermDict) -> Dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "term": row.term,
        "aliases": row.aliases or [],
        "field_hints": row.field_hints or [],
        "category": row.category,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_terms(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出词条：全局词条 + 指定租户词条叠加；不传 tenant_id 仅返回全局词条"""
    async with PlatformSessionLocal() as session:
        stmt = select(TermDict)
        if tenant_id:
            stmt = stmt.where(TermDict.tenant_id.in_([None, tenant_id]))
        else:
            stmt = stmt.where(TermDict.tenant_id.is_(None))
        rows = (await session.execute(stmt.order_by(TermDict.term))).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def get_term(term_id: str) -> Optional[Dict[str, Any]]:
    async with PlatformSessionLocal() as session:
        row = await session.get(TermDict, term_id)
    return _row_to_dict(row) if row else None


async def create_term(data: Dict[str, Any], tenant_id: Optional[str] = None,
                      created_by: Optional[str] = None) -> Dict[str, Any]:
    """创建词条；同租户（含全局）下术语名重复返回 None"""
    async with PlatformSessionLocal() as session:
        stmt = select(TermDict).where(TermDict.term == data["term"])
        stmt = stmt.where(TermDict.tenant_id == tenant_id) if tenant_id \
            else stmt.where(TermDict.tenant_id.is_(None))
        if (await session.execute(stmt)).scalars().first():
            return None
        row = TermDict(
            id=uuid.uuid4().hex[:32],
            tenant_id=tenant_id,
            term=data["term"],
            aliases=data.get("aliases", []),
            field_hints=data.get("field_hints", []),
            category=data.get("category"),
            created_by=created_by,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _row_to_dict(row)


async def update_term(term_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    allowed = {"term", "aliases", "field_hints", "category"}
    async with PlatformSessionLocal() as session:
        row = await session.get(TermDict, term_id)
        if not row:
            return None
        for key, value in updates.items():
            if key in allowed and value is not None:
                setattr(row, key, value)
        await session.commit()
        await session.refresh(row)
    return _row_to_dict(row)


async def delete_term(term_id: str) -> bool:
    async with PlatformSessionLocal() as session:
        row = await session.get(TermDict, term_id)
        if not row:
            return False
        await session.delete(row)
        await session.commit()
    return True


async def seed_builtin_terms(tenant_id: Optional[str] = None,
                             created_by: str = "system") -> List[str]:
    """内置术语幂等种子：按 (tenant_id, term) 查重，已存在跳过。
    返回本次新写入的术语名列表。"""
    created = []
    async with PlatformSessionLocal() as session:
        for t in BUILTIN_TERMS:
            stmt = select(TermDict).where(TermDict.term == t["term"])
            stmt = stmt.where(TermDict.tenant_id == tenant_id) if tenant_id \
                else stmt.where(TermDict.tenant_id.is_(None))
            if (await session.execute(stmt)).scalars().first():
                continue
            session.add(TermDict(
                id=uuid.uuid4().hex[:32], tenant_id=tenant_id,
                created_by=created_by, **t))
            created.append(t["term"])
        await session.commit()
    if created:
        logger.info("内置业务术语种子写入 %d 条: %s", len(created), created)
    return created


async def load_term_hints_safe(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """供映射引擎使用的容错查询：表不存在/未种子等异常一律返回空列表，
    绝不因词典缺失阻断推断流程（静默降级）。"""
    try:
        return await list_terms(tenant_id)
    except Exception as e:
        logger.warning("术语词典加载失败，按空词典降级: %s", e)
        return []
