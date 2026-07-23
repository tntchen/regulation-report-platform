"""
预置制度文档导入脚本
将 02_制度文档向量库/ 下 38 份真实制度 txt 批量导入为 T001 租户的预置制度文档：
  1. 复制文件到 data/tenants/T001/regulations/（原目录只读，不修改）
  2. 元数据落 SQLite（regulation_documents 表）
  3. 切片 + 向量化索引落租户向量库
  4. 打印每个文档的切片数与状态汇总

运行方式: python scripts/seed_regulations.py
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 制度文档源目录（相对项目根的上一级 sheet_project 目录）
SOURCE_ROOT = PROJECT_ROOT.parent / "02_制度文档向量库"
TENANT_ID = "T001"


async def main():
    from backend.config import settings
    from backend.database import Base, platform_engine, PlatformSessionLocal
    from backend.models.document import RegulationDocument
    from backend.services.vector_service import VectorService
    from sqlalchemy import select

    # 建表（幂等）
    async with platform_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if not SOURCE_ROOT.exists():
        print(f"❌ 制度文档源目录不存在: {SOURCE_ROOT}")
        return 1

    vs = VectorService(TENANT_ID)
    upload_dir = os.path.join(settings.upload_dir, TENANT_ID, "regulations")
    os.makedirs(upload_dir, exist_ok=True)

    txt_files = sorted(SOURCE_ROOT.rglob("*.txt"))
    print(f"发现制度文档 {len(txt_files)} 份，开始导入租户 {TENANT_ID}...\n")

    success = 0
    failed = []
    total_chunks = 0

    for txt_path in txt_files:
        # 文档类型取自一级目录名（去掉数字前缀）
        category = txt_path.parent.name
        doc_type = category.split("_", 1)[1] if "_" in category else category
        filename = txt_path.name
        title = txt_path.stem

        try:
            content = txt_path.read_text(encoding="utf-8")
            doc_id = str(uuid.uuid4())

            # 复制到租户目录
            dest_path = os.path.join(upload_dir, f"{doc_id}_{filename}")
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(content)

            # 元数据落库（已存在同名文档则跳过重建，幂等）
            async with PlatformSessionLocal() as session:
                existing = await session.execute(
                    select(RegulationDocument).where(
                        RegulationDocument.tenant_id == TENANT_ID,
                        RegulationDocument.filename == filename
                    )
                )
                if existing.scalars().first():
                    print(f"  [跳过] {filename}（已存在）")
                    continue
                session.add(RegulationDocument(
                    id=doc_id, tenant_id=TENANT_ID, filename=filename,
                    doc_type=doc_type, file_path=dest_path,
                    size=len(content.encode("utf-8")), status="indexing",
                    uploaded_by="seed"
                ))
                await session.commit()

            # 索引
            result = await vs.index_document(doc_id, content, doc_type, title)
            chunks = result["chunk_count"]
            total_chunks += chunks

            async with PlatformSessionLocal() as session:
                row = await session.get(RegulationDocument, doc_id)
                row.status = "indexed"
                row.chunk_count = chunks
                row.vector_count = chunks
                await session.commit()

            success += 1
            print(f"  [成功] {filename:40s} 类型={doc_type:12s} 切片={chunks}")

        except Exception as e:
            failed.append((filename, str(e)))
            print(f"  [失败] {filename}: {e}")

    print("\n" + "=" * 60)
    print(f"导入完成: 成功 {success} / 失败 {len(failed)} / 总切片 {total_chunks}")
    stats = vs.stats()
    print(f"向量库统计: 文档 {stats['indexed_docs']} / 向量 {stats['vector_count']} / 维度 {stats['vector_dimension']}")
    if failed:
        for name, err in failed:
            print(f"  失败明细: {name} -> {err}")
    print("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
