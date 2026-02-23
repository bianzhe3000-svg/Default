#!/usr/bin/env python3
"""
PDF → 飞书云文档 CLI 入口

用法：
  # 基础用法（PyMuPDF 解析）
  python run_pdf_to_feishu.py report.pdf --title "季度报告总结"

  # 使用 MinerU 解析结果（推荐用于双栏/复杂排版 PDF）
  mineru -p report.pdf -o ./output -m hybrid
  python run_pdf_to_feishu.py report.pdf \\
      --title "技术文档总结" \\
      --mineru ./output/report/auto/content_list.json \\
      --save-md debug.md

环境变量（必须设置）：
  OPENAI_API_KEY       OpenAI API Key
  FEISHU_APP_ID        飞书应用 App ID
  FEISHU_APP_SECRET    飞书应用 App Secret
  FEISHU_FOLDER_TOKEN  目标文件夹 token

飞书 FOLDER_TOKEN 获取方式：
  1. 打开飞书云空间 → 进入目标文件夹
  2. 复制浏览器 URL，取 folder/ 后面的字符串
     例：https://xxx.feishu.cn/drive/folder/AbCdEfGh → AbCdEfGh

飞书应用配置：
  1. 前往 https://open.feishu.cn/app 创建企业自建应用
  2. 开通权限：
     - drive:drive（云空间文件读写）
     - docx:document（云文档读写）
  3. 发布应用 → 将应用机器人加入目标文件夹协作者
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 把项目根目录加到 sys.path，保证 src 包可以找到
sys.path.insert(0, str(Path(__file__).parent))

from src.modules.pdf_to_feishu import PdfToFeishuConfig, pdf_to_feishu
from src.utils.logger import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PDF → 飞书云文档：解析 PDF → LLM 总结 → 生成 Markdown → 导入飞书",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf", help="PDF 文件路径")
    parser.add_argument("--title", default="", help="飞书文档标题（默认使用 PDF 文件名）")
    parser.add_argument(
        "--mineru",
        metavar="CONTENT_LIST_JSON",
        help="MinerU 输出的 content_list.json 路径（可选，精度更高）",
    )
    parser.add_argument(
        "--save-md",
        metavar="PATH",
        help="同时把生成的 Markdown 保存到本地（可选，便于调试）",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o",
        help="OpenAI 模型名称（默认：gpt-4o）",
    )
    parser.add_argument(
        "--pages-per-chunk",
        type=int,
        default=5,
        help="无 TOC 时每块的页数（默认：5）",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=4000,
        help="每节喂给 LLM 的最大字符数（默认：4000）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认：INFO）",
    )
    return parser.parse_args()


def check_env() -> None:
    required = ["OPENAI_API_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_FOLDER_TOKEN"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print("错误：缺少以下环境变量：", file=sys.stderr)
        for k in missing:
            print(f"  {k}", file=sys.stderr)
        print("\n请在 .env 文件或 shell 中设置后重试。", file=sys.stderr)
        sys.exit(1)


async def main() -> None:
    args = parse_args()
    setup_logging(log_level=args.log_level)
    check_env()

    pdf_path = str(Path(args.pdf).resolve())
    if not Path(pdf_path).exists():
        print(f"错误：找不到文件 {pdf_path}", file=sys.stderr)
        sys.exit(1)

    doc_title = args.title or Path(args.pdf).stem

    config = PdfToFeishuConfig(
        openai_model=args.model,
        pages_per_chunk=args.pages_per_chunk,
        max_chars_per_section=args.max_chars,
    )

    url = await pdf_to_feishu(
        pdf_path=pdf_path,
        doc_title=doc_title,
        config=config,
        mineru_content_list=args.mineru,
        save_md_to=args.save_md,
    )

    print(f"\n飞书文档已生成：{url}")


if __name__ == "__main__":
    asyncio.run(main())
