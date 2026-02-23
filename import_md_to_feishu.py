#!/usr/bin/env python3
"""
将本地 Markdown 文件导入为飞书云文档

为什么 MD 直接导入不可行：
  飞书 Import API (drive/v1/import_tasks) 的 file_extension 参数
  只支持：docx / xlsx / pptx / pdf / csv / xmind
  不支持 "md"，传 md 会直接报错或生成乱码文档。

为什么不用 upload_all：
  飞书 Node SDK 的 uploadAll 存在 bug，无法正确上传文件内容。
  即使用 httpx 直传，upload_all 在部分环境也不稳定。
  分片上传（upload_prepare → upload_part → upload_finish）更可靠。

本脚本的正确流程：
  MD 文件
    ↓ python-docx 解析（保留标题/粗体/列表/分割线）
  .docx 字节流
    ↓ 飞书分片上传（upload_prepare + upload_part + upload_finish）
  file_token
    ↓ 飞书 Import API（file_extension="docx"）
  ticket
    ↓ 轮询等待完成
  飞书云文档 URL ✓

依赖安装：
  pip install python-docx httpx

环境变量：
  FEISHU_APP_ID        飞书应用 App ID
  FEISHU_APP_SECRET    飞书应用 App Secret
  FEISHU_FOLDER_TOKEN  目标文件夹 token

用法：
  python import_md_to_feishu.py summary.md --title "季度报告总结"
  python import_md_to_feishu.py summary.md          # 标题自动取文件名
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import time
from pathlib import Path

import httpx
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

_FEISHU_BASE = "https://open.feishu.cn/open-apis"


# ─────────────────────────────────────────────────────────────────────────────
# 第一步：Markdown → python-docx（.docx 字节流）
# ─────────────────────────────────────────────────────────────────────────────

class MdToDocx:
    """
    把 Markdown 文本转换为 .docx 字节流。

    支持的语法：
      # / ## / ### / #### 标题
      **粗体**  *斜体*
      - 无序列表项
      --- 分割线
      > 引用块
      普通段落（支持行内粗/斜体）
    """

    _INLINE_RE = re.compile(r"(\*\*(.+?)\*\*|\*(.+?)\*)")

    def convert(self, md_text: str) -> bytes:
        doc = Document()
        self._init_styles(doc)

        for raw in md_text.splitlines():
            line = raw.rstrip()

            if line.startswith("#### "):
                doc.add_heading(line[5:], level=4)
            elif line.startswith("### "):
                doc.add_heading(line[4:], level=3)
            elif line.startswith("## "):
                doc.add_heading(line[3:], level=2)
            elif line.startswith("# "):
                doc.add_heading(line[2:], level=1)
            elif line.startswith("> "):
                p = doc.add_paragraph(style="Intense Quote")
                self._inline_runs(p, line[2:])
            elif re.match(r"^[-*+] ", line):
                p = doc.add_paragraph(style="List Bullet")
                self._inline_runs(p, line[2:])
            elif line.strip() in ("---", "***", "___"):
                self._add_hrule(doc)
            elif line.strip():
                p = doc.add_paragraph()
                self._inline_runs(p, line)
            # 空行跳过（段落间距由 Word 样式控制）

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    # ── 行内样式解析 ──────────────────────────────────────────────────────────

    def _inline_runs(self, paragraph, text: str) -> None:
        """把 **bold** / *italic* 切分后逐段添加 run。"""
        last = 0
        for m in self._INLINE_RE.finditer(text):
            if m.start() > last:
                paragraph.add_run(text[last:m.start()])
            if m.group(0).startswith("**"):
                r = paragraph.add_run(m.group(2))
                r.bold = True
            else:
                r = paragraph.add_run(m.group(3))
                r.italic = True
            last = m.end()
        if last < len(text):
            paragraph.add_run(text[last:])

    # ── 分割线 ────────────────────────────────────────────────────────────────

    def _add_hrule(self, doc: Document) -> None:
        """用段落底部边框模拟 --- 分割线。"""
        p = doc.add_paragraph()
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), "AAAAAA")
        pBdr.append(bottom)
        pPr.append(pBdr)

    # ── 字体初始化 ────────────────────────────────────────────────────────────

    def _init_styles(self, doc: Document) -> None:
        normal = doc.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(11)


# ─────────────────────────────────────────────────────────────────────────────
# 第二步：飞书鉴权
# ─────────────────────────────────────────────────────────────────────────────

async def _tenant_token(client: httpx.AsyncClient) -> str:
    """获取 tenant_access_token（App Token，无需用户授权）。"""
    resp = await client.post(
        f"{_FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={
            "app_id": os.environ["FEISHU_APP_ID"],
            "app_secret": os.environ["FEISHU_APP_SECRET"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败: code={data['code']}, msg={data.get('msg')}")
    return data["tenant_access_token"]


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# 第三步：分片上传（绕开 upload_all bug）
# ─────────────────────────────────────────────────────────────────────────────

async def _upload_chunked(
    client: httpx.AsyncClient,
    token: str,
    filename: str,
    content: bytes,
    folder_token: str,
) -> str:
    """
    分片上传文件到飞书云空间，返回 file_token。

    为什么不用 upload_all：
      upload_all 的 multipart 实现在部分 SDK/环境下有 bug，
      文件内容无法正确上传。分片接口更稳定，且支持任意大小文件。

    分片流程：
      1. upload_prepare → 获取 upload_id, block_size, block_num
      2. upload_part × block_num → 上传每一片
      3. upload_finish → 合并，获取 file_token
    """
    size = len(content)

    # ── Prepare ──────────────────────────────────────────────────────────────
    resp = await client.post(
        f"{_FEISHU_BASE}/drive/v1/files/upload_prepare",
        headers={**_hdr(token), "Content-Type": "application/json"},
        json={
            "file_name": filename,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": size,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"upload_prepare 失败: code={data['code']}, msg={data.get('msg')}")

    upload_id: str = data["data"]["upload_id"]
    block_size: int = data["data"]["block_size"]
    block_num: int = data["data"]["block_num"]
    print(f"    upload_prepare 完成: {block_num} 片，每片 {block_size:,} 字节")

    # ── Upload parts ─────────────────────────────────────────────────────────
    for seq in range(block_num):
        chunk = content[seq * block_size: (seq + 1) * block_size]
        resp = await client.post(
            f"{_FEISHU_BASE}/drive/v1/files/upload_part",
            headers=_hdr(token),
            data={
                "upload_id": upload_id,
                "seq": str(seq),
                "size": str(len(chunk)),
            },
            files={"file": ("chunk", chunk, "application/octet-stream")},
            timeout=60,
        )
        resp.raise_for_status()
        part_data = resp.json()
        if part_data.get("code") != 0:
            raise RuntimeError(
                f"upload_part 失败 (seq={seq}): "
                f"code={part_data['code']}, msg={part_data.get('msg')}"
            )
        print(f"    片段 {seq + 1}/{block_num} 上传成功")

    # ── Finish ───────────────────────────────────────────────────────────────
    resp = await client.post(
        f"{_FEISHU_BASE}/drive/v1/files/upload_finish",
        headers={**_hdr(token), "Content-Type": "application/json"},
        json={"upload_id": upload_id, "block_num": block_num},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"upload_finish 失败: code={data['code']}, msg={data.get('msg')}"
        )

    file_token = data["data"]["file_token"]
    print(f"    上传完成: file_token={file_token}")
    return file_token


# ─────────────────────────────────────────────────────────────────────────────
# 第四步：Import API（必须传 docx，飞书不支持 md）
# ─────────────────────────────────────────────────────────────────────────────

async def _create_import_task(
    client: httpx.AsyncClient,
    token: str,
    file_token: str,
    doc_title: str,
    folder_token: str,
) -> str:
    """
    创建导入任务，返回 ticket。

    关键参数说明：
      file_extension = "docx"  ← 必须是 docx，飞书 Import API 不支持 md
      type = "docx"            ← 目标格式也是 docx（原生飞书云文档）
    """
    resp = await client.post(
        f"{_FEISHU_BASE}/drive/v1/import_tasks",
        headers={**_hdr(token), "Content-Type": "application/json"},
        json={
            "file_extension": "docx",   # ← 必须是 docx，不能是 md
            "file_token": file_token,
            "type": "docx",
            "file_name": doc_title,
            "point": {
                "mount_type": 1,        # 1 = 云空间文件夹
                "mount_key": folder_token,
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"创建导入任务失败: code={data['code']}, msg={data.get('msg')}"
        )

    ticket = data["data"]["ticket"]
    print(f"    导入任务已创建: ticket={ticket}")
    return ticket


async def _poll_import(
    client: httpx.AsyncClient,
    token: str,
    ticket: str,
    timeout: int = 120,
) -> str:
    """轮询导入任务直到完成，返回飞书文档 token。"""
    deadline = time.time() + timeout
    interval = 2.0

    while time.time() < deadline:
        await asyncio.sleep(interval)

        resp = await client.get(
            f"{_FEISHU_BASE}/drive/v1/import_tasks/{ticket}",
            headers=_hdr(token),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                f"查询导入状态失败: code={data['code']}, msg={data.get('msg')}"
            )

        result = data["data"]["result"]
        status = result.get("job_status")

        if status == 0:                          # 成功
            doc_token = result["token"]
            print(f"    导入成功: doc_token={doc_token}")
            return doc_token
        if status in (2, 3):                     # 失败
            raise RuntimeError(f"导入失败: {result.get('job_error_msg', '未知错误')}")

        print(f"    导入进行中 (status={status})，等待 {interval:.0f}s ...")
        interval = min(interval * 1.5, 10)

    raise TimeoutError(f"导入任务超时（>{timeout}s）")


# ─────────────────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────────────────

async def import_md_to_feishu(md_path: str, doc_title: str = "") -> str:
    """
    将本地 Markdown 文件导入为飞书原生云文档。

    Args:
        md_path:   .md 文件路径
        doc_title: 飞书文档标题（默认取文件名）

    Returns:
        飞书云文档 URL (https://bytedance.feishu.cn/docx/...)
    """
    md_text = Path(md_path).read_text(encoding="utf-8")
    if not doc_title:
        doc_title = Path(md_path).stem

    folder_token = os.environ["FEISHU_FOLDER_TOKEN"]

    # ── Step 1: MD → .docx ───────────────────────────────────────────────────
    print("\n[1/4] Markdown → docx 转换中...")
    docx_bytes = MdToDocx().convert(md_text)
    docx_filename = f"{doc_title}.docx"
    print(f"    生成 {docx_filename}，大小 {len(docx_bytes):,} 字节")

    async with httpx.AsyncClient() as client:

        token = await _tenant_token(client)
        print(f"    tenant_access_token 已获取")

        # ── Step 2: 分片上传 ─────────────────────────────────────────────────
        print("\n[2/4] 分片上传到飞书云空间...")
        file_token = await _upload_chunked(
            client, token, docx_filename, docx_bytes, folder_token
        )

        # ── Step 3: 创建导入任务 ──────────────────────────────────────────────
        print("\n[3/4] 创建 Import 任务（file_extension=docx）...")
        ticket = await _create_import_task(
            client, token, file_token, doc_title, folder_token
        )

        # ── Step 4: 轮询等待 ──────────────────────────────────────────────────
        print("\n[4/4] 等待导入完成...")
        doc_token = await _poll_import(client, token, ticket)

    url = f"https://bytedance.feishu.cn/docx/{doc_token}"
    print(f"\n✓ 完成！飞书文档：{url}\n")
    return url


# ─────────────────────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────────────────────

def _check_env() -> None:
    missing = [k for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_FOLDER_TOKEN")
               if not os.environ.get(k)]
    if missing:
        import sys
        print("错误：缺少环境变量：", ", ".join(missing), file=sys.stderr)
        print("请在 .env 或 shell 中设置后重试。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="将本地 Markdown 文件导入为飞书原生云文档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python import_md_to_feishu.py summary.md
  python import_md_to_feishu.py summary.md --title "Q3 季度报告总结"

环境变量：
  FEISHU_APP_ID        飞书应用 App ID
  FEISHU_APP_SECRET    飞书应用 App Secret
  FEISHU_FOLDER_TOKEN  目标文件夹 token（云空间 URL 中 folder/ 后的字符串）

飞书应用权限（在开放平台配置）：
  drive:drive                  云空间（上传文件）
  drive:file:upload            文件上传
  docx:document                云文档读写
""",
    )
    parser.add_argument("md", help="Markdown 文件路径（.md）")
    parser.add_argument("--title", default="", help="飞书文档标题（默认取文件名）")
    args = parser.parse_args()

    _check_env()
    asyncio.run(import_md_to_feishu(args.md, args.title))
