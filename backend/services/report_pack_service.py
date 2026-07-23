"""
场景包服务
报表定义从代码变成数据：CRUD + 进程内 TTL 缓存 + 内置场景包种子（幂等）。
Agent 1/2 通过本服务读取目标结构/候选源表/勾稽规则/检索关键词，替换硬编码。
"""

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.database import PlatformSessionLocal
from backend.models.report_pack import ReportPack
from backend.utils.logging import get_logger

logger = get_logger(__name__)

# 场景包缓存：{pack_id: (pack_dict, expire_ts)}
# Demo 深度：进程内 TTL 缓存 + 写操作主动失效，足够单机演示
_PACK_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 60

# 缺省场景包：任务未指定 report_pack_id 时回退到 G01（兼容现有行为）
DEFAULT_PACK_ID = "G01"

# ============================================
# 内置场景包定义（从现有硬编码场景迁移而来）
# ============================================
BUILTIN_REPORT_PACKS: List[Dict[str, Any]] = [
    {
        "id": "G01",
        "report_name": "1104 G01 资产负债项目统计表·个人住房贷款",
        "report_type": "1104",
        "target_table": "rpt_g01_housing_loan",
        "target_schema": [
            {"field": "contract_no", "data_type": "string", "required": True,
             "caliber_text": "借款合同编号，直接取数"},
            {"field": "cust_id", "data_type": "string", "required": True,
             "caliber_text": "客户ID，直接取数"},
            {"field": "loan_balance", "data_type": "decimal", "required": True,
             "caliber_text": "贷款余额 = 本金余额 + 资本化利息（1104口径）"},
            {"field": "execute_rate", "data_type": "decimal", "required": False,
             "caliber_text": "执行利率，D20.6 保留6位小数"},
            {"field": "overdue_principal", "data_type": "decimal", "required": False,
             "caliber_text": "逾期本金：90天以内按已逾期部分，91天及以上按整笔"},
            {"field": "biz_date", "data_type": "date", "required": True,
             "caliber_text": "业务日期"},
            {"field": "org_no", "data_type": "string", "required": True,
             "caliber_text": "机构号（权限过滤字段）"},
        ],
        "source_tables": ["loan_contract"],
        "reconciliation_rules": [
            # 贷款余额 = 本金余额 + 利息调整（资本化利息），与源表按口径重算勾稽
            {"name": "贷款余额勾稽",
             "expression": "SUM(loan_balance) = SUM(principal_balance + IFNULL(interest_capitalized, 0))",
             "tolerance": 0.01},
            # 笔数勾稽：目标表户数 = 源表有效借据笔数
            {"name": "借据笔数勾稽", "expression": "COUNT(*) = COUNT(*)",
             "tolerance": 0},
        ],
        "trap_refs": ["组合贷", "资本化利息"],
        "regulation_keywords": "1104 G01 个人住房贷款 资产负债项目统计 口径",
        "status": "active",
    },
    {
        "id": "G11",
        "report_name": "1104 G11 资产质量五级分类表",
        "report_type": "1104",
        "target_table": "rpt_g11_five_classify",
        "target_schema": [
            {"field": "contract_no", "data_type": "string", "required": True,
             "caliber_text": "借款合同编号，直接取数"},
            {"field": "cust_id", "data_type": "string", "required": True,
             "caliber_text": "客户ID，直接取数"},
            {"field": "loan_balance", "data_type": "decimal", "required": True,
             "caliber_text": "贷款余额 = 本金余额 + 资本化利息"},
            {"field": "five_classify", "data_type": "string", "required": True,
             "caliber_text": "五级分类：1正常/2关注/3次级/4可疑/5损失",
             "expected_domain": ["1", "2", "3", "4", "5"]},
            {"field": "overdue_days", "data_type": "integer", "required": False,
             "caliber_text": "逾期天数：逾期90天以上应至少降为次级"},
            {"field": "biz_date", "data_type": "date", "required": True,
             "caliber_text": "业务日期"},
            {"field": "org_no", "data_type": "string", "required": True,
             "caliber_text": "机构号（权限过滤字段）"},
        ],
        "source_tables": ["loan_contract"],
        "reconciliation_rules": [
            {"name": "贷款余额勾稽",
             "expression": "SUM(loan_balance) = SUM(principal_balance + IFNULL(interest_capitalized, 0))",
             "tolerance": 0.01},
            # 五级分类各项之和 = 贷款总额：按五级分类逐组与源表勾稽
            {"name": "五级分类合计勾稽", "expression": "SUM_BY(five_classify, loan_balance)",
             "tolerance": 0.01},
        ],
        "trap_refs": ["逾期90天", "五级分类"],
        "regulation_keywords": "1104 G11 资产质量 五级分类 逾期90天 口径",
        "status": "active",
    },
    {
        "id": "EAST_JJ",
        "report_name": "EAST 信贷业务借据表（个人住房贷款）",
        "report_type": "EAST",
        "target_table": "rpt_east_housing_loan",
        "target_schema": [
            {"field": "contract_no", "data_type": "string", "required": True,
             "caliber_text": "借据号/合同编号，直接取数"},
            {"field": "cust_id", "data_type": "string", "required": True,
             "caliber_text": "客户ID，直接取数"},
            {"field": "loan_balance", "data_type": "decimal", "required": True,
             "caliber_text": "贷款余额 = 本金余额 + 资本化利息（EAST口径：含利息调整部分）"},
            {"field": "execute_rate", "data_type": "decimal", "required": False,
             "caliber_text": "执行利率，D20.6 保留6位小数"},
            {"field": "overdue_principal", "data_type": "decimal", "required": False,
             "caliber_text": "逾期本金：90天以内按已逾期部分，91天及以上按整笔"},
            {"field": "biz_date", "data_type": "date", "required": True,
             "caliber_text": "业务日期"},
            {"field": "org_no", "data_type": "string", "required": True,
             "caliber_text": "机构号（权限过滤字段）"},
        ],
        "source_tables": ["loan_contract"],
        "reconciliation_rules": [
            # 借据金额汇总勾稽：EAST 口径余额（含利息调整）与源表重算一致
            {"name": "借据余额汇总勾稽",
             "expression": "SUM(loan_balance) = SUM(principal_balance + IFNULL(interest_capitalized, 0))",
             "tolerance": 0.01},
            {"name": "借据笔数勾稽", "expression": "COUNT(*) = COUNT(*)",
             "tolerance": 0},
        ],
        "trap_refs": ["组合贷", "资本化利息", "逾期90天"],
        "regulation_keywords": "EAST 信贷业务借据 个人住房贷款 口径",
        "status": "active",
    },
]


def _row_to_dict(row: ReportPack) -> Dict[str, Any]:
    """ORM 行 → 对外的场景包字典"""
    return {
        "id": row.id,
        "report_name": row.report_name,
        "report_type": row.report_type,
        "target_table": row.target_table,
        "target_schema": row.target_schema or [],
        "source_tables": row.source_tables or [],
        "reconciliation_rules": row.reconciliation_rules or [],
        "trap_refs": row.trap_refs or [],
        "regulation_keywords": row.regulation_keywords or "",
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def invalidate_cache(pack_id: Optional[str] = None):
    """主动失效缓存；pack_id 为 None 时清空全部"""
    if pack_id is None:
        _PACK_CACHE.clear()
    else:
        _PACK_CACHE.pop(pack_id, None)


async def get_pack(pack_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 加载场景包（缓存 → 平台库）；不存在返回 None"""
    now = time.monotonic()
    cached = _PACK_CACHE.get(pack_id)
    if cached and cached[1] > now:
        return cached[0]

    async with PlatformSessionLocal() as session:
        row = await session.get(ReportPack, pack_id)

    if not row:
        return None
    pack = _row_to_dict(row)
    _PACK_CACHE[pack_id] = (pack, now + _CACHE_TTL_SECONDS)
    return pack


async def get_pack_safe(pack_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """容错版 get_pack：表不存在/未种子等异常一律返回 None
    供 Agent 使用——包缺失时回退到原有硬编码行为，绝不因包加载失败阻断任务"""
    if not pack_id:
        pack_id = DEFAULT_PACK_ID
    try:
        return await get_pack(pack_id)
    except Exception as e:
        logger.warning("场景包加载失败(pack=%s)，回退硬编码行为: %s", pack_id, e)
        return None


async def list_packs(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出场景包；status 传入时按状态过滤"""
    async with PlatformSessionLocal() as session:
        stmt = select(ReportPack).order_by(ReportPack.id)
        if status:
            stmt = stmt.where(ReportPack.status == status)
        rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def create_pack(pack_data: Dict[str, Any], created_by: Optional[str] = None) -> Optional[ReportPack]:
    """创建场景包；ID 冲突返回 None"""
    async with PlatformSessionLocal() as session:
        if await session.get(ReportPack, pack_data["id"]):
            return None
        row = ReportPack(
            id=pack_data["id"],
            report_name=pack_data["report_name"],
            report_type=pack_data["report_type"],
            target_table=pack_data["target_table"],
            target_schema=pack_data.get("target_schema", []),
            source_tables=pack_data.get("source_tables", []),
            reconciliation_rules=pack_data.get("reconciliation_rules", []),
            trap_refs=pack_data.get("trap_refs", []),
            regulation_keywords=pack_data.get("regulation_keywords", ""),
            status=pack_data.get("status", "active"),
            created_by=created_by,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    invalidate_cache(row.id)
    return row


async def update_pack(pack_id: str, updates: Dict[str, Any]) -> Optional[ReportPack]:
    """更新场景包（仅更新传入的字段）；不存在返回 None"""
    allowed = {"report_name", "report_type", "target_table", "target_schema",
               "source_tables", "reconciliation_rules", "trap_refs",
               "regulation_keywords", "status"}
    async with PlatformSessionLocal() as session:
        row = await session.get(ReportPack, pack_id)
        if not row:
            return None
        for key, value in updates.items():
            if key in allowed and value is not None:
                setattr(row, key, value)
        await session.commit()
        await session.refresh(row)
    invalidate_cache(pack_id)
    return row


async def seed_builtin_packs() -> List[str]:
    """把内置场景包灌入 report_packs 表（upsert 语义：
    不存在则插入；已存在则更新内置字段，使规则修订随版本生效）
    返回本次新写入的包 ID 列表"""
    created = []
    async with PlatformSessionLocal() as session:
        for pack in BUILTIN_REPORT_PACKS:
            existing = await session.get(ReportPack, pack["id"])
            if existing:
                # upsert：内置包的定义字段始终跟随代码版本更新
                for key, value in pack.items():
                    setattr(existing, key, value)
                continue
            session.add(ReportPack(created_by="system", **pack))
            created.append(pack["id"])
        await session.commit()
    if created:
        invalidate_cache()
        logger.info("内置场景包种子写入: %s", created)
    return created
