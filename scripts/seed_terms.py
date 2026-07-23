"""
业务术语词典种子脚本（范围 E）
把内置零售信贷术语幂等灌入平台库 term_dicts 表（全局词条）。

运行方式: python scripts/seed_terms.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def main():
    from backend.database import Base, platform_engine
    from backend.models.term_dict import TermDict  # noqa: F401  注册到 metadata
    from backend.services.term_service import seed_builtin_terms, list_terms

    # 建表（幂等；只建本脚本负责的表，不动其他子代理的模型注册）
    async with platform_engine.begin() as conn:
        await conn.run_sync(TermDict.__table__.create, checkfirst=True)

    created = await seed_builtin_terms()
    if created:
        print(f"✅ 新写入内置术语 {len(created)} 条: {created}")
    else:
        print("⏭️  内置术语已存在，跳过（幂等）")

    terms = await list_terms()
    print(f"\n当前全局术语共 {len(terms)} 条:")
    for t in terms:
        print(f"  - {t['term']:<8} [{t['category'] or '-'}] hints={t['field_hints']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
