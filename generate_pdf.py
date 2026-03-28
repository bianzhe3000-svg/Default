#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate PDF for OpenClaw + Feishu solution document."""

from fpdf import FPDF
from fpdf.enums import XPos, YPos

FONT_PATH = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
OUTPUT = "/home/user/Default/OpenClaw_Feishu_PDF解读方案.pdf"

CONTENT = [
    # (type, text)
    ("title", "OpenClaw + 飞书 PDF 文档解读系统"),
    ("subtitle", "落地实施方案"),
    ("date", "2026-02-23"),

    ("h1", "一、整体架构"),
    ("body", "用户发送 PDF 到飞书机器人，OpenClaw 接收后调用 mineru-pdf Skill 提取结构化文本与表格，再由内置 LLM 做深度解读分析，最终通过 create_and_write 写入飞书云文档（Markdown → Block），机器人回复文档链接。"),
    ("code", "用户发送 PDF → OpenClaw 接收\n  → [Skill] mineru-pdf 解析\n  → [AI] 深度解读\n  → [Skill] create_and_write 写飞书云文档\n  → 机器人回复文档链接"),

    ("h1", "二、第一步：部署 OpenClaw"),
    ("h2", "推荐方式：VPS 自托管"),
    ("code", "# 安装 OpenClaw\ncurl -fsSL https://get.openclaw.ai | sh\n\n# 或使用 Docker\ndocker run -d --name openclaw \\\n  -v ~/.openclaw:/root/.openclaw \\\n  -e ANTHROPIC_API_KEY=your_key \\\n  openclaw/openclaw:latest"),
    ("body", "模型选择建议：长文档解读推荐 claude-opus-4 或 claude-sonnet-4（200k context，处理大 PDF 不截断）；预算有限可用本地 Ollama + qwen2.5:72b。"),

    ("h1", "三、第二步：接入飞书"),
    ("h2", "3.1 在飞书开放平台创建应用"),
    ("body", "访问 open.feishu.cn → 创建企业自建应用，开启以下权限："),
    ("bullet", "im.message.receive_v1 — 接收消息（含文件）"),
    ("bullet", "im.message:write — 发送消息和卡片"),
    ("bullet", "docx:document:create — 创建云文档"),
    ("bullet", "docx:document:write — 写入云文档内容"),
    ("bullet", "drive:file:upload — 上传 PDF 附件"),
    ("body", "记录 App ID 和 App Secret 备用。"),

    ("h2", "3.2 安装飞书插件并连接"),
    ("code", "# 安装官方飞书插件\nopenclaw plugins install @openclaw/feishu\n\n# 交互式添加飞书渠道\nopenclaw channels add\n# 选择 Feishu → 输入 App ID → 输入 App Secret\n\n# 重启并确认连接\nopenclaw gateway restart\nopenclaw logs --follow"),
    ("tip", "飞书使用 WebSocket 长连接，无需公网 IP、无需 ngrok，本地机器或 VPS 均可运行。"),

    ("h1", "四、第三步：安装 PDF 解析 Skill"),
    ("code", "# 方案A：mineru-pdf（推荐，支持表格+公式）\nnpx playbooks add skill openclaw/skills --skill mineru-pdf\n\n# 安装底层引擎\npip install magic-pdf[full] --extra-index-url https://wheels.myhloli.com"),
    ("body", "mineru-pdf 能力：基于 MinerU 引擎，输出 Markdown + JSON；支持表格结构化提取；支持公式提取（LaTeX 格式）；Apple Silicon 可开启 MLX 加速；扫描版 PDF 内置 OCR。"),

    ("h1", "五、第四步：编写 OpenClaw Prompt"),
    ("body", "在 OpenClaw 的 CLAUDE.md（系统提示）中配置以下 PDF 解读流程规则："),
    ("code", "## PDF 文档解读流程\n\n第1步：调用 mineru-pdf 解析文件（>100页则分段）\n第2步：生成解读报告，格式如下：\n  - 一、文档概述（200字）\n  - 二、核心摘要（300字）\n  - 三、关键数据与发现（5-10条）\n  - 四、逐章节详细解读（100%覆盖）\n  - 五、结论与建议（400字）\n  - 六、待跟进事项（Checklist）\n第3步：调用 create_and_write 写入飞书云文档\n  标题格式：[解读] 原文件名 YYYY-MM-DD"),

    ("h1", "六、防止格式混乱的专项措施"),
    ("h2", "问题1：飞书写入时 Markdown 格式丢失"),
    ("body", "原因：直接发文本而非用 Block API 写入。解决：使用 m1heng 插件的 create_and_write 原子操作，内置 Markdown-to-Block 转换。"),
    ("code", "curl -O https://registry.npmjs.org/@m1heng-clawd/feishu/-/feishu-0.1.3.tgz\nopenclaw plugins install ./feishu-0.1.3.tgz"),

    ("h2", "问题2：多列 PDF 文本顺序错乱"),
    ("body", "在 Prompt 中添加：如果解析结果出现明显阅读顺序混乱（左右栏文字交叉），按列分离文本，左列读完再读右列。"),

    ("h2", "问题3：长文档内容被截断"),
    ("body", "选用 200k context 模型（如 claude-sonnet-4）；Prompt 中要求分析完成后列出所有章节标题，对照确认全部覆盖。"),

    ("h2", "问题4：表格在飞书中变成纯文本"),
    ("body", "mineru-pdf 输出 JSON 格式表格，在 Prompt 中指定：原文中的表格必须在飞书文档中以表格 Block 形式呈现，不得转换为纯文本或列表。"),

    ("h1", "七、完整使用流程（用户视角）"),
    ("bullet", "打开飞书，找到你的 OpenClaw 机器人"),
    ("bullet", "直接发送 PDF 文件（拖拽即可）"),
    ("bullet", "可选附加指令：「请重点分析第三章的财务数据」"),
    ("bullet", "等待 1-3 分钟（取决于文档大小）"),
    ("bullet", "收到飞书云文档链接，点击查看格式化结果"),

    ("h1", "八、依赖清单"),
    ("table_header", "组件 | 作用 | 获取方式"),
    ("table_row", "OpenClaw Gateway | 核心 Agent 引擎 | openclaw.ai"),
    ("table_row", "@openclaw/feishu | 飞书消息收发 | openclaw plugins install"),
    ("table_row", "mineru-pdf Skill | PDF 解析 | playbooks.com"),
    ("table_row", "@m1heng-clawd/feishu | 飞书云文档写入 | github.com/m1heng"),
    ("table_row", "MinerU | mineru-pdf 底层引擎 | pip install magic-pdf"),
    ("table_row", "LLM API | AI 推理 | Anthropic / OpenAI / Ollama"),

    ("h1", "九、参考资源"),
    ("bullet", "OpenClaw 官网: https://openclaw.ai/"),
    ("bullet", "mineru-pdf Skill: https://playbooks.com/skills/openclaw/skills/mineru-pdf"),
    ("bullet", "openclaw-feishu 配置指南: https://github.com/AlexAnys/openclaw-feishu"),
    ("bullet", "feishu-openclaw 桥接器: https://github.com/AlexAnys/feishu-openclaw"),
    ("bullet", "m1heng 文档写入插件: https://github.com/m1heng/clawdbot-feishu"),
    ("bullet", "飞书官方: OpenClaw 完全指南: https://www.feishu.cn/content/article/7602519239445974205"),
]


class PDF(FPDF):
    def __init__(self):
        super().__init__()
        self.add_font("WQY", "", FONT_PATH)
        self.add_font("WQY", "B", FONT_PATH)
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(20, 20, 20)

    def header(self):
        if self.page_no() > 1:
            self.set_font("WQY", size=8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 8, "OpenClaw + 飞书 PDF 文档解读系统 — 落地实施方案", align="R",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_text_color(0, 0, 0)
            self.ln(2)

    def footer(self):
        self.set_y(-15)
        self.set_font("WQY", size=8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"第 {self.page_no()} 页", align="C")
        self.set_text_color(0, 0, 0)


def generate():
    pdf = PDF()
    pdf.add_page()

    for item_type, text in CONTENT:
        if item_type == "title":
            pdf.set_font("WQY", size=22)
            pdf.set_text_color(30, 80, 160)
            pdf.multi_cell(0, 12, text, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)

        elif item_type == "subtitle":
            pdf.set_font("WQY", size=14)
            pdf.set_text_color(80, 80, 80)
            pdf.multi_cell(0, 8, text, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)

        elif item_type == "date":
            pdf.set_font("WQY", size=10)
            pdf.set_text_color(130, 130, 130)
            pdf.multi_cell(0, 6, text, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(8)
            # separator line
            pdf.set_draw_color(30, 80, 160)
            pdf.set_line_width(0.8)
            pdf.line(20, pdf.get_y(), 190, pdf.get_y())
            pdf.ln(8)
            pdf.set_text_color(0, 0, 0)

        elif item_type == "h1":
            pdf.ln(4)
            pdf.set_font("WQY", size=14)
            pdf.set_text_color(30, 80, 160)
            pdf.set_fill_color(235, 242, 255)
            pdf.multi_cell(0, 9, text, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
            pdf.set_text_color(0, 0, 0)

        elif item_type == "h2":
            pdf.ln(2)
            pdf.set_font("WQY", size=12)
            pdf.set_text_color(50, 50, 150)
            pdf.multi_cell(0, 8, "▶ " + text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
            pdf.set_text_color(0, 0, 0)

        elif item_type == "body":
            pdf.set_font("WQY", size=10)
            pdf.set_text_color(40, 40, 40)
            pdf.multi_cell(0, 7, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)

        elif item_type == "bullet":
            pdf.set_font("WQY", size=10)
            pdf.set_text_color(40, 40, 40)
            pdf.set_x(25)
            pdf.multi_cell(0, 7, "- " + text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        elif item_type == "code":
            pdf.ln(1)
            pdf.set_fill_color(245, 245, 245)
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.3)
            # draw background rect
            lines = text.split("\n")
            block_h = len(lines) * 6 + 6
            x, y = pdf.get_x(), pdf.get_y()
            pdf.rect(x, y, 170, block_h, style="FD")
            pdf.ln(3)
            pdf.set_font("WQY", size=8.5)
            pdf.set_text_color(20, 80, 20)
            for line in lines:
                pdf.set_x(25)
                pdf.multi_cell(160, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            pdf.set_text_color(0, 0, 0)

        elif item_type == "tip":
            pdf.ln(1)
            pdf.set_fill_color(255, 251, 230)
            pdf.set_draw_color(255, 200, 0)
            pdf.set_line_width(0.5)
            x, y = pdf.get_x(), pdf.get_y()
            lines = text.split("\n")
            block_h = len(lines) * 7 + 6
            pdf.rect(x, y, 170, block_h, style="FD")
            pdf.ln(3)
            pdf.set_font("WQY", size=10)
            pdf.set_text_color(100, 70, 0)
            pdf.set_x(25)
            pdf.multi_cell(160, 7, "[提示] " + text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
            pdf.set_text_color(0, 0, 0)

        elif item_type == "table_header":
            pdf.ln(2)
            cols = text.split(" | ")
            col_w = [50, 60, 60]
            pdf.set_font("WQY", size=9)
            pdf.set_fill_color(30, 80, 160)
            pdf.set_text_color(255, 255, 255)
            for i, col in enumerate(cols):
                pdf.cell(col_w[i], 8, col, border=1, fill=True)
            pdf.ln()
            pdf.set_text_color(0, 0, 0)

        elif item_type == "table_row":
            cols = text.split(" | ")
            col_w = [50, 60, 60]
            pdf.set_font("WQY", size=9)
            pdf.set_fill_color(248, 248, 248)
            alt = (CONTENT.index((item_type, text)) % 2 == 0)
            if alt:
                pdf.set_fill_color(235, 242, 255)
            for i, col in enumerate(cols):
                pdf.cell(col_w[i], 7, col, border=1, fill=True)
            pdf.ln()

    pdf.output(OUTPUT)
    print(f"PDF generated: {OUTPUT}")


if __name__ == "__main__":
    generate()
