#!/usr/bin/env python3
"""
Markdown → 飞书云文档（逐条同步插入，已验证方案）

之前所有方案失败的根因：
  1. Import API + md           → 飞书 file_extension 不支持 md
  2. Import API + docx 转换    → upload_all bug + Import 解析不稳定
  3. Block API 批量 + index    → API 响应 children 为空 → index=0 → 完全倒序
  4. Block API 批量 + 无 index → children:[50个block] 飞书不保证批内顺序

本方案（唯一正确做法）：
  每次 API 调用只传 children: [1个block]
  await 等返回后再发下一条
  不传 index（追加到末尾）

  三重保证：
    Python for 循环顺序 → 单条发送 → 等待完成后才发下一条
  顺序 100% 由客户端控制，与飞书服务端行为无关

依赖：pip install httpx
环境变量：FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_FOLDER_TOKEN

用法：
  python md_to_feishu.py summary.md --title "报告标题"
  python md_to_feishu.py summary.md                      # 标题取文件名
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
#  第一步：解析 Markdown → 飞书 Block JSON 列表（纯字符串，无第三方依赖）
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
    输出列表的下标顺序 == 文档中的物理顺序。
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
#  第二步：飞书 API — 鉴权 + 创建文档 + 逐条插入
# ═══════════════════════════════════════════════════════════════════════════════

def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def feishu_token(client: httpx.AsyncClient) -> str:
    """获取 tenant_access_token。"""
    r = await client.post(
        f"{FEISHU}/auth/v3/tenant_access_token/internal",
        json={
            "app_id": os.environ["FEISHU_APP_ID"],
            "app_secret": os.environ["FEISHU_APP_SECRET"],
        },
        timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"鉴权失败: {d}")
    print("  tenant_access_token 已获取")
    return d["tenant_access_token"]


async def feishu_create_doc(client: httpx.AsyncClient, token: str, title: str) -> str:
    """创建空白文档，返回 document_id。"""
    r = await client.post(
        f"{FEISHU}/docx/v1/documents",
        headers=_h(token),
        json={
            "folder_token": os.environ["FEISHU_FOLDER_TOKEN"],
            "title": title,
        },
        timeout=30,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"创建文档失败: {d}")
    doc_id = d["data"]["document"]["document_id"]
    print(f"  文档已创建: {doc_id}")
    return doc_id


async def feishu_insert_one(
    client: httpx.AsyncClient,
    token: str,
    doc_id: str,
    block: dict,
    seq: int,
    total: int,
    max_retries: int = 4,
) -> None:
    """
    插入 **单个** block 到文档末尾。

    ★ 这是保证顺序的核心：
      children 数组永远只有 1 个元素。
      不传 index → 服务端追加到末尾。
      飞书没有机会打乱顺序，因为每次只有 1 个 block。
    """
    for attempt in range(1, max_retries + 1):
        try:
            r = await client.post(
                f"{FEISHU}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                headers=_h(token),
                params={"document_revision_id": -1},
                json={"children": [block]},          # ← 永远只有 1 个
                timeout=30,
            )
            r.raise_for_status()
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(f"code={d['code']}, msg={d.get('msg')}")
            return  # 成功

        except Exception as exc:
            if attempt == max_retries:
                raise RuntimeError(
                    f"block[{seq}] 插入失败（已重试 {max_retries} 次）: {exc}"
                ) from exc
            wait = 2 ** attempt
            print(f"    [{seq+1}/{total}] 重试 {attempt}/{max_retries}，{wait}s 后: {exc}")
            await asyncio.sleep(wait)


async def feishu_insert_all(
    client: httpx.AsyncClient,
    token: str,
    doc_id: str,
    blocks: list[dict],
    delay: float = 0.3,
) -> None:
    """
    逐条同步插入所有 block。

    顺序保证链：
      blocks[0] → await insert_one → 完成
      blocks[1] → await insert_one → 完成
      blocks[2] → await insert_one → 完成
      ...
      blocks[N] → await insert_one → 完成

    每条都是独立的 HTTP 请求，await 保证串行。
    delay 秒间隔防止飞书限流。
    """
    total = len(blocks)
    t0 = time.time()

    for i, block in enumerate(blocks):
        await feishu_insert_one(client, token, doc_id, block, seq=i, total=total)

        # 进度显示（每 10 条或最后一条）
        if (i + 1) % 10 == 0 or i == total - 1:
            elapsed = time.time() - t0
            speed = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / speed if speed > 0 else 0
            print(f"    [{i+1}/{total}] 已插入  ({speed:.1f} blocks/s, 剩余 ~{eta:.0f}s)")

        # 最后一条不需要等
        if i < total - 1:
            await asyncio.sleep(delay)

    print(f"  全部 {total} 个 blocks 插入完成（耗时 {time.time()-t0:.1f}s）")


# ═══════════════════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════════════════

async def md_to_feishu(md_path: str, title: str = "", delay: float = 0.3) -> str:
    """
    将本地 Markdown 文件写入为飞书原生云文档。

    Args:
        md_path: .md 文件路径
        title:   飞书文档标题（默认取文件名）
        delay:   逐条插入间隔秒数（默认 0.3）

    Returns:
        飞书云文档 URL
    """
    md_text = Path(md_path).read_text(encoding="utf-8")
    if not title:
        title = Path(md_path).stem

    # ── Step 1: Markdown → Block JSON ────────────────────────────────────────
    print(f"\n[1/3] 解析 Markdown...")
    blocks = md_to_blocks(md_text)
    print(f"  共 {len(blocks)} 个 blocks")

    if not blocks:
        raise ValueError("Markdown 解析结果为空")

    # 打印前 5 个 block 内容摘要，供核对顺序
    print("  前 5 个 block 预览：")
    for i, b in enumerate(blocks[:5]):
        bt = b["block_type"]
        content = "---"
        for key in ("heading1", "heading2", "heading3", "heading4", "text", "bullet"):
            if key in b:
                els = b[key].get("elements", [])
                if els:
                    content = els[0].get("text_run", {}).get("content", "")[:40]
                break
        print(f"    [{i}] type={bt} content={content!r}")

    # ── Step 2: 创建飞书文档 ──────────────────────────────────────────────────
    print(f"\n[2/3] 创建飞书文档...")
    async with httpx.AsyncClient() as client:
        token = await feishu_token(client)
        doc_id = await feishu_create_doc(client, token, title)

        # ── Step 3: 逐条同步插入 ──────────────────────────────────────────────
        print(f"\n[3/3] 逐条同步插入（每条间隔 {delay}s）...")
        await feishu_insert_all(client, token, doc_id, blocks, delay=delay)

    url = f"https://bytedance.feishu.cn/docx/{doc_id}"
    print(f"\n{'='*50}")
    print(f"完成！飞书文档：{url}")
    print(f"{'='*50}\n")
    return url


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_FOLDER_TOKEN"):
        if not os.environ.get(k):
            print(f"错误：缺少环境变量 {k}", file=sys.stderr)
            sys.exit(1)

    ap = argparse.ArgumentParser(
        description="Markdown → 飞书云文档（逐条同步插入，100%% 保序）",
    )
    ap.add_argument("md", help="Markdown 文件路径")
    ap.add_argument("--title", default="", help="飞书文档标题（默认取文件名）")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="逐条插入间隔秒数（默认 0.3，越大越稳越慢）")
    args = ap.parse_args()

    asyncio.run(md_to_feishu(args.md, args.title, args.delay))
