"""
检索质量评测脚本（L2-D8）
基于 38 份预置制度文档构造"业务问题 → 期望命中文档"评测集（16 条，
含同义/口语化表达，用于验证语义通道价值），输出 Top-1/Top-3/Top-5 命中率基线。

运行方式: python scripts/eval_retrieval.py
前置: 已执行 python scripts/seed_regulations.py（38 份文档已索引）
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 评测集：question = 业务问题（含口语化/同义表达）；expect = 期望文档标题关键词
EVAL_SET = [
    {"question": "个人住房贷款逾期90天怎么报送", "expect": "EAST_信贷业务借据"},
    {"question": "房贷逾期三个月以上按整笔本金算吗", "expect": "EAST_信贷业务借据"},  # 同义：90天≈三个月
    {"question": "贷款余额要不要包含资本化利息", "expect": "EAST_信贷业务借据"},
    {"question": "客户身份证号和手机号怎么脱敏", "expect": "通用_数据安全与个人信息保护"},
    {"question": "五级分类次级可疑损失怎么划分", "expect": "G11_资产质量五级分类"},
    {"question": "可疑交易报告什么情况下要报", "expect": "反洗钱_可疑交易"},
    {"question": "大额现金交易多少金额要上报", "expect": "反洗钱_大额交易"},
    {"question": "普惠小微首贷户怎么统计", "expect": "普惠小微_首贷户统计"},
    {"question": "小微企业考核口径两增两控是什么", "expect": "普惠小微_考核口径"},
    {"question": "碳减排支持工具怎么报送", "expect": "绿色金融_碳减排支持工具"},
    {"question": "房地产企业贷款集中度红线是多少", "expect": "房地产贷款集中度_管理要求"},
    {"question": "个人征信账户状态结清怎么映射", "expect": "征信_个人信用信息"},
    {"question": "执行利率和LPR的浮动区间怎么算", "expect": "利率报备_总体制度"},
    {"question": "存款保险投保机构有哪些统计要求", "expect": "存款保险_投保机构统计"},
    {"question": "理财产品投资情况怎么报", "expect": "理财_投资情况表"},
    {"question": "跨境资本项目外汇怎么申报", "expect": "外汇_资本项目"},
]


async def main():
    from backend.services.vector_service import VectorService

    vs = VectorService("T001")
    stats = await vs.stats()
    print(f"向量库: {stats['indexed_docs']} 文档 / {stats['chunk_count']} 切片 / "
          f"{stats['vector_dimension']} 维 / provider={stats['embedding_provider']}")
    print("=" * 64)

    top1 = top3 = top5 = 0
    for item in EVAL_SET:
        r = await vs.retrieve(item["question"], top_k=5)
        titles = [x["doc_title"] for x in r["results"]]
        ranks = [i + 1 for i, t in enumerate(titles) if item["expect"] in t]
        hit1 = bool(ranks and ranks[0] == 1)
        hit3 = bool(ranks and ranks[0] <= 3)
        hit5 = bool(ranks)
        top1 += hit1
        top3 += hit3
        top5 += hit5
        mark = "✅" if hit1 else ("🟡" if hit5 else "❌")
        best = r["results"][0] if r["results"] else None
        print(f"{mark} [{item['expect']}] {item['question']}")
        if best:
            print(f"   Top1: {best['doc_title']} (融合 {best['relevance_score']} / "
                  f"向量 {best['vector_score']} / 文本 {best['text_score']})")

    n = len(EVAL_SET)
    print("=" * 64)
    print(f"命中率基线: Top-1 {top1}/{n} ({top1/n:.0%}) | "
          f"Top-3 {top3}/{n} ({top3/n:.0%}) | Top-5 {top5}/{n} ({top5/n:.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
