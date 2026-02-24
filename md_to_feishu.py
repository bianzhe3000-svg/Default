#!/usr/bin/env python3
"""
Markdown → 飞书云文档（版本号链式插入，彻底解决长文档乱序）

之前所有方案失败的根因：
  1. Import API + md           → 飞书 file_extension 不支持 md
  2. Import API + docx 转换    → upload_all bug + Import 解析不稳定
  3. Block API 批量 + index    → API 响应 children 为空 → index=0 → 完全倒序
  4. Block API 批量 + 无 index → children:[50个block] 飞书不保证批内顺序
  5. Block API 单条 + rev=-1   → 短文档OK，长文档密集请求版本号冲突 → 乱序

本方案（终极修复）：
  每次 API 调用只传 children: [1个block]
  ★ 传入上一次返回的 revision_id（不再用 -1）
  ★ 形成 rev1 → rev2 → rev3 → ... 的严格链式写入
  ★ 超时时查询版本号判断是否已插入，杜绝重复
  await 等返回后再发下一条

  四重保证：
    Python for 循环顺序 → 单条发送 → 版本号链式传递 → 超时验证防重复
  长文档也 100% 保序

依赖：pip install httpx
环境变量：FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_FOLDER_TOKEN

用法：
  python md_to_feishu.py summary.md --title "报告标题"
  python md_to_feishu.py summary.md                      # 标题取文件名
  python md_to_feishu.py big_report.md --delay 1.0       # 长文档用更大延迟
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
#  第二步：飞书 API — 鉴权 + 创建文档 + 版本号链式逐条插入
#
#  ★★★ 长文档乱序彻底修复 ★★★
#
#  旧方案：document_revision_id=-1（取最新版本号）
#    短文档：请求稀疏，版本号推进来得及，问题不明显
#    长文档：请求密集，多个请求解析到同一个"最新"版本号 → 冲突 → 乱序
#            超时后重试可能导致重复插入 → 乱序
#
#  新方案：
#    创建文档 → rev_0
#    插入 block_0（rev=rev_0） → 返回 rev_1
#    插入 block_1（rev=rev_1） → 返回 rev_2
#    ...
#    版本号链式传递，飞书不可能乱序，因为版本号不对会被拒绝
#    超时时查询当前版本号判断是否已插入，杜绝重复
# ═══════════════════════════════════════════════════════════════════════════════

def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class _TokenManager:
    """Token 管理器，自动续期。"""

    def __init__(self):
        self._token: str = ""
        self._expire: float = 0.0

    async def get(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._expire - 120:
            return self._token

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
        self._token = d["tenant_access_token"]
        self._expire = time.time() + d.get("expire", 7200)
        print(f"  tenant_access_token 已获取（有效期 {d.get('expire', 7200)}s）")
        return self._token

    def force_refresh(self):
        self._expire = 0.0


_tm = _TokenManager()


async def feishu_create_doc(client: httpx.AsyncClient, title: str) -> tuple[str, int]:
    """创建空白文档，返回 (document_id, revision_id)。"""
    token = await _tm.get(client)
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
    doc = d["data"]["document"]
    doc_id = doc["document_id"]
    rev_id = doc.get("revision_id", 1)
    print(f"  文档已创建: {doc_id} (revision={rev_id})")
    return doc_id, rev_id


async def feishu_get_revision(client: httpx.AsyncClient, doc_id: str) -> int:
    """查询文档当前版本号。用于超时后验证 block 是否已插入。"""
    token = await _tm.get(client)
    r = await client.get(
        f"{FEISHU}/docx/v1/documents/{doc_id}",
        headers=_h(token),
        timeout=30,
    )
    d = r.json()
    if d.get("code") == 0:
        return d["data"]["document"]["revision_id"]
    raise RuntimeError(f"获取版本号失败: {d}")


async def feishu_insert_one(
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

    ★ 版本号链式传递 — 长文档乱序的终极修复 ★

    关键机制：
      1. 传入上一次操作的 revision_id（不是 -1）
      2. 成功后从响应提取新 revision_id，返回给调用者
      3. 超时/网络错误时：查询当前版本号
         - 版本号已推进 → block 已插入，继续
         - 版本号未变 → block 未插入，安全重试
      4. 版本冲突时：查询最新版本号，判断是否已插入
      5. 限流时：指数退避等待后重试
    """
    for attempt in range(1, max_retries + 1):
        resp_data = None
        try:
            token = await _tm.get(client)
            r = await client.post(
                f"{FEISHU}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                headers=_h(token),
                params={"document_revision_id": revision_id},  # ★ 真实版本号
                json={"children": [block]},  # ← 永远只有 1 个
                timeout=60,  # 比旧方案的 30s 更长，减少超时
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
                curr_rev = await feishu_get_revision(client, doc_id)
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
                curr_rev = await feishu_get_revision(client, doc_id)
                if curr_rev > revision_id:
                    print(f"    [{seq+1}/{total}] 版本冲突但 block 已插入"
                          f"（rev: {revision_id}→{curr_rev}）")
                    return curr_rev
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


async def feishu_insert_all(
    client: httpx.AsyncClient,
    doc_id: str,
    blocks: list[dict],
    initial_revision: int,
    delay: float = 0.5,
) -> None:
    """
    版本号链式逐条插入所有 block。

    顺序保证链（四重保证）：
      1. Python for 循环（串行）
      2. await 等待每条 HTTP 完成
      3. 从响应提取 revision_id 传给下一条（链式版本号）
      4. 超时时验证再决定是否重试（防重复）

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

    TOKEN_REFRESH_INTERVAL = 100

    for i, block in enumerate(blocks):
        # 定期刷新 token（防止长文档处理中 token 过期）
        if i > 0 and i % TOKEN_REFRESH_INTERVAL == 0:
            print(f"    [{i}/{total}] 刷新 token...")
            _tm.force_refresh()
            await _tm.get(client)

        # ★ 核心：版本号链式传递
        revision = await feishu_insert_one(
            client, doc_id, block, revision, seq=i, total=total
        )

        # 进度显示（每 20 条或最后一条）
        if (i + 1) % 20 == 0 or i == total - 1:
            elapsed = time.time() - t0
            speed = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / speed if speed > 0 else 0
            print(f"    [{i+1}/{total}] rev={revision}"
                  f"  ({speed:.1f} blocks/s, 剩余 ~{eta:.0f}s)")

        # 最后一条不需要等
        if i < total - 1:
            await asyncio.sleep(delay)

    print(f"  全部 {total} 个 blocks 插入完成"
          f"（耗时 {time.time()-t0:.1f}s，最终 rev={revision}）")


# ═══════════════════════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════════════════════

async def md_to_feishu(md_path: str, title: str = "", delay: float = 0.5) -> str:
    """
    将本地 Markdown 文件写入为飞书原生云文档。

    Args:
        md_path: .md 文件路径
        title:   飞书文档标题（默认取文件名）
        delay:   逐条插入间隔秒数（默认 0.5）

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
        doc_id, rev = await feishu_create_doc(client, title)

        # ── Step 3: 版本号链式逐条插入 ──────────────────────────────────────
        print(f"\n[3/3] 版本号链式逐条插入（初始 rev={rev}，延迟 {delay}s）...")
        await feishu_insert_all(client, doc_id, blocks, rev, delay=delay)

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
        description="Markdown → 飞书云文档（版本号链式插入，长文档也保序）",
    )
    ap.add_argument("md", help="Markdown 文件路径")
    ap.add_argument("--title", default="", help="飞书文档标题（默认取文件名）")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="逐条插入间隔秒数（默认 0.5，长文档自动调大）")
    args = ap.parse_args()

    asyncio.run(md_to_feishu(args.md, args.title, args.delay))
