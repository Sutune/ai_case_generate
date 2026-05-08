# AI 用例生成 Skill

根据需求文档生成结构化测试用例，并导出为 XMind 脑图或 Excel 表格。该 skill 支持解析本地文件、在线文档 URL 和图片，能够整合正文、图片 OCR、流程图节点与路径分析，再通过 OpenAI 兼容接口生成测试用例 Markdown，最后导出成可交付文件。

## 功能特性

- 支持输入：URL、PDF、DOCX、DOC、Markdown、TXT、HTML、常见图片格式。
- 支持图片 OCR：提取图片中的按钮文案、表格、权限矩阵、状态流转等内容。
- 支持流程图分析：识别流程图节点、连线、入口、出口和主要路径。
- 支持自动生成：调用 OpenAI 兼容 Chat Completions 接口生成 XMind 或 Excel 用例。
- 支持手动导出：将已有 Markdown 用例直接转换为 `.xmind` 或 `.xlsx`。
- 自动保存过程文件：Markdown、下载资源、抽取图片和最终导出文件都会落盘。
- 自动处理重名文件：同名 Markdown 和导出文件会追加时间戳，避免覆盖历史版本。

## 项目结构

```text
ai_case_generate/
├── SKILL.md                  # Codex/Claude skill 使用说明
├── README.md                 # 项目说明文档
├── manual_xmind_input.md     # 手动 XMind Markdown 示例
├── scripts/
│   ├── parser.py             # 解析需求文档，输出 JSON 上下文
│   ├── flowchart.py          # 流程图识别与路径分析
│   ├── generate_cases.py     # 自动生成测试用例并导出
│   └── exporter.py           # Markdown 导出 XMind/Excel
├── assets/
│   └── <用例标题>/
│       ├── resources/        # 下载文件、抽取图片、OCR 相关资源
│       └── markdown/         # 生成或导出的 Markdown 源文件
└── exports/                  # 最终导出的 .xmind / .xlsx 文件
```

## 环境依赖

建议使用 Python 3.10+。脚本依赖以下 Python 包：

```bash
pip install requests beautifulsoup4 pymupdf python-docx numpy pandas openpyxl xmind
```

如需图片 OCR 和流程图文字识别，还需要安装 Tesseract：

```bash
# macOS
brew install tesseract tesseract-lang
```

说明：

- 未安装 Tesseract 时，文档正文仍可解析，但图片 OCR 结果会为空。
- `.doc` 文件转换依赖 macOS 自带的 `/usr/bin/textutil`。
- 自动生成模式需要可访问 OpenAI 兼容接口。

## LLM 配置

自动生成模式支持通过环境变量配置模型服务：

```bash
export OPENAI_API_KEY="你的 API Key"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4.1"
```

也可以使用兼容变量名：

```bash
export LLM_API_KEY="你的 API Key"
export LLM_BASE_URL="https://your-provider.example.com/v1"
export LLM_MODEL="your-model-name"
```

命令行参数 `--api-key`、`--base-url`、`--model` 会覆盖环境变量。`--base-url` 可以传根地址或 `/v1` 地址，脚本会自动拼接到 `/v1/chat/completions`。

## 快速开始

### 1. 解析需求文档

```bash
python3 scripts/parser.py "/path/to/spec.docx"
```

解析成功后会输出 JSON，关键字段包括：

- `case_title`：用例标题。
- `resource_dir`：资源保存目录。
- `text`：文档正文。
- `images`：抽取出的图片路径。
- `image_ocr`：图片 OCR 文本。
- `image_analysis`：图片结构化分析和流程图分析。
- `llm_context.prompt_ready_context`：可直接提供给大模型的聚合上下文。

### 2. 自动生成并导出 XMind

```bash
python3 scripts/generate_cases.py "/path/to/spec.docx" xmind --title "登录注册流程"
```

### 3. 自动生成并导出 Excel

```bash
python3 scripts/generate_cases.py "/path/to/spec.docx" excel --title "登录注册流程"
```

### 4. 同时导出 XMind 和 Excel

```bash
python3 scripts/generate_cases.py "/path/to/spec.docx" both --title "登录注册流程"
```

### 5. 只查看生成 Prompt

不调用模型、不导出文件，适合检查需求上下文是否完整：

```bash
python3 scripts/generate_cases.py "/path/to/spec.docx" xmind --prompt-only
```

### 6. 使用已有 Markdown 导出

如果已经手动整理好了 XMind Markdown：

```bash
python3 scripts/exporter.py xmind "登录注册流程" "manual_xmind_input.md"
```

如果已经手动整理好了 Excel Markdown 表格：

```bash
python3 scripts/exporter.py excel "登录注册流程" "/path/to/cases.md"
```

也可以通过自动生成脚本跳过 LLM，直接导出已有 Markdown：

```bash
python3 scripts/generate_cases.py "/path/to/spec.docx" xmind --markdown-file "manual_xmind_input.md"
```

## 输入格式

| 类型 | 支持格式 |
| --- | --- |
| 在线文档 | `http://`、`https://` URL，支持 HTML、PDF、DOC、DOCX、TXT、Markdown、图片等 |
| 本地文档 | `.pdf`、`.docx`、`.doc`、`.md`、`.txt`、`.html`、`.htm` |
| 图片 | `.png`、`.jpg`、`.jpeg`、`.webp`、`.bmp`、`.gif`、`.tif`、`.tiff` |

URL 解析包含基本安全限制：仅允许 `http` 和 `https`，阻止 localhost、内网 IP、云元数据地址等目标，并限制远程文件最大 50MB。

## Markdown 输出约定

### XMind Markdown

XMind 导出要求 Markdown 必须是无序列表，且第一行是根节点：

```markdown
- 登录注册流程
    - 手机号验证码登录
        - 输入合法手机号和验证码登录成功
        - 验证码错误时提示失败
    - 扫码登录
        - 微信扫码命中已绑定账号后登录成功
```

### Excel Markdown

Excel 导出要求 Markdown 表格表头固定为：

```markdown
| 项目类型 | 项目 | 模块 | 子模块 | 功能点 | 用例标题 | 前置条件 | 优先级 | 测试步骤 | 期望结果 | 是否自动化 | 关联需求 | 是否准入用例 | 测试结果 | 用例作者 | 备注 | 附件图片 |
|------|----|-----|-----|------|-------------|----------|-----|----------------------------------|-----------------------------------------------------------|-------|------|--------|------|------|----|------|
```

其中 `模块`、`子模块`、`功能点`、`用例标题`、`前置条件`、`优先级`、`测试步骤`、`期望结果` 会生成内容，其他列保留为空。

## 输出位置

Markdown 源文件会保存到：

```text
assets/<用例标题>/markdown/
```

解析资源会保存到：

```text
assets/<用例标题>/resources/
```

最终文件会保存到：

```text
exports/
```

导出文件命名示例：

- `exports/登录注册流程_测试用例.xmind`
- `exports/登录注册流程_测试用例.xlsx`
- `assets/登录注册流程/markdown/登录注册流程_测试用例_xmind.md`

如果文件已存在，会自动追加时间戳，例如：

```text
登录注册流程_测试用例_2026-04-29_16-36-38.xmind
```

## 常见用法

解析在线 PDF 并生成 XMind：

```bash
python3 scripts/generate_cases.py "https://example.com/spec.pdf" xmind --title "生产票流转"
```

解析本地 PRD 并同时生成两种格式：

```bash
python3 scripts/generate_cases.py "./docs/prd.docx" both --title "用户账号与团队权限体系重构"
```

检查解析结果是否包含 OCR 和流程图信息：

```bash
python3 scripts/parser.py "./docs/prd.pdf"
```

导出示例脑图：

```bash
python3 scripts/exporter.py xmind "用户账号与团队权限体系重构" "manual_xmind_input.md"
```

## 排错指南

| 问题 | 可能原因 | 处理方式 |
| --- | --- | --- |
| `未提供 API Key` | 未配置模型接口密钥 | 设置 `OPENAI_API_KEY` 或传入 `--api-key` |
| `未提供 Base URL` | 未配置模型接口地址 | 设置 `OPENAI_BASE_URL` 或传入 `--base-url` |
| `需求上下文为空` | 文档无可解析文本且 OCR 无结果 | 检查源文件内容，或安装 Tesseract 后重试 |
| `暂不支持的文件格式` | 输入文件扩展名不在支持列表内 | 转换为 PDF、DOCX、TXT、Markdown 或图片 |
| `XMind Markdown 必须全部使用 '-' 无序列表格式` | Markdown 不符合脑图导出格式 | 保证所有层级都使用 `-` 列表 |
| `Excel Markdown 表头与约定字段不一致` | 表格列名或顺序被改动 | 使用 README 中固定表头 |
| URL 无法访问或被禁止 | 链接不可达、内网地址或超过 50MB | 换用可公开访问的 URL，或下载后使用本地文件 |

## 适用场景

- 根据 PRD、流程图、权限矩阵快速生成测试用例。
- 将业务流程拆解为 XMind 脑图，便于评审和补充。
- 将测试用例整理为 Excel 交付给测试管理平台或团队归档。
- 对包含图片、截图、流程图的需求文档做统一解析和测试覆盖。

