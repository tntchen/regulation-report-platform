"""
制度文档版本对比服务（regulation_diff）
职责：对同一制度的新旧两个版本做结构化 diff，辅助评估口径变化影响面。

Demo 深度设计：
- 按 Markdown 标题（# ~ ######）切分"节"，以节标题为键做增/删/改对比；
- 变更节用 difflib.SequenceMatcher 计算相似度（无第三方依赖）；
- 从变更段落中提取口径敏感关键词（金额/天数/分类/比例/利率等上下文），
  帮助报送开发人员快速定位"哪类口径被动了"。
"""

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List

# Markdown 标题行（# ~ ######）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# 口径敏感关键词：出现即认为该变更可能影响取数口径
_CALIPER_KEYWORDS = [
    "金额", "余额", "本金", "利息", "天数", "逾期", "分类", "比例", "占比",
    "口径", "费率", "利率", "期限", "阈值", "上限", "下限", "万元", "亿元",
    "毛额", "净额", "账面", "折扣率", "权重",
]

# 相似度阈值：低于该值判定为"变更节"（容忍标点/空白的细微差异）
_CHANGED_THRESHOLD = 0.98

# 片段截断长度（避免响应体过大）
_EXCERPT_LEN = 120
_CONTEXT_LEN = 60


def _split_sections(content: str) -> List[Dict[str, str]]:
    """按 Markdown 标题把文档切分为节。

    返回 [{title, content}]，标题前的文字归入"（文档开头）"节（仅当非空）。
    无标题的纯文本整体作为一节（title="（全文）"）。
    """
    sections: List[Dict[str, str]] = []
    title = None
    buf: List[str] = []

    def flush():
        nonlocal title, buf
        body = "\n".join(buf).strip()
        if title is None:
            if body:
                sections.append({"title": "（文档开头）", "content": body})
        else:
            sections.append({"title": title, "content": body})
        buf = []

    for line in (content or "").splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            flush()
            title = m.group(2).strip()
        else:
            buf.append(line)
    flush()

    if not sections and (content or "").strip():
        sections.append({"title": "（全文）", "content": content.strip()})
    return sections


def _excerpt(text: str, limit: int = _EXCERPT_LEN) -> str:
    """取片段前 N 字符作为预览"""
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _extract_keywords(section_title: str, text: str) -> List[Dict[str, str]]:
    """从变更段落中提取口径敏感关键词及所在行上下文（按 关键词+节 去重）"""
    found: List[Dict[str, str]] = []
    seen = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for kw in _CALIPER_KEYWORDS:
            if kw in line and (kw, section_title) not in seen:
                seen.add((kw, section_title))
                found.append({
                    "keyword": kw,
                    "section": section_title,
                    "context": _excerpt(line, _CONTEXT_LEN),
                })
    return found


def compare_documents(old_content: str, new_content: str) -> Dict[str, Any]:
    """对比新旧制度文本，输出结构化 diff。

    返回:
      summary: 一句话总结
      added_sections / removed_sections: 节标题列表
      changed_sections: [{title, similarity, old_excerpt, new_excerpt}]
      unchanged_sections: 未变化节标题列表
      affected_keywords: 变更段落中的口径敏感关键词（含上下文）
    """
    old_sections = _split_sections(old_content)
    new_sections = _split_sections(new_content)
    old_map = {s["title"]: s["content"] for s in old_sections}
    new_map = {s["title"]: s["content"] for s in new_sections}

    added = [t for t in new_map if t not in old_map]
    removed = [t for t in old_map if t not in new_map]

    changed: List[Dict[str, Any]] = []
    unchanged: List[str] = []
    for title in old_map.keys() & new_map.keys():
        old_body, new_body = old_map[title], new_map[title]
        if old_body == new_body:
            unchanged.append(title)
            continue
        similarity = SequenceMatcher(None, old_body, new_body).ratio()
        if similarity >= _CHANGED_THRESHOLD:
            unchanged.append(title)  # 仅标点/空白差异，视为未实质变更
            continue
        changed.append({
            "title": title,
            "similarity": round(similarity, 4),
            "old_excerpt": _excerpt(old_body),
            "new_excerpt": _excerpt(new_body),
        })

    # 受影响关键词：扫描新增 + 变更 + 删除的节
    keywords: List[Dict[str, str]] = []
    for title in added:
        keywords.extend(_extract_keywords(title, new_map[title]))
    for item in changed:
        keywords.extend(_extract_keywords(item["title"], new_map[item["title"]]))
        keywords.extend(_extract_keywords(item["title"], old_map[item["title"]]))
    for title in removed:
        keywords.extend(_extract_keywords(title, old_map[title]))
    # 去重（同一关键词在同一节只保留一次）
    dedup: List[Dict[str, str]] = []
    seen = set()
    for kw in keywords:
        key = (kw["keyword"], kw["section"])
        if key not in seen:
            seen.add(key)
            dedup.append(kw)

    kw_names = sorted({k["keyword"] for k in dedup})
    summary = (
        f"新增 {len(added)} 节、删除 {len(removed)} 节、变更 {len(changed)} 节"
        f"（未变 {len(unchanged)} 节）"
        + (f"；涉及口径关键词：{'、'.join(kw_names[:8])}" if kw_names else "")
    )

    return {
        "summary": summary,
        "added_sections": added,
        "removed_sections": removed,
        "changed_sections": changed,
        "unchanged_sections": unchanged,
        "affected_keywords": dedup,
    }
