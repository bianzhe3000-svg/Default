# 播客自动化内容处理系统

Automated Podcast Content Processing System

一套完整的播客内容自动抓取、音频转录、中文语义分析和 Markdown 文档生成系统。

## 功能特性

### 1. RSS Feed 自动发现与抓取
- 集成 iTunes API、Spotify API 进行播客搜索
- 支持 OPML 文件导入/导出
- 模糊匹配和智能推荐
- RSS Feed 自动验证和解析
- 增量更新检测（默认24小时窗口）

### 2. 音频内容转录
- 集成 OpenAI Whisper API
- 支持 MP3、M4A、WAV、OGG 等多种格式
- 大文件自动分片转录
- 多语言支持（默认中文）

### 3. 中文语义分析
- 1000-2000字精炼概述
- 5-8个核心要点提取
- 主要观点识别和详细展开
- 知识点分类整理

### 4. 定时任务调度
- Cron 表达式配置（默认北京时间每日 23:00）
- 指数退避重试机制（最多3次）
- 详细任务日志记录

### 5. Markdown 文档生成
- 标准化 Jinja2 模板
- 按播客名称分目录存储
- 标准化命名：`YYYY-MM-DD-podcast-summary.md`
- 支持 HTML 预览和 PDF 导出

## 系统架构

```
src/
├── main.py                 # FastAPI 应用入口
├── config.py               # 配置管理
├── database.py             # 数据库层
├── models/                 # 数据模型
│   ├── podcast.py          # Podcast + Episode
│   └── task.py             # TaskLog
├── modules/                # 核心处理模块
│   ├── rss_discovery.py    # RSS 搜索发现
│   ├── rss_validator.py    # Feed 验证解析
│   ├── opml_parser.py      # OPML 导入导出
│   ├── audio_transcriber.py # 音频转录
│   ├── content_analyzer.py # 内容分析
│   └── markdown_generator.py # 文档生成
├── scheduler/
│   └── task_scheduler.py   # 任务调度
├── api/
│   ├── routes.py           # REST API 路由
│   └── schemas.py          # Pydantic 模型
└── utils/
    ├── logger.py           # 日志工具
    ├── retry.py            # 重试机制
    └── memory_monitor.py   # 内存监控
```

## 快速开始

### 环境要求
- Python 3.10+
- ffmpeg（用于音频处理）
- OpenAI API Key

### 安装步骤

1. **克隆项目**
```bash
git clone <repository-url>
cd podcast-automation
```

2. **创建虚拟环境**
```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
# 或 venv\Scripts\activate  # Windows
```

3. **安装依赖**
```bash
pip install -r requirements.txt
```

4. **配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 文件，填入你的 API 密钥
```

5. **配置播客源（可选）**
```bash
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml，添加你要监听的播客
```

6. **启动服务**
```bash
python -m src.main
```

服务将在 `http://localhost:8000` 启动。

### Docker 部署

```bash
# 复制并编辑配置
cp .env.example .env
cp config/config.example.yaml config/config.yaml

# 构建并启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

## API 文档

启动服务后访问：
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/podcasts` | 列出所有播客 |
| POST | `/api/podcasts` | 添加播客 |
| DELETE | `/api/podcasts/{id}` | 删除播客 |
| GET | `/api/podcasts/{id}` | 获取播客详情 |
| GET | `/api/podcasts/{id}/episodes` | 列出剧集 |
| POST | `/api/podcasts/search` | 搜索播客 |
| POST | `/api/podcasts/import-opml` | 导入 OPML |
| POST | `/api/crawl` | 手动触发抓取 |
| GET | `/api/episodes/{id}` | 剧集详情（含分析结果） |
| POST | `/api/episodes/{id}/reprocess` | 重新处理剧集 |
| GET | `/api/documents` | 列出文档 |
| GET | `/api/documents/view` | 查看文档 |
| GET | `/api/documents/export` | 导出文档 |
| GET | `/api/tasks` | 任务日志 |
| GET | `/api/scheduler/status` | 调度器状态 |
| POST | `/api/scheduler/start` | 启动调度器 |
| POST | `/api/scheduler/stop` | 停止调度器 |
| GET | `/api/memory` | 内存统计 |

### 使用示例

**添加播客**
```bash
curl -X POST http://localhost:8000/api/podcasts \
  -H "Content-Type: application/json" \
  -d '{"name": "我的播客", "rss_url": "https://example.com/feed.xml"}'
```

**搜索播客**
```bash
curl -X POST http://localhost:8000/api/podcasts/search \
  -H "Content-Type: application/json" \
  -d '{"query": "科技播客", "limit": 10}'
```

**手动触发抓取**
```bash
curl -X POST http://localhost:8000/api/crawl
```

**导入 OPML**
```bash
curl -X POST http://localhost:8000/api/podcasts/import-opml \
  -F "file=@my_podcasts.opml"
```

## 配置说明

### 环境变量 (.env)

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | 数据库连接字符串 | `sqlite+aiosqlite:///./data/podcast.db` |
| `OPENAI_API_KEY` | OpenAI API 密钥 | - |
| `OPENAI_MODEL` | 内容分析模型 | `gpt-4` |
| `WHISPER_MODEL` | 音频转录模型 | `whisper-1` |
| `SCHEDULER_CRON_HOUR` | 定时任务小时 | `23` |
| `SCHEDULER_CRON_MINUTE` | 定时任务分钟 | `0` |
| `SCHEDULER_TIMEZONE` | 时区 | `Asia/Shanghai` |
| `MAX_CONCURRENT_FEEDS` | 最大并发 Feed 数 | `5` |
| `INCREMENTAL_UPDATE_HOURS` | 增量更新时间窗口（小时） | `24` |
| `MAX_RETRY_ATTEMPTS` | 最大重试次数 | `3` |
| `MEMORY_LIMIT_MB` | 内存限制（MB） | `1024` |

### YAML 配置 (config/config.yaml)

用于配置播客 RSS 源列表和详细的处理参数，见 `config/config.example.yaml`。

## 测试

```bash
# 运行所有测试
pytest tests/ -v

# 运行特定模块测试
pytest tests/test_rss_validator.py -v

# 测试覆盖率
pytest tests/ --cov=src --cov-report=html
```

## 项目结构

```
.
├── src/                    # 源代码
├── tests/                  # 测试用例
├── templates/              # Jinja2 模板
├── config/                 # 配置文件
├── summaries/              # 生成的 Markdown 文档
├── .env.example            # 环境变量模板
├── Dockerfile              # Docker 镜像定义
├── docker-compose.yml      # Docker Compose 配置
├── pyproject.toml          # Python 项目配置
├── requirements.txt        # Python 依赖
└── README.md               # 本文档
```

## 技术栈

- **后端框架**: FastAPI + Uvicorn
- **数据库**: SQLAlchemy + SQLite/PostgreSQL
- **任务调度**: APScheduler
- **音频转录**: OpenAI Whisper API
- **内容分析**: OpenAI GPT-4
- **RSS 解析**: feedparser
- **模板引擎**: Jinja2
- **HTTP 客户端**: httpx
- **容器化**: Docker + Docker Compose

## 许可证

MIT License
