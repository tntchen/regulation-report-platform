"""
文档解析服务
上传文档 → 统一文本内容。
- TXT/MD：直接读取（UTF-8，容错 GBK）
- PDF/DOCX：环境具备对应解析库时支持，否则明确报"暂不支持"（不硬装重依赖）
"""

from typing import Tuple


def parse_document(filename: str, content: bytes) -> Tuple[str, str]:
    """解析上传文档，返回 (文本内容, 格式标记)
    解析失败抛出 ValueError，消息中说明原因"""
    lower = (filename or "").lower()

    if lower.endswith((".txt", ".md")):
        return _decode_text(content), "txt"

    if lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # requirements 中为 PyPDF2，优先尝试 pypdf
        except ImportError:
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                raise ValueError("PDF 解析库不可用，PDF 文档暂不支持，请上传 TXT 版本")
        import io
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip():
            raise ValueError("PDF 文本提取为空（可能为扫描件），暂不支持")
        return text, "pdf"

    if lower.endswith((".docx",)):
        try:
            import docx
        except ImportError:
            raise ValueError("Word 解析库不可用，DOCX 文档暂不支持，请上传 TXT 版本")
        import io
        document = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in document.paragraphs), "docx"

    raise ValueError(f"不支持的文档格式: {filename}（支持 TXT/MD，PDF/DOCX 视环境而定）")


def _decode_text(content: bytes) -> str:
    """文本解码：优先 UTF-8，回退 GBK"""
    for encoding in ("utf-8", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("文本编码无法识别（支持 UTF-8 / GBK）")
