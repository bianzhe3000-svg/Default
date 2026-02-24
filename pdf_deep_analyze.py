#!/usr/bin/env python3
"""
PDF 深度解读 → 飞书云文档（彻底解决长文档乱序问题）

长文档乱序的根本原因：
  旧方案使用 document_revision_id=-1（取最新版本号），
  短文档 block 少请求稀疏，问题不明显。
  长文档 block 多请求密集，多个请求可能解析到同一个"最新"版本号，
  超时重试可能导致重复插入，限流重试同理。

彻底修复 — 版本号链式传递：
  ★ 每次插入从响应中获取新的 document_revision_id
  ★ 下一次插入使用上一次返回的真实版本号（不再用 -1）
  ★ 链式 rev1 → rev2 → rev3 → ... 严格保证写入顺序
  ★ 超时时查询当前版本号判断是否已插入，杜绝重复
  ★ 自适应延迟 + 限流退避 + Token 自动续期

用法：
  # 已有 Markdown → 飞书（修复长文档乱序）
  python pdf_deep_analyze.py --md summary.md --title "报告标题"

  # 完整流程：PDF → GPT-4o 深度解读 → 飞书
  python pdf_deep_analyze.py --pdf report.pdf --title "深度解读"

  # 仅生成 Markdown，不上传飞书
  python pdf_deep_analyze.py --pdf report.pdf --save-md output.md --no-feishu

  # 自定义延迟（越大越稳，越慢）
  python pdf_deep_analyze.py --md summary.md --delay 0.8

依赖：pip install httpx PyMuPDF openai
环境变量：
  FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_FOLDER_TOKEN  （飞书必须）
  OPENAI_API_KEY                                          （PDF解读必须）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

import httpx

FEISHU = "https://open.feishu.cn/open-apis"


# ═══════════════════════════════════════════════════════════════════════════════
#  飞书 Block 类型常量
# ═══════════════════════════════════════════════════════════════════════════════

BT_TEXT    = 2
BT_H1     = 3
BT_H2     = 4
BT_H3     = 5
BT_H4     = 6
BT_BULLET = 12
BT_DIVIDER = 22


# ═══════════════════════════════════════════════════════════════════════════════
#  第一步：解析 Markdown → 飞书 Block JSON 列表
# ═══════════════════════════════════════════════════════════════════════════════

_INLINE_RE = re.compile(r"(\*\*(.+?)\*\*|\*(.+?)\*)")


def _run(text: str, bold: bool = False, italic: bool = False) -> dict:
    el: dict = {"text_run": {"content": text}}
    style = {}
    if bold:
        style["bold"] = True
    if italic:
        style["italic"] = True
    if style:
        el["text_run"]["text_element_style"] = style
    return el


def _parse_inline(text: str) -> list[dict]:
    """解析 **bold** 和 *italic*，返回 elements 数组。"""
    elements: list[dict] = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            elements.append(_run(text[last:m.start()]))
        if m.group(0).startswith("**"):
            elements.append(_run(m.group(2), bold=True))
        else:
            elements.append(_run(m.group(3), italic=True))
        last = m.end()
    if last < len(text):
        elements.append(_run(text[last:]))
    return elements or [_run(text)]


def md_to_blocks(md_text: str) -> list[dict]:
    """
    逐行解析 Markdown → 飞书 Block JSON 列表。
    支持：# 标题(1-4)  **粗体**  *斜体*  - 列表  --- 分割线  > 引用  段落
    """
    blocks: list[dict] = []

    for raw in md_text.splitlines():
        line = raw.rstrip()

        if line.startswith("#### "):
            blocks.append({
                "block_type": BT_H4,
                "heading4": {"elements": [_run(line[5:])]},
            })
        elif line.startswith("### "):
            blocks.append({
                "block_type": BT_H3,
                "heading3": {"elements": [_run(line[4:])]},
            })
        elif line.startswith("## "):
            blocks.append({
                "block_type": BT_H2,
                "heading2": {"elements": [_run(line[3:])]},
            })
        elif line.startswith("# "):
            blocks.append({
                "block_type": BT_H1,
                "heading1": {"elements": [_run(line[2:])]},
            })
        elif re.match(r"^[-*+] ", line):
            blocks.append({
                "block_type": BT_BULLET,
                "bullet": {"elements": _parse_inline(line[2:])},
            })
        elif line.strip() in ("---", "***", "___"):
            blocks.append({
                "block_type": BT_DIVIDER,
                "divider": {},
            })
        elif line.startswith("> "):
            blocks.append({
                "block_type": BT_TEXT,
                "text": {"elements": [_run(line[2:], italic=True)]},
            })
        elif line.strip():
            blocks.append({
                "block_type": BT_TEXT,
                "text": {"elements": _parse_inline(line)},
            })
        # 空行跳过

    return blocks


# ═══════════════════════════════════════════════════════════════════════════════
#  第二步（可选）：PDF 提取 + GPT-4o 深度解读
# ═══════════════════════════════════════════════════════════════════════════════

ANALYSIS_SYSTEM_PROMPT = """你是一位资深文档分析专家。你的任务是对用户提供的文档进行深度解读和分析。

关键要求：
1. 你必须用自己的语言进行分析和总结，绝对不能复制粘贴原文
2. 你需要提供真正的洞察和见解，而非简单复述
3. 识别文档中隐含的信息、未明说的假设、潜在风险
4. 分析要有深度，每个要点都要有你的专业判断
5. 输出格式严格使用 Markdown

输出必须严格按照以下模板结构（使用 Markdown 格式）：

# 文档概览

- **核心主题**：[一句话概括文档的核心议题]
- **写作目的**：[作者写这份文档想达到什么目的]
- **目标读者**：[这份文档是写给谁看的]
- **一句话结论**：[如果只能说一句话，这份文档最重要的结论是什么]

---

# 核心内容逐章解读

## [第一章/节标题]

**本节核心**：[一句话概括]

[100-200字深度分析，用你自己的语言解读，指出关键逻辑、隐含假设、优势与不足]

- [要点1]
- [要点2]
- [要点3]

## [第二章/节标题]

**本节核心**：[一句话概括]

[100-200字深度分析]

- [要点1]
- [要点2]
- [要点3]

（以此类推，覆盖所有主要章节）

---

# 关键数据与事实

- [列出文档中所有关键数字、百分比、日期、金额等具体数据]
- [每条数据说明其含义和重要性]

---

# 核心洞察

1. **[洞察1标题]**：[深入分析，不是复述原文，而是你从原文推导出的见解。引用原文证据支持你的判断]

2. **[洞察2标题]**：[同上]

3. **[洞察3标题]**：[同上]

（提供3-5个真正有深度的洞察）

---

# 结论与建议

## 文档核心结论

[忠实总结文档的核心结论，2-3句话]

## 行动建议

- **建议1**：[具体可执行的建议]
- **建议2**：[具体可执行的建议]
- **建议3**：[具体可执行的建议]

---

# 延伸问题

1. [基于文档内容，提出一个值得深入探讨的问题]
2. [第二个延伸问题]
3. [第三个延伸问题]"""


def extract_pdf_text(pdf_path: str) -> tuple[str, list[str]]:
    """用 PyMuPDF 提取 PDF 全文和目录。"""
    try:
        import fitz
    except ImportError:
        print("错误：请先安装 PyMuPDF：pip install PyMuPDF", file=sys.stderr)
        sys.exit(1)

    doc = fitz.open(pdf_path)

    # 提取目录
    toc = doc.get_toc()
    toc_lines = [f"{'  ' * (level - 1)}{title} (p.{page})" for level, title, page in toc]

    # 提取全文
    full_text = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        if text.strip():
            full_text.append(f"--- 第 {page_num + 1} 页 ---\n{text}")

    doc.close()
    return "\n".join(full_text), toc_lines


def analyze_with_gpt4o(full_text: str, toc_lines: list[str], pdf_name: str) -> str:
    """调用 GPT-4o 进行深度解读，返回 Markdown 文本。"""
    try:
        from openai import OpenAI
    except ImportError:
        print("错误：请先安装 openai：pip install openai", file=sys.stderr)
        sys.exit(1)

    client = OpenAI()
    toc_str = "\n".join(toc_lines) if toc_lines else "（无目录信息）"

    # GPT-4o 上下文约 128k tokens，中文约 1.5 token/字，安全阈值 80k 字
    MAX_CHARS = 80000

    if len(full_text) <= MAX_CHARS:
        return _analyze_single_pass(client, full_text, toc_str, pdf_name)
    else:
        print(f"  文档较长（{len(full_text)} 字），采用分段分析 + 综合...")
        return _analyze_chunked(client, full_text, toc_str, pdf_name)


def _analyze_single_pass(client, full_text: str, toc_str: str, pdf_name: str) -> str:
    """单次全文分析（文档 < 80k 字）。"""
    user_msg = (
        f"请对以下文档进行深度解读。\n\n"
        f"文档名称：{pdf_name}\n\n"
        f"文档目录：\n{toc_str}\n\n"
        f"文档全文：\n{full_text}\n\n"
        f"请严格按照模板格式输出你的深度分析。"
        f"记住：用你自己的语言分析，不要复制粘贴原文。"
    )
    print("  调用 GPT-4o 全文分析（streaming）...")
    # 使用 streaming 避免长文档超时
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=16000,
        stream=True,
    )
    chunks = []
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            chunks.append(delta.content)
            # 每 50 个 chunk 打印一个点表示进度
            if len(chunks) % 50 == 0:
                print(".", end="", flush=True)
    print()  # 换行
    return "".join(chunks)


def _analyze_chunked(client, full_text: str, toc_str: str, pdf_name: str) -> str:
    """分段分析 + 综合（文档 > 80k 字）。"""
    CHUNK_SIZE = 40000
    chunks = []
    current: list[str] = []
    current_len = 0

    for line in full_text.split("\n"):
        current.append(line)
        current_len += len(line) + 1
        if current_len >= CHUNK_SIZE:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
    if current:
        chunks.append("\n".join(current))

    print(f"  拆分为 {len(chunks)} 个片段进行分析...")

    # 逐段分析
    chunk_analyses = []
    for i, chunk_text in enumerate(chunks):
        print(f"  分析片段 {i + 1}/{len(chunks)}...")
        stream = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": (
                    "你是资深文档分析专家。请对文档片段进行深入分析，"
                    "提取核心内容、关键数据、重要洞察。"
                    "用你自己的语言总结，不要复制粘贴原文。"
                )},
                {"role": "user", "content": (
                    f"文档名称：{pdf_name}\n\n"
                    f"这是文档的第 {i + 1}/{len(chunks)} 部分：\n\n{chunk_text}\n\n"
                    f"请分析这部分的核心内容、关键数据和重要发现。"
                )},
            ],
            temperature=0.3,
            max_tokens=4000,
            stream=True,
        )
        parts = []
        for c in stream:
            if c.choices[0].delta.content:
                parts.append(c.choices[0].delta.content)
        chunk_analyses.append("".join(parts))

    # 综合所有片段
    print("  综合各片段，生成最终报告...")
    all_analyses = "\n\n---\n\n".join(
        [f"## 片段 {i + 1} 分析\n{a}" for i, a in enumerate(chunk_analyses)]
    )
    synthesis_msg = (
        f"文档名称：{pdf_name}\n\n"
        f"文档目录：\n{toc_str}\n\n"
        f"以下是对文档各部分的分析结果：\n\n{all_analyses}\n\n"
        f"请基于以上所有片段的分析结果，按照模板要求生成一份完整的深度解读报告。\n"
        f"要求：\n"
        f"1. 整合所有片段的分析，形成连贯的报告\n"
        f"2. 严格按照模板格式（文档概览→核心内容逐章解读→关键数据与事实→核心洞察→结论与建议→延伸问题）\n"
        f"3. 用你自己的语言，不要复制粘贴\n"
        f"4. 提供真正有深度的洞察"
    )
    stream = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": synthesis_msg},
        ],
        temperature=0.3,
        max_tokens=16000,
        stream=True,
    )
    parts = []
    for c in stream:
        if c.choices[0].delta.content:
            parts.append(c.choices[0].delta.content)
            if len(parts) % 50 == 0:
                print(".", end="", flush=True)
    print()
    return "".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
#  第三步：飞书 API — 鉴权 + 创建文档 + 版本链式逐条插入
#
#  ★★★ 这是彻底解决长文档乱序的核心 ★★★
#
#  旧方案用 document_revision_id=-1（最新版本），长文档密集请求时
#  多个请求可能解析到同一个版本号 → 冲突 → 乱序。
#  超时重试可能导致重复插入 → 乱序。
#
#  新方案：
#    创建文档 → 获得 rev_0
#    插入 block_0（rev=rev_0） → 响应返回 rev_1
#    插入 block_1（rev=rev_1） → 响应返回 rev_2
#    插入 block_2（rev=rev_2） → 响应返回 rev_3
#    ...
#
#  ★ 版本号严格递增，每次写入依赖上一次的版本号
#  ★ 飞书不可能乱序处理，因为版本号不对的请求会被拒绝
#  ★ 超时时查询当前版本号判断 block 是否已插入，杜绝重复
# ═══════════════════════════════════════════════════════════════════════════════


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class FeishuClient:
    """飞书 API 客户端，带 Token 自动续期。"""

    def __init__(self):
        self.app_id = os.environ["FEISHU_APP_ID"]
        self.app_secret = os.environ["FEISHU_APP_SECRET"]
        self.folder_token = os.environ["FEISHU_FOLDER_TOKEN"]
        self._token: str = ""
        self._token_expire: float = 0.0

    async def get_token(self, client: httpx.AsyncClient) -> str:
        """获取 tenant_access_token，过期前自动续期。"""
        if self._token and time.time() < self._token_expire - 120:
            return self._token

        r = await client.post(
            f"{FEISHU}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"鉴权失败: {d}")
        self._token = d["tenant_access_token"]
        self._token_expire = time.time() + d.get("expire", 7200)
        print(f"  tenant_access_token 已获取（有效期 {d.get('expire', 7200)}s）")
        return self._token

    async def create_doc(self, client: httpx.AsyncClient, title: str) -> tuple[str, int]:
        """创建空白文档，返回 (document_id, revision_id)。"""
        token = await self.get_token(client)
        r = await client.post(
            f"{FEISHU}/docx/v1/documents",
            headers=_h(token),
            json={"folder_token": self.folder_token, "title": title},
            timeout=30,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"创建文档失败: {d}")
        doc = d["data"]["document"]
        doc_id = doc["document_id"]
        rev_id = doc.get("revision_id", 1)
        print(f"  文档已创建: {doc_id} (revision={rev_id})")
        return doc_id, rev_id

    async def get_revision(self, client: httpx.AsyncClient, doc_id: str) -> int:
        """查询文档当前版本号。用于超时后验证 block 是否已插入。"""
        token = await self.get_token(client)
        r = await client.get(
            f"{FEISHU}/docx/v1/documents/{doc_id}",
            headers=_h(token),
            timeout=30,
        )
        d = r.json()
        if d.get("code") == 0:
            return d["data"]["document"]["revision_id"]
        raise RuntimeError(f"获取版本号失败: {d}")

    async def insert_one(
        self,
        client: httpx.AsyncClient,
        doc_id: str,
        block: dict,
        revision_id: int,
        seq: int,
        total: int,
        max_retries: int = 6,
    ) -> int:
        """
        插入单个 block，返回新的 revision_id。

        ★★★ 长文档乱序彻底修复的核心函数 ★★★

        关键机制：
          1. 传入上一次操作的 revision_id（不是 -1）
          2. 成功后从响应提取新 revision_id，传给下一次调用
          3. 超时/网络错误时：查询当前版本号
             - 版本号已推进 → block 已插入，取新版本号继续
             - 版本号未变 → block 未插入，安全重试
          4. 限流时：指数退避等待后重试
          5. 版本冲突时：查询最新版本号，判断是否已插入
        """
        for attempt in range(1, max_retries + 1):
            resp_data = None
            try:
                token = await self.get_token(client)
                r = await client.post(
                    f"{FEISHU}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                    headers=_h(token),
                    params={"document_revision_id": revision_id},
                    json={"children": [block]},
                    timeout=60,
                )
                resp_data = r.json()

            except Exception as exc:
                # ── 网络/超时错误 ──────────────────────────────────────────
                if attempt == max_retries:
                    raise RuntimeError(
                        f"block[{seq}] 网络错误（已重试 {max_retries} 次）: {exc}"
                    )

                # 查询版本号判断 block 是否已插入
                try:
                    curr_rev = await self.get_revision(client, doc_id)
                    if curr_rev > revision_id:
                        print(f"    [{seq+1}/{total}] 请求超时但 block 已插入"
                              f"（rev: {revision_id}→{curr_rev}）")
                        return curr_rev
                except Exception:
                    pass  # 查询也失败了，直接重试

                wait = min(2 ** attempt, 32)
                print(f"    [{seq+1}/{total}] 网络错误，{wait}s 后重试"
                      f" ({attempt}/{max_retries}): {exc}")
                await asyncio.sleep(wait)
                continue

            # ── 解析响应 ──────────────────────────────────────────────────
            code = resp_data.get("code", -1)

            # 成功
            if code == 0:
                new_rev = resp_data.get("data", {}).get(
                    "document_revision_id", revision_id + 1
                )
                return new_rev

            # 限流
            if code == 99991400:
                wait = min(2 ** attempt * 3, 60)
                print(f"    [{seq+1}/{total}] 限流，等待 {wait}s...")
                await asyncio.sleep(wait)
                continue

            # 版本冲突（可能是之前超时的请求其实成功了）
            msg = str(resp_data.get("msg", "")).lower()
            if "revision" in msg or code in (1770005, 1770006, 1770010):
                try:
                    curr_rev = await self.get_revision(client, doc_id)
                    if curr_rev > revision_id:
                        print(f"    [{seq+1}/{total}] 版本冲突但 block 已插入"
                              f"（rev: {revision_id}→{curr_rev}）")
                        return curr_rev
                    # 版本号没变 → 用当前版本重试
                    revision_id = curr_rev
                except Exception:
                    pass

                wait = min(2 ** attempt, 16)
                print(f"    [{seq+1}/{total}] 版本冲突，{wait}s 后重试"
                      f" ({attempt}/{max_retries})")
                await asyncio.sleep(wait)
                continue

            # 其他不可恢复错误
            raise RuntimeError(
                f"block[{seq}] 插入失败: code={code}, msg={resp_data.get('msg')}"
            )

        raise RuntimeError(f"block[{seq}] 重试 {max_retries} 次后仍失败")

    async def insert_all(
        self,
        client: httpx.AsyncClient,
        doc_id: str,
        blocks: list[dict],
        initial_revision: int,
        delay: float = 0.5,
    ) -> None:
        """
        版本号链式逐条插入所有 block。

        顺序保证链（比旧方案多了版本号链）：
          Python for 循环（串行）
          → await 等待每条 HTTP 完成
          → 从响应提取 revision_id 传给下一条
          → 飞书用版本号校验写入顺序
          → 四重保证 = 100% 保序

        自适应延迟：
          < 100 blocks: 基础延迟
          100-300: 基础延迟 × 1.5
          300+: 基础延迟 × 2
        """
        total = len(blocks)
        t0 = time.time()
        revision = initial_revision

        # 自适应延迟
        if total > 300:
            delay = max(delay, 1.0)
            print(f"  长文档模式（{total} blocks），延迟调整为 {delay}s")
        elif total > 100:
            delay = max(delay, 0.6)
            print(f"  中等文档（{total} blocks），延迟调整为 {delay}s")

        # 每 N 个 block 刷新 token（防止长文档处理中 token 过期）
        TOKEN_REFRESH_INTERVAL = 100

        for i, block in enumerate(blocks):
            # 定期刷新 token
            if i > 0 and i % TOKEN_REFRESH_INTERVAL == 0:
                print(f"    [{i}/{total}] 刷新 token...")
                self._token_expire = 0  # 强制刷新
                await self.get_token(client)

            # ★ 核心：传入上一次的 revision_id
            revision = await self.insert_one(
                client, doc_id, block, revision, seq=i, total=total
            )

            # 进度显示
            if (i + 1) % 20 == 0 or i == total - 1:
                elapsed = time.time() - t0
                speed = (i + 1) / elapsed if elapsed > 0 else 0
                eta = (total - i - 1) / speed if speed > 0 else 0
                print(f"    [{i+1}/{total}] rev={revision}"
                      f"  ({speed:.1f} blocks/s, 剩余 ~{eta:.0f}s)")

            # 非最后一条时等待
            if i < total - 1:
                await asyncio.sleep(delay)

        print(f"  全部 {total} blocks 插入完成"
              f"（耗时 {time.time()-t0:.1f}s，最终 rev={revision}）")


# ═══════════════════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════════════════

async def run(
    md_text: str,
    title: str,
    delay: float = 0.5,
) -> str:
    """Markdown 文本 → 飞书云文档，返回文档 URL。"""

    # ── Step 1: Markdown → Block JSON ──────────────────────────────────────
    print(f"\n[1/3] 解析 Markdown...")
    blocks = md_to_blocks(md_text)
    print(f"  共 {len(blocks)} 个 blocks")

    if not blocks:
        raise ValueError("Markdown 解析结果为空")

    # 预览前 5 个
    print("  前 5 个 block 预览：")
    for i, b in enumerate(blocks[:5]):
        bt = b["block_type"]
        content = "---"
        for key in ("heading1", "heading2", "heading3", "heading4", "text", "bullet"):
            if key in b:
                els = b[key].get("elements", [])
                if els:
                    content = els[0].get("text_run", {}).get("content", "")[:50]
                break
        print(f"    [{i}] type={bt} content={content!r}")

    # ── Step 2: 创建飞书文档 ──────────────────────────────────────────────
    print(f"\n[2/3] 创建飞书文档...")
    feishu = FeishuClient()

    async with httpx.AsyncClient() as client:
        doc_id, rev = await feishu.create_doc(client, title)

        # ── Step 3: 版本号链式逐条插入 ──────────────────────────────────────
        print(f"\n[3/3] 版本号链式逐条插入（初始 rev={rev}，延迟 {delay}s）...")
        await feishu.insert_all(client, doc_id, blocks, rev, delay=delay)

    url = f"https://bytedance.feishu.cn/docx/{doc_id}"
    print(f"\n{'='*60}")
    print(f"完成！飞书文档：{url}")
    print(f"{'='*60}\n")
    return url


async def run_pdf_pipeline(
    pdf_path: str,
    title: str,
    save_md: str | None = None,
    no_feishu: bool = False,
    delay: float = 0.5,
) -> str | None:
    """PDF → GPT-4o 深度解读 → 飞书云文档，完整流程。"""

    # ── 1. 提取 PDF 文本 ──────────────────────────────────────────────────
    print(f"\n[1/4] 提取 PDF 文本...")
    full_text, toc_lines = extract_pdf_text(pdf_path)
    print(f"  全文 {len(full_text)} 字，目录 {len(toc_lines)} 条")

    if not full_text.strip():
        raise ValueError("PDF 文本提取为空，可能是扫描件，请用 OCR 工具预处理")

    # ── 2. GPT-4o 深度解读 ────────────────────────────────────────────────
    print(f"\n[2/4] GPT-4o 深度解读...")
    pdf_name = Path(pdf_path).stem
    md_text = analyze_with_gpt4o(full_text, toc_lines, pdf_name)
    print(f"  解读完成，{len(md_text)} 字")

    # ── 3. 保存 Markdown（可选）────────────────────────────────────────────
    if save_md:
        Path(save_md).write_text(md_text, encoding="utf-8")
        print(f"  Markdown 已保存：{save_md}")

    if no_feishu:
        print("\n已跳过飞书上传（--no-feishu）")
        return None

    # ── 4. 上传飞书 ──────────────────────────────────────────────────────
    print(f"\n[3/4] 解析 Markdown → 飞书 Blocks...")
    print(f"[4/4] 上传飞书...")
    return await run(md_text, title, delay=delay)


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="PDF 深度解读 → 飞书云文档（长文档乱序彻底修复版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 已有 Markdown → 飞书（修复长文档乱序）
  python pdf_deep_analyze.py --md summary.md --title "报告"

  # 完整流程：PDF → GPT-4o 解读 → 飞书
  python pdf_deep_analyze.py --pdf report.pdf --title "深度解读"

  # 仅生成 Markdown
  python pdf_deep_analyze.py --pdf report.pdf --save-md output.md --no-feishu

  # 长文档用更大延迟（更稳）
  python pdf_deep_analyze.py --md big_report.md --delay 1.0
        """,
    )

    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--md", help="输入 Markdown 文件路径")
    source.add_argument("--pdf", help="输入 PDF 文件路径")

    ap.add_argument("--title", default="", help="飞书文档标题（默认取文件名）")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="逐条插入间隔秒数（默认 0.5，长文档自动调大）")
    ap.add_argument("--save-md", metavar="PATH",
                    help="保存 Markdown 到本地（仅 --pdf 模式）")
    ap.add_argument("--no-feishu", action="store_true",
                    help="不上传飞书（仅 --pdf 模式，调试用）")

    args = ap.parse_args()

    # 环境变量检查
    if not args.no_feishu:
        for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_FOLDER_TOKEN"):
            if not os.environ.get(k):
                print(f"错误：缺少环境变量 {k}", file=sys.stderr)
                sys.exit(1)

    if args.pdf and not os.environ.get("OPENAI_API_KEY"):
        print("错误：--pdf 模式需要 OPENAI_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    title = args.title

    if args.md:
        # Markdown → 飞书
        md_path = args.md
        if not Path(md_path).exists():
            print(f"错误：文件不存在 {md_path}", file=sys.stderr)
            sys.exit(1)
        if not title:
            title = Path(md_path).stem
        md_text = Path(md_path).read_text(encoding="utf-8")
        asyncio.run(run(md_text, title, delay=args.delay))

    else:
        # PDF → GPT-4o → 飞书
        pdf_path = args.pdf
        if not Path(pdf_path).exists():
            print(f"错误：文件不存在 {pdf_path}", file=sys.stderr)
            sys.exit(1)
        if not title:
            title = Path(pdf_path).stem
        asyncio.run(run_pdf_pipeline(
            pdf_path, title,
            save_md=args.save_md,
            no_feishu=args.no_feishu,
            delay=args.delay,
        ))


if __name__ == "__main__":
    main()
