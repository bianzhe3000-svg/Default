"""
PDF → 飞书云文档 完整方案（v3：追加式同步 Block 插入，修复乱序 bug）

v2 的致命 bug（导致完全乱序）：
  inserted = len(data.get("data", {}).get("children", []))
  current_index += inserted
  ↑ 飞书 API 响应里 children 字段可能为空列表或缺失，导致 inserted=0，
    每一批都插入到 index=0，结果是完全倒序（最后批在最顶，第一批在最底）。

v3 修复原理：
  彻底去掉 index 参数。飞书 API 文档说明：
    「不填或超出实际 children 数量时，默认从最后插入」
  只要每批之间 await 等待完成，服务端就按串行顺序追加，
  顺序完全由 Python 的串行 for 循环保证，不依赖任何计数。

流程：
  1. 解析 PDF（PyMuPDF 或 MinerU content_list.json）
  2. 按章节顺序逐节 LLM 总结（order 字段锁定，禁止并发）
  3. SectionSummary → 飞书 Block JSON（纯内存转换）
  4. 创建空白云文档
  5. 分批追加 blocks（无 index，自动 append，严格串行 await）

依赖安装：
  pip install PyMuPDF httpx openai

环境变量：
  OPENAI_API_KEY       OpenAI API Key
  FEISHU_APP_ID        飞书应用 App ID
  FEISHU_APP_SECRET    飞书应用 App Secret
  FEISHU_FOLDER_TOKEN  目标文件夹 token（飞书云空间文件夹 URL 中 folder/ 后的字符串）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from openai import AsyncOpenAI

from src.utils.logger import get_logger
from src.utils.retry import async_retry

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PdfToFeishuConfig:
    # OpenAI
    openai_api_key: str = field(default_factory=lambda: os.environ["OPENAI_API_KEY"])
    openai_model: str = "gpt-4o"

    # 飞书
    feishu_app_id: str = field(default_factory=lambda: os.environ["FEISHU_APP_ID"])
    feishu_app_secret: str = field(default_factory=lambda: os.environ["FEISHU_APP_SECRET"])
    feishu_folder_token: str = field(default_factory=lambda: os.environ["FEISHU_FOLDER_TOKEN"])

    # PDF 解析
    pages_per_chunk: int = 5           # 无 TOC 时每块的页数
    max_chars_per_section: int = 4000  # 每节最多喂给 LLM 的字符数

    # LLM
    summary_max_tokens: int = 800
    summary_temperature: float = 0.3

    # 飞书 Block 写入
    block_batch_size: int = 50    # 每批最多 block 数（飞书 API 上限 50）
    block_batch_delay: float = 0.5  # 批次间等待秒数（防限流）


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Section:
    """PDF 章节。order 是全局排序键，整个流程不可变。"""
    order: int
    level: int       # 标题层级 1/2/3
    title: str
    content: str
    start_page: int = 0
    end_page: int = 0


@dataclass
class SectionSummary:
    """LLM 总结结果。"""
    order: int
    level: int
    title: str
    summary: str
    key_points: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 第一步：PDF 解析
# ─────────────────────────────────────────────────────────────────────────────

class PDFParser:
    """使用 PyMuPDF 解析 PDF。优先使用 TOC，无 TOC 时按固定页数分块。"""

    def __init__(self, pdf_path: str, config: PdfToFeishuConfig):
        try:
            import fitz
        except ImportError as exc:
            raise ImportError("请先安装 PyMuPDF：pip install PyMuPDF") from exc
        self.doc = fitz.open(pdf_path)
        self.config = config

    def extract_sections(self) -> list[Section]:
        toc = self.doc.get_toc()
        if toc:
            logger.info("检测到 TOC，共 %d 条目", len(toc))
            return self._from_toc(toc)
        logger.warning("无 TOC，按每 %d 页分块", self.config.pages_per_chunk)
        return self._from_pages()

    def _from_toc(self, toc: list) -> list[Section]:
        sections = []
        for i, (level, title, page) in enumerate(toc):
            next_page = toc[i + 1][2] if i + 1 < len(toc) else self.doc.page_count
            sections.append(Section(
                order=i,
                level=min(level, 3),
                title=title.strip(),
                content=self._text(page - 1, next_page - 1),
                start_page=page,
                end_page=next_page,
            ))
        return sections

    def _from_pages(self) -> list[Section]:
        sections = []
        chunk = self.config.pages_per_chunk
        for i, start in enumerate(range(0, self.doc.page_count, chunk)):
            end = min(start + chunk, self.doc.page_count)
            sections.append(Section(
                order=i,
                level=1,
                title=f"第 {start + 1}–{end} 页",
                content=self._text(start, end),
                start_page=start + 1,
                end_page=end,
            ))
        return sections

    def _text(self, start: int, end: int) -> str:
        return "\n".join(
            self.doc[p].get_text("text")
            for p in range(start, min(end, self.doc.page_count))
        ).strip()

    def close(self) -> None:
        self.doc.close()


def load_sections_from_mineru(content_list_path: str) -> list[Section]:
    """
    从 MinerU 输出的 content_list.json 加载章节。
    MinerU 命令：mineru -p your.pdf -o ./output -m hybrid
    json 路径：./output/<filename>/auto/content_list.json
    """
    with open(content_list_path, encoding="utf-8") as f:
        items = json.load(f)

    sections: list[Section] = []
    order = 0
    cur_title, cur_level, cur_buf = "正文", 1, []

    for item in items:
        t = item.get("type", "")
        if t == "title":
            if cur_buf:
                sections.append(Section(
                    order=order, level=cur_level, title=cur_title,
                    content="\n".join(cur_buf),
                ))
                order += 1
                cur_buf = []
            cur_title = item.get("text", "").strip()
            cur_level = min(int(item.get("level", 1)), 3)
        elif t in ("text", "list"):
            if txt := item.get("text", "").strip():
                cur_buf.append(txt)
        elif t == "table":
            cur_buf.append("[表格]")
        elif t == "image":
            cap = item.get("img_caption", "")
            cur_buf.append(f"[图片：{cap}]" if cap else "[图片]")

    if cur_buf:
        sections.append(Section(order=order, level=cur_level, title=cur_title,
                                content="\n".join(cur_buf)))
    return sections


# ─────────────────────────────────────────────────────────────────────────────
# 第二步：LLM 分段总结（严格串行）
# ─────────────────────────────────────────────────────────────────────────────

class Summarizer:
    """逐节调用 GPT-4，严格串行，禁止 asyncio.gather 并发。"""

    _SYSTEM = (
        "你是专业的文档分析助手。"
        "准确总结章节内容，保持原始信息完整性，不添加章节中没有的内容。"
    )

    def __init__(self, config: PdfToFeishuConfig):
        self.client = AsyncOpenAI(api_key=config.openai_api_key)
        self.config = config

    @async_retry(max_attempts=3, retryable_exceptions=(Exception,))
    async def _call_llm(self, section: Section) -> str:
        content = section.content[: self.config.max_chars_per_section]
        resp = await self.client.chat.completions.create(
            model=self.config.openai_model,
            messages=[
                {"role": "system", "content": self._SYSTEM},
                {"role": "user", "content": (
                    f"章节标题：{section.title}\n\n"
                    f"章节内容：\n{content}\n\n"
                    "要求：\n"
                    "1. 中文总结，200-400 字\n"
                    "2. 保留所有重要数据、结论、关键信息\n"
                    "3. 最后用「核心要点：」列出 3-5 条要点，每条以「-」开头\n"
                    "直接输出内容，不要说「以下是总结」之类的前言。"
                )},
            ],
            max_tokens=self.config.summary_max_tokens,
            temperature=self.config.summary_temperature,
        )
        return resp.choices[0].message.content.strip()

    async def summarize_section(self, section: Section) -> SectionSummary:
        raw = await self._call_llm(section)
        summary, key_points = raw, []
        if "核心要点：" in raw:
            body, rest = raw.split("核心要点：", 1)
            summary = body.strip()
            key_points = [
                ln.lstrip("-• ").strip()
                for ln in rest.strip().splitlines()
                if ln.strip()
            ]
        logger.info("[%d] 总结完成：%s", section.order + 1, section.title)
        return SectionSummary(
            order=section.order, level=section.level,
            title=section.title, summary=summary, key_points=key_points,
        )

    async def summarize_all(self, sections: list[Section]) -> list[SectionSummary]:
        ordered = sorted(sections, key=lambda s: s.order)
        total = len(ordered)
        results: list[SectionSummary] = []
        for sec in ordered:
            if not sec.content.strip():
                logger.warning("跳过空节 [%d]：%s", sec.order, sec.title)
                continue
            logger.info("总结 [%d/%d]：%s（%d 字）", sec.order + 1, total,
                        sec.title, len(sec.content))
            results.append(await self.summarize_section(sec))
            await asyncio.sleep(0.2)
        return sorted(results, key=lambda r: r.order)  # 兜底排序


# ─────────────────────────────────────────────────────────────────────────────
# 第三步：生成 Markdown（仅供本地调试保存，不再用于飞书导入）
# ─────────────────────────────────────────────────────────────────────────────

class MarkdownBuilder:
    @staticmethod
    def build(summaries: list[SectionSummary], doc_title: str = "") -> str:
        lines: list[str] = []
        if doc_title:
            lines += [f"# {doc_title}", "",
                      f"> 由 AI 自动生成 · {time.strftime('%Y-%m-%d %H:%M')}", "", "---", ""]
        for s in sorted(summaries, key=lambda x: x.order):
            level = s.level + (1 if doc_title else 0)
            lines += [f"{'#' * min(level, 4)} {s.title}", "", s.summary, ""]
            if s.key_points:
                lines += ["**核心要点：**", ""]
                lines += [f"- {p}" for p in s.key_points]
                lines.append("")
            lines += ["---", ""]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 第四步：Convert — SectionSummary → 飞书 Block JSON
# ─────────────────────────────────────────────────────────────────────────────

# 飞书 Block Type 常量（来源：飞书开放平台文档 /docx/v1/document-block）
_BT_TEXT = 2
_BT_H = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8, 7: 9, 8: 10, 9: 11}  # heading1-9
_BT_BULLET = 12
_BT_DIVIDER = 22

_MAX_RUN_BYTES = 2000  # 单个 text_run 最大字节数（UTF-8）


def _run(text: str, bold: bool = False, italic: bool = False) -> dict:
    style = {}
    if bold:
        style["bold"] = True
    if italic:
        style["italic"] = True
    el: dict = {"text_run": {"content": text}}
    if style:
        el["text_run"]["text_element_style"] = style
    return el


def _runs(text: str, **kw) -> list[dict]:
    """把长文本拆成多个 text_run，每段 ≤ _MAX_RUN_BYTES 字节。"""
    if len(text.encode()) <= _MAX_RUN_BYTES:
        return [_run(text, **kw)]
    parts, remaining = [], text
    while remaining:
        trial = remaining
        while len(trial.encode()) > _MAX_RUN_BYTES:
            cut = max(1, len(trial) * _MAX_RUN_BYTES // len(trial.encode()))
            trial = remaining[:cut]
        parts.append(_run(trial, **kw))
        remaining = remaining[len(trial):]
    return parts


def _text_block(content: str, bold: bool = False, italic: bool = False) -> dict:
    return {"block_type": _BT_TEXT, "text": {"elements": _runs(content, bold=bold, italic=italic)}}


def _heading_block(content: str, level: int) -> dict:
    level = max(1, min(level, 9))
    key = f"heading{level}"
    return {"block_type": _BT_H[level], key: {"elements": [_run(content)]}}


def _bullet_block(content: str) -> dict:
    return {"block_type": _BT_BULLET, "bullet": {"elements": _runs(content)}}


def _divider_block() -> dict:
    return {"block_type": _BT_DIVIDER, "divider": {}}


class FeishuBlockConverter:
    """
    SectionSummary 列表 → 飞书 Block JSON 列表。
    纯内存转换，无 API 调用，顺序由 Python 列表下标保证。
    """

    @staticmethod
    def convert(summaries: list[SectionSummary], doc_title: str = "") -> list[dict]:
        blocks: list[dict] = []
        ordered = sorted(summaries, key=lambda s: s.order)

        if doc_title:
            blocks.append(_text_block(
                f"由 AI 自动生成 · {time.strftime('%Y-%m-%d %H:%M')}", italic=True
            ))
            blocks.append(_divider_block())

        for s in ordered:
            # 有文档标题时层级下移（标题本身占 heading1）
            h_level = max(1, min(s.level + (1 if doc_title else 0), 9))
            blocks.append(_heading_block(s.title, h_level))

            # 正文：按双换行拆段，降级为单换行兜底
            paras = [p.strip() for p in s.summary.split("\n\n") if p.strip()] or \
                    [p.strip() for p in s.summary.split("\n") if p.strip()]
            for para in paras:
                blocks.append(_text_block(para))

            # 核心要点
            if s.key_points:
                blocks.append(_text_block("核心要点：", bold=True))
                for pt in s.key_points:
                    if pt.strip():
                        blocks.append(_bullet_block(pt.strip()))

            blocks.append(_divider_block())

        logger.info("Block 转换完成，共 %d 个", len(blocks))

        # 打印前 5 个 block 标题，供调试核对顺序
        for i, b in enumerate(blocks[:5]):
            for hk in [f"heading{n}" for n in range(1, 10)]:
                if hk in b:
                    first_content = b[hk]["elements"][0]["text_run"]["content"]
                    logger.debug("  block[%d] %s: %s", i, hk, first_content)
                    break

        return blocks


# ─────────────────────────────────────────────────────────────────────────────
# 第五步：写入飞书云文档（追加式，无 index，彻底修复乱序）
# ─────────────────────────────────────────────────────────────────────────────

class FeishuDocWriter:
    """
    创建飞书云文档并追加式写入所有 Block。

    v3 核心修复：
      去掉 insert 请求中的 index 参数。
      飞书 API：「index 不填时默认从最后插入」。
      每批之间有 await，Python 保证串行，服务端按到达顺序追加，不可能乱序。

    对比 v2 的 bug（index 计数错误）：
      v2 用 len(data["data"]["children"]) 计算 inserted，
      该字段可能为空列表 → inserted=0 → 所有批次都插入 index=0
      → 最后一批在最顶，完全倒序。

    飞书 API 权限要求：
      - docx:document（创建/编辑云文档）
      应用类型：企业自建应用，使用 tenant_access_token（无需 user_access_token）
    """

    _BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, config: PdfToFeishuConfig):
        self.config = config
        self._token: str | None = None
        self._token_expire: float = 0.0

    # ── 鉴权（App Token）────────────────────────────────────────────────────

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token

        resp = await client.post(
            f"{self._BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.config.feishu_app_id,
                  "app_secret": self.config.feishu_app_secret},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书鉴权失败: code={data['code']}, msg={data.get('msg')}")
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200)
        logger.info("tenant_access_token 获取成功，有效期 %ds", data.get("expire", 7200))
        return self._token  # type: ignore[return-value]

    def _hdr(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ── 创建空白文档 ─────────────────────────────────────────────────────────

    @async_retry(max_attempts=3, retryable_exceptions=(httpx.HTTPError, RuntimeError))
    async def _create_doc(self, client: httpx.AsyncClient, token: str, title: str) -> str:
        """创建空白文档，返回 document_id（同时也是根 block 的 block_id）。"""
        resp = await client.post(
            f"{self._BASE}/docx/v1/documents",
            headers=self._hdr(token),
            json={"folder_token": self.config.feishu_folder_token, "title": title},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"创建文档失败: code={data['code']}, msg={data.get('msg')}")
        doc_id = data["data"]["document"]["document_id"]
        logger.info("空白文档已创建: document_id=%s", doc_id)
        return doc_id

    # ── 追加一批 Block（v3 关键：无 index 参数）──────────────────────────────

    @async_retry(max_attempts=3, retryable_exceptions=(httpx.HTTPError,))
    async def _append_batch(
        self,
        client: httpx.AsyncClient,
        token: str,
        doc_id: str,
        blocks: list[dict],
        batch_num: int,
    ) -> None:
        """
        向文档根 block 追加一批 children。

        ★ 关键：不传 index 字段。
          飞书 API 规范：「不填时默认从最后插入」
          这样无论 API 返回什么，顺序都由 Python for 循环决定。
        """
        resp = await client.post(
            f"{self._BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            headers=self._hdr(token),
            params={"document_revision_id": -1},  # -1 = 最新版本
            json={"children": blocks},             # ← 无 index，追加到末尾
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                f"第 {batch_num} 批插入失败: code={data['code']}, msg={data.get('msg')}"
            )

        # 仅供诊断日志，不用于任何计数逻辑
        api_children_count = len(data.get("data", {}).get("children", []))
        logger.info(
            "  第 %d 批追加成功: 发送=%d, API返回children数=%d",
            batch_num, len(blocks), api_children_count,
        )

    # ── 分批串行追加全部 Block ────────────────────────────────────────────────

    async def _append_all(
        self,
        client: httpx.AsyncClient,
        token: str,
        doc_id: str,
        blocks: list[dict],
    ) -> None:
        """
        分批、串行、追加式写入所有 block。

        顺序保证链：
          Python for 循环（串行）
          → await 等待每批 HTTP 完成后再发下一批
          → 飞书服务端按请求到达顺序追加
          → 文档 block 顺序 == blocks 列表顺序
        """
        size = self.config.block_batch_size
        total = len(blocks)
        total_batches = (total + size - 1) // size
        logger.info("开始写入 %d 个 blocks，共 %d 批（每批 %d）", total, total_batches, size)

        for batch_idx, start in enumerate(range(0, total, size), start=1):
            batch = blocks[start: start + size]
            logger.info(
                "追加第 %d/%d 批: blocks[%d:%d]",
                batch_idx, total_batches, start, start + len(batch),
            )
            await self._append_batch(client, token, doc_id, batch, batch_idx)

            # 批次间延迟，防止飞书限流
            if start + size < total:
                await asyncio.sleep(self.config.block_batch_delay)

        logger.info("全部 %d 个 blocks 写入完成", total)

    # ── 主入口 ───────────────────────────────────────────────────────────────

    async def write_document(self, blocks: list[dict], title: str) -> str:
        """创建文档并追加所有 blocks，返回文档 URL。"""
        async with httpx.AsyncClient() as client:
            token = await self._get_token(client)
            doc_id = await self._create_doc(client, token, title)
            await self._append_all(client, token, doc_id, blocks)

        url = f"https://bytedance.feishu.cn/docx/{doc_id}"
        logger.info("文档就绪: %s", url)
        return url


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

async def pdf_to_feishu(
    pdf_path: str,
    doc_title: str,
    config: Optional[PdfToFeishuConfig] = None,
    mineru_content_list: Optional[str] = None,
    save_md_to: Optional[str] = None,
) -> str:
    """
    PDF → 飞书云文档主流程。

    Args:
        pdf_path:            PDF 文件路径
        doc_title:           飞书文档标题
        config:              配置（None 时从环境变量读取）
        mineru_content_list: MinerU content_list.json 路径（可选，适合复杂排版）
        save_md_to:          本地 Markdown 保存路径（可选，调试用）

    Returns:
        飞书云文档 URL
    """
    if config is None:
        config = PdfToFeishuConfig()

    logger.info("=" * 60)
    logger.info("开始处理：%s → 飞书文档《%s》", pdf_path, doc_title)

    # ── 1. 解析 PDF ──────────────────────────────────────────────────────────
    if mineru_content_list:
        logger.info("使用 MinerU 解析：%s", mineru_content_list)
        sections = load_sections_from_mineru(mineru_content_list)
    else:
        logger.info("使用 PyMuPDF 解析")
        parser = PDFParser(pdf_path, config)
        try:
            sections = parser.extract_sections()
        finally:
            parser.close()

    if not sections:
        raise ValueError("PDF 解析结果为空，请检查文件是否损坏或为扫描件")

    logger.info("识别到 %d 个章节：", len(sections))
    for s in sections:
        logger.info("  [%02d] %s%s（%d 字）", s.order, "  " * (s.level - 1),
                    s.title, len(s.content))

    # ── 2. 逐节 LLM 总结 ─────────────────────────────────────────────────────
    logger.info("开始 LLM 总结（严格串行）...")
    summaries = await Summarizer(config).summarize_all(sections)
    logger.info("总结完成，共 %d 节", len(summaries))

    # ── 3. 本地保存 Markdown（调试用）────────────────────────────────────────
    if save_md_to:
        md = MarkdownBuilder.build(summaries, doc_title=doc_title)
        Path(save_md_to).write_text(md, encoding="utf-8")
        logger.info("Markdown 已保存：%s（%d 字符）", save_md_to, len(md))

    # ── 4. Convert：SectionSummary → 飞书 Block JSON ─────────────────────────
    blocks = FeishuBlockConverter.convert(summaries, doc_title=doc_title)

    # ── 5. 创建文档 + 追加 Block ──────────────────────────────────────────────
    url = await FeishuDocWriter(config).write_document(blocks, title=doc_title)

    logger.info("完成！飞书文档：%s", url)
    logger.info("=" * 60)
    return url
