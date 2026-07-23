"""
预置租户种子脚本
把 core/tenant_context.PRESET_TENANTS 的预置租户配置灌入平台库 tenants 表（幂等）。
Day10 起 tenants 表为租户配置的权威来源，本脚本独立运行，不挂应用启动钩子。

运行方式: python scripts/seed_tenants.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def main():
    from backend.database import Base, platform_engine
    from backend import models  # noqa: F401  确保所有模型注册到 Base.metadata
    from backend.services.tenant_service import seed_preset_tenants, list_all_tenants

    # 建表（幂等）
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    created = await seed_preset_tenants()
    tenants = await list_all_tenants()

    if created:
        print(f"✅ 新写入预置租户: {', '.join(created)}")
    else:
        print("ℹ️  预置租户已存在，跳过（幂等）")
    print(f"当前 tenants 表共 {len(tenants)} 个租户:")
    for t in tenants:
        print(f"  - {t['id']}  {t['name']}  ({t['code']})  [{t['status']}]")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
