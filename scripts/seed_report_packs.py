"""
内置场景包种子脚本
把 G01 / G11 / EAST_JJ 三个内置场景包幂等灌入平台库 report_packs 表。

运行方式: python scripts/seed_report_packs.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def main():
    from backend.database import Base, platform_engine
    from backend import models  # noqa: F401  确保 ReportPack 注册到 metadata
    from backend.services.report_pack_service import seed_builtin_packs, list_packs

    # 建表（幂等）
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    created = await seed_builtin_packs()
    if created:
        print(f"✅ 新写入内置场景包: {created}")
    else:
        print("⏭️  内置场景包已存在，跳过（幂等）")

    packs = await list_packs()
    print(f"\n当前场景包共 {len(packs)} 个:")
    for p in packs:
        print(f"  - {p['id']:<8} {p['report_name']}  [{p['report_type']}] "
              f"目标表={p['target_table']} 状态={p['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
