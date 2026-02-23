"""
PDF → 飞书云文档 完整方案

流程：
  1. 解析 PDF（PyMuPDF 或 MinerU content_list.json）
  2. 按章节顺序逐节用 GPT-4 总结（顺序由 order 字段锁定，禁止并发）
  3. 按 order 拼接生成完整 Markdown 文本
  4. 上传 .md 文件 → 飞书 Import API 整体导入为云文档
     （彻底绕开 Block API 的顺序问题）

依赖安装：
  pip install PyMuPDF lark-oapi

环境变量：
  OPENAI_API_KEY       OpenAI API Key
  FEISHU_APP_ID        飞书应用 App ID
  FEISHU_APP_SECRET    飞书应用 App Secret
  FEISHU_FOLDER_TOKEN  目标文件夹 token（从飞书云空间 URL 中获取）

用法示例：
  python run_pdf_to_feishu.py report.pdf --title "季度报告总结"
  python run_pdf_to_feishu.py report.pdf --title "技术文档" --mineru ./output/content_list.json --save-md debug.md
"""

from __future__ import annotations

import asyncio
import io
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

    # 解析参数
    pages_per_chunk: int = 5          # 无 TOC 时每块包含的页数
    max_chars_per_section: int = 4000  # 喂给 LLM 的最大字符数/节

    # LLM 参数
    summary_max_tokens: int = 800
    summary_temperature: float = 0.3

    # 飞书导入轮询
    import_timeout_sec: int = 120     # 最多等待导入完成的秒数


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Section:
    """PDF 中的一个章节，order 是全局排序键，全程不可变。"""
    order: int
    level: int        # 标题层级 1/2/3
    title: str
    content: str      # 提取的原始文字
    start_page: int = 0
    end_page: int = 0


@dataclass
class SectionSummary:
    """LLM 对单个章节的总结结果。"""
    order: int
    level: int
    title: str
    summary: str
    key_points: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 第一步：PDF 解析
# ─────────────────────────────────────────────────────────────────────────────

class PDFParser:
    """
    使用 PyMuPDF 解析 PDF。
    优先使用 PDF 内置目录（TOC），无 TOC 时按固定页数分块。
    """

    def __init__(self, pdf_path: str, config: PdfToFeishuConfig):
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise ImportError("请先安装 PyMuPDF：pip install PyMuPDF") from exc

        self._fitz = fitz
        self.doc = fitz.open(pdf_path)
        self.config = config

    def extract_sections(self) -> list[Section]:
        toc = self.doc.get_toc()
        if toc:
            logger.info("检测到 PDF 目录（TOC），共 %d 个条目", len(toc))
            return self._from_toc(toc)
        logger.warning("未检测到 TOC，按每 %d 页分块", self.config.pages_per_chunk)
        return self._from_pages()

    def _from_toc(self, toc: list) -> list[Section]:
        sections: list[Section] = []
        for i, (level, title, page) in enumerate(toc):
            next_page = toc[i + 1][2] if i + 1 < len(toc) else self.doc.page_count
            content = self._extract_text(page - 1, next_page - 1)
            sections.append(Section(
                order=i,
                level=min(level, 3),
                title=title.strip(),
                content=content,
                start_page=page,
                end_page=next_page,
            ))
        return sections

    def _from_pages(self) -> list[Section]:
        sections: list[Section] = []
        total = self.doc.page_count
        chunk = self.config.pages_per_chunk
        for i, start in enumerate(range(0, total, chunk)):
            end = min(start + chunk, total)
            content = self._extract_text(start, end)
            sections.append(Section(
                order=i,
                level=1,
                title=f"第 {start + 1}–{end} 页",
                content=content,
                start_page=start + 1,
                end_page=end,
            ))
        return sections

    def _extract_text(self, start: int, end: int) -> str:
        texts = []
        for p in range(start, min(end, self.doc.page_count)):
            texts.append(self.doc[p].get_text("text"))
        return "\n".join(texts).strip()

    def close(self) -> None:
        self.doc.close()


def load_sections_from_mineru(content_list_path: str) -> list[Section]:
    """
    从 MinerU 输出的 content_list.json 加载章节。

    MinerU 解析命令（推荐用于双栏/复杂排版 PDF）：
      pip install mineru
      mineru -p your.pdf -o ./output -m hybrid

    生成的 content_list.json 位于 ./output/<filename>/auto/content_list.json
    """
    with open(content_list_path, encoding="utf-8") as f:
        content_list = json.load(f)

    sections: list[Section] = []
    order = 0
    current_title = "正文"
    current_level = 1
    current_content: list[str] = []

    for block in content_list:
        btype = block.get("type", "")

        if btype == "title":
            # 遇到新标题时保存当前节
            if current_content:
                sections.append(Section(
                    order=order,
                    level=current_level,
                    title=current_title,
                    content="\n".join(current_content),
                ))
                order += 1
                current_content = []
            current_title = block.get("text", "").strip()
            current_level = min(int(block.get("level", 1)), 3)

        elif btype in ("text", "list"):
            text = block.get("text", "").strip()
            if text:
                current_content.append(text)

        elif btype == "table":
            current_content.append("[表格]")

        elif btype == "image":
            caption = block.get("img_caption", "")
            current_content.append(f"[图片：{caption}]" if caption else "[图片]")

    # 保存最后一节
    if current_content:
        sections.append(Section(
            order=order,
            level=current_level,
            title=current_title,
            content="\n".join(current_content),
        ))

    return sections


# ─────────────────────────────────────────────────────────────────────────────
# 第二步：LLM 分段总结
# ─────────────────────────────────────────────────────────────────────────────

class Summarizer:
    """
    按章节顺序逐节调用 GPT-4 总结。
    严格串行执行，不使用 asyncio.gather，保证处理顺序可预期。
    """

    _SYSTEM_PROMPT = (
        "你是专业的文档分析助手。"
        "任务：准确总结给定章节内容，保持原始信息完整性，不添加章节中没有的内容。"
    )

    def __init__(self, config: PdfToFeishuConfig):
        self.client = AsyncOpenAI(api_key=config.openai_api_key)
        self.config = config

    @async_retry(max_attempts=3, retryable_exceptions=(Exception,))
    async def _call_llm(self, section: Section) -> str:
        content = section.content[: self.config.max_chars_per_section]
        prompt = (
            f"章节标题：{section.title}\n\n"
            f"章节内容：\n{content}\n\n"
            "要求：\n"
            "1. 中文总结，200-400 字\n"
            "2. 保留所有重要数据、结论、关键信息\n"
            "3. 最后用「核心要点：」列出 3-5 条要点，每条以「-」开头\n"
            "直接输出内容，不要说「以下是总结」之类的前言。"
        )
        resp = await self.client.chat.completions.create(
            model=self.config.openai_model,
            messages=[
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=self.config.summary_max_tokens,
            temperature=self.config.summary_temperature,
        )
        return resp.choices[0].message.content.strip()

    async def summarize_section(self, section: Section) -> SectionSummary:
        raw = await self._call_llm(section)

        # 分离正文和要点
        summary, key_points = raw, []
        if "核心要点：" in raw:
            parts = raw.split("核心要点：", 1)
            summary = parts[0].strip()
            key_points = [
                line.lstrip("-• ").strip()
                for line in parts[1].strip().splitlines()
                if line.strip()
            ]

        logger.info("[%d/%s] 已总结：%s", section.order + 1, "?", section.title)
        return SectionSummary(
            order=section.order,
            level=section.level,
            title=section.title,
            summary=summary,
            key_points=key_points,
        )

    async def summarize_all(self, sections: list[Section]) -> list[SectionSummary]:
        """
        严格按 order 升序逐节处理。
        不使用并发，保证输出顺序与输入完全一致。
        """
        ordered = sorted(sections, key=lambda s: s.order)
        total = len(ordered)
        results: list[SectionSummary] = []

        for section in ordered:
            if not section.content.strip():
                logger.warning("跳过空节 [%d]：%s", section.order, section.title)
                continue
            logger.info(
                "正在总结 [%d/%d]：%s（%d 字）",
                section.order + 1, total, section.title, len(section.content),
            )
            result = await self.summarize_section(section)
            results.append(result)
            await asyncio.sleep(0.2)   # 避免 OpenAI 限流

        # 最终兜底排序，保证绝对有序
        return sorted(results, key=lambda r: r.order)


# ─────────────────────────────────────────────────────────────────────────────
# 第三步：生成 Markdown
# ─────────────────────────────────────────────────────────────────────────────

class MarkdownBuilder:
    """
    把有序的 SectionSummary 列表拼接成完整 Markdown 字符串。
    顺序完全由 Python 列表的排列顺序决定，与任何 API 行为无关。
    """

    @staticmethod
    def build(summaries: list[SectionSummary], doc_title: str = "") -> str:
        lines: list[str] = []

        if doc_title:
            lines += [
                f"# {doc_title}",
                "",
                f"> 由 AI 自动生成 · {time.strftime('%Y-%m-%d %H:%M')}",
                "",
                "---",
                "",
            ]

        # 严格按 order 排列，不依赖传入顺序
        for s in sorted(summaries, key=lambda x: x.order):
            # 有文档标题时标题层级整体下移一级
            level = s.level + (1 if doc_title else 0)
            heading = "#" * min(level, 4)

            lines += [f"{heading} {s.title}", ""]
            lines += [s.summary, ""]

            if s.key_points:
                lines += ["**核心要点：**", ""]
                for point in s.key_points:
                    lines.append(f"- {point}")
                lines.append("")

            lines += ["---", ""]

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 第四步：飞书导入
# ─────────────────────────────────────────────────────────────────────────────

class FeishuImporter:
    """
    使用飞书 Drive Import API 把 Markdown 文件整体导入为云文档。

    为什么用 Import API 而不是 Block API：
      - Import API 把文件作为整体解析，顺序由文件字节流保证
      - Block API 的 index 参数容易出错，且受限流影响顺序不稳定

    飞书文件夹 token 获取方式：
      打开飞书云空间目标文件夹 → 复制 URL 中 folder/ 后面的字符串
      例：https://bytedance.feishu.cn/drive/folder/AbCdEfGh → AbCdEfGh
    """

    _BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, config: PdfToFeishuConfig):
        self.config = config
        self._token: str | None = None
        self._token_expire: float = 0.0

    # ── 鉴权 ─────────────────────────────────────────────────────────────────

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token

        resp = await client.post(
            f"{self._BASE}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.config.feishu_app_id,
                "app_secret": self.config.feishu_app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书鉴权失败: {data}")

        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200)
        logger.info("飞书 Access Token 已获取，有效期 %d 秒", data.get("expire", 7200))
        return self._token  # type: ignore[return-value]

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # ── 上传文件 ──────────────────────────────────────────────────────────────

    @async_retry(max_attempts=3, retryable_exceptions=(httpx.HTTPError,))
    async def _upload_file(
        self,
        client: httpx.AsyncClient,
        token: str,
        filename: str,
        content: bytes,
    ) -> str:
        """上传文件到飞书云空间，返回 file_token。"""
        resp = await client.post(
            f"{self._BASE}/drive/v1/files/upload_all",
            headers=self._auth_headers(token),
            data={
                "file_name": filename,
                "parent_type": "explorer",
                "parent_node": self.config.feishu_folder_token,
                "size": str(len(content)),
            },
            files={"file": (filename, io.BytesIO(content), "text/markdown")},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"文件上传失败: {data}")

        file_token = data["data"]["file_token"]
        logger.info("文件已上传: file_token=%s", file_token)
        return file_token

    # ── 创建导入任务 ──────────────────────────────────────────────────────────

    @async_retry(max_attempts=3, retryable_exceptions=(httpx.HTTPError,))
    async def _create_import_task(
        self,
        client: httpx.AsyncClient,
        token: str,
        file_token: str,
        filename: str,
    ) -> str:
        """创建导入任务，返回 ticket。"""
        resp = await client.post(
            f"{self._BASE}/drive/v1/import_tasks",
            headers={**self._auth_headers(token), "Content-Type": "application/json"},
            json={
                "file_extension": "md",
                "file_token": file_token,
                "type": "docx",
                "file_name": filename,
                "point": {
                    "mount_type": 1,
                    "mount_key": self.config.feishu_folder_token,
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"创建导入任务失败: {data}")

        ticket = data["data"]["ticket"]
        logger.info("导入任务已创建: ticket=%s", ticket)
        return ticket

    # ── 轮询导入结果 ──────────────────────────────────────────────────────────

    async def _wait_for_import(
        self,
        client: httpx.AsyncClient,
        token: str,
        ticket: str,
    ) -> str:
        """轮询直到导入完成，返回飞书文档 token。"""
        deadline = time.time() + self.config.import_timeout_sec
        poll_interval = 2

        while time.time() < deadline:
            await asyncio.sleep(poll_interval)

            resp = await client.get(
                f"{self._BASE}/drive/v1/import_tasks/{ticket}",
                headers=self._auth_headers(token),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                raise RuntimeError(f"查询导入任务失败: {data}")

            result = data["data"]["result"]
            status = result.get("job_status")

            if status == 0:  # 成功
                doc_token = result["token"]
                logger.info("导入成功: doc_token=%s", doc_token)
                return doc_token
            if status in (2, 3):  # 失败
                raise RuntimeError(f"导入任务失败: {result.get('job_error_msg')}")

            logger.debug("导入进行中 (status=%s)，等待 %ds ...", status, poll_interval)
            poll_interval = min(poll_interval * 1.5, 10)  # 渐增轮询间隔

        raise TimeoutError(f"导入任务超时（>{self.config.import_timeout_sec}s）")

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def import_markdown(self, md_text: str, filename: str) -> str:
        """
        把 Markdown 文本上传并导入为飞书云文档。

        Args:
            md_text:  完整的 Markdown 字符串
            filename: 文档名称（不含 .md 后缀）

        Returns:
            飞书云文档的完整 URL
        """
        md_bytes = md_text.encode("utf-8")
        feishu_filename = f"{filename}.md"

        async with httpx.AsyncClient() as client:
            token = await self._get_access_token(client)
            file_token = await self._upload_file(client, token, feishu_filename, md_bytes)
            ticket = await self._create_import_task(client, token, file_token, filename)
            doc_token = await self._wait_for_import(client, token, ticket)

        return f"https://bytedance.feishu.cn/docx/{doc_token}"


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
    PDF → 飞书云文档 主流程。

    Args:
        pdf_path:             PDF 文件路径
        doc_title:            飞书文档标题
        config:               配置对象（为 None 时从环境变量自动读取）
        mineru_content_list:  MinerU 输出的 content_list.json 路径
                              （可选；适合双栏/复杂排版 PDF，精度更高）
        save_md_to:           同时把生成的 Markdown 保存到本地（可选，便于调试）

    Returns:
        生成的飞书云文档 URL
    """
    if config is None:
        config = PdfToFeishuConfig()

    logger.info("=" * 60)
    logger.info("开始处理：%s", pdf_path)
    logger.info("目标文档标题：%s", doc_title)

    # ── 1. 解析 PDF ──────────────────────────────────────────────────────────
    if mineru_content_list:
        logger.info("使用 MinerU 解析结果：%s", mineru_content_list)
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

    logger.info("共识别 %d 个章节：", len(sections))
    for s in sections:
        indent = "  " * (s.level - 1)
        logger.info("  [%02d] %s%s（%d 字）", s.order, indent, s.title, len(s.content))

    # ── 2. 逐节总结 ──────────────────────────────────────────────────────────
    logger.info("开始 LLM 总结（严格串行，共 %d 节）...", len(sections))
    summarizer = Summarizer(config)
    summaries = await summarizer.summarize_all(sections)
    logger.info("LLM 总结完成，共 %d 节", len(summaries))

    # ── 3. 生成 Markdown ──────────────────────────────────────────────────────
    md_text = MarkdownBuilder.build(summaries, doc_title=doc_title)
    logger.info("Markdown 已生成：%d 字符", len(md_text))

    if save_md_to:
        Path(save_md_to).write_text(md_text, encoding="utf-8")
        logger.info("Markdown 已保存到本地：%s", save_md_to)

    # ── 4. 导入飞书 ──────────────────────────────────────────────────────────
    logger.info("开始导入飞书...")
    importer = FeishuImporter(config)
    url = await importer.import_markdown(md_text, doc_title)

    logger.info("=" * 60)
    logger.info("完成！飞书文档：%s", url)
    return url
