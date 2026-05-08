---
name: "ai_case_generate"
description: "根据需求文档 URL 或本地文件生成测试用例，支持解析文本与图片 OCR，并导出为 XMind 或 Excel。适用于用户提供需求文档并要求输出结构化测试用例、脑图或表格文件时。"
---

# AI 用例生成 Skill

当用户要求“根据需求文档生成测试用例 / XMind / Excel”时，使用这个 skill。

## 输入支持

- 在线文档 URL：网页 HTML、PDF、DOC、DOCX 链接
- 本地文件：`.pdf`、`.docx`、`.doc`、`.md`、`.txt`
- 单张图片：`.png`、`.jpg`、`.jpeg`、`.webp`、`.bmp`、`.gif`、`.tif`、`.tiff`

## 工作流

1. 解析需求文档：
   - 执行：`python3 /Users/klyg/.trae/skills/ai_case_generate/parser.py '<URL或本地路径>'`
   - 读取返回 JSON 中的：
     - `case_title`：解析阶段确定的用例标题
     - `resource_dir`：本次解析资源目录
     - `text`：正文文本
     - `images`：提取后的图片路径
     - `image_ocr`：图片 OCR 结果
     - `image_analysis`：图片 OCR、结构化图片判断，以及流程图的节点/连线/路径分析结果
     - `llm_context.prompt_ready_context`：已经整理好的“正文 + OCR + 流程图路径 + 测试设计重点”提示词上下文
   - 如果返回 `error`，直接向用户报错，不继续生成。
2. 整理需求内容：
   - 优先使用 `llm_context.prompt_ready_context` 作为大模型输入的需求上下文。
   - 同时保留 `text`、`image_ocr`、`image_analysis` 作为补充，必要时交叉校验。
   - 当图片中包含表格、权限矩阵、按钮文案、状态流转、流程图等内容时，必须把 OCR 信息和 `image_analysis.flowchart` 一起纳入分析，不能只看正文。
3. 按用户目标生成 Markdown：
   - 手动模式：
     - 生成 XMind 时，必须输出无序列表 Markdown。
     - 生成 Excel 时，必须输出 Markdown 表格。
   - 自动模式：
     - 执行：`python3 /Users/klyg/.trae/skills/ai_case_generate/generate_cases.py '<URL或本地路径>' <xmind|excel|both> [--title '<用例标题>']`
     - 该脚本会自动：
       - 解析需求文档
       - 使用 `llm_context.prompt_ready_context` 拼接提示词
       - 调用 OpenAI 兼容接口生成 Markdown
       - 自动导出 XMind/Excel
     - 如果只想查看自动生成的 prompt，不调用模型：
       - `python3 /Users/klyg/.trae/skills/ai_case_generate/generate_cases.py '<URL或本地路径>' xmind --prompt-only`
4. 导出文件：
   - XMind：`python3 /Users/klyg/.trae/skills/ai_case_generate/exporter.py xmind '<用例标题>' '<Markdown内容或Markdown文件路径>'`
   - Excel：`python3 /Users/klyg/.trae/skills/ai_case_generate/exporter.py excel '<用例标题>' '<Markdown内容或Markdown文件路径>'`
   - `exporter.py` 会自动：
     - 将 Markdown 保存到 `assets/<用例标题>/markdown/`
     - 将最终文件导出到 `exports/`
     - XMind 文件自动补充 `META-INF/manifest.xml` 做兼容处理，支持 XMind 20+

## 文档解析规则

- URL 输入统一先下载，再按本地文档逻辑解析。
- 所有解析和下载得到的资源统一保存到 `assets/<用例标题>/resources/`。
- 本地文件解析时：
  - `<用例标题>` 默认使用需求文档文件名（不含扩展名）。
  - 本地文档中的图片、PDF 抽取图片、HTML 本地图片都会保存到固定 `resources/` 子目录，同一文档多次解析复用同一目录。
- URL 解析时：
  - `<用例标题>` 优先使用网页标题；如果无法获取网页标题，则回退为下载文件名或 URL 路径名。
  - 下载得到的 HTML、PDF、图片等原始资源，以及后续解析出的图片资源，都会保存到固定 `resources/` 子目录。
- 远程资源不再保存到 `downloads/` 目录。
- `.doc` 文件会先转换为 `.docx` 后再解析。
- 流程图图片会额外执行“节点检测 + 连线/箭头方向推断 + 拓扑重建”，输出到 `image_analysis[].flowchart`。
- 无法识别的文档格式、URL 无法访问、转换失败时，直接返回错误信息。

## 自动生成配置

自动模式 `generate_cases.py` 使用 OpenAI 兼容接口，请通过以下任一方式提供配置：

- 环境变量：
  - `OPENAI_API_KEY` 或 `LLM_API_KEY`
  - `OPENAI_BASE_URL` 或 `LLM_BASE_URL`
  - 可选：`OPENAI_MODEL` 或 `LLM_MODEL`
- 或命令行参数：
  - `--api-key`
  - `--base-url`
  - `--model`

补充说明：

- `--base-url` 传入根地址或 `/v1` 地址都可以，脚本会自动归一化到 `/v1/chat/completions`。
- 没有模型可用时，可先用 `--prompt-only` 查看 prompt，或用 `--markdown-file` 对已有 Markdown 结果做导出验证。

## XMind 生成提示词

使用下面这段提示词生成 Markdown：

```text
你是一名资深的软件测试专家，擅长从需求文档中整理所有的文字信息、图片OCR信息、流程图节点与路径信息并设计测试用例。请基于我提供的“需求上下文”设计测试用例，确保以下内容全部覆盖：
1. 正文中的功能点、规则、限制条件
2. 图片中的按钮文案、权限矩阵、表格字段、状态流转
3. 流程图中的每个入口、每个判定节点、每条主路径、每条分支路径、每个汇合点、每个结束节点
4. 正向流程、逆向流程、异常流程、边界条件
最终请按照内容层级整理输出，并以markdown无序列表'-'符号进行标记输出，请直接输出markdown内容，缩进使用空格控制，不要有空行。参考结果如下：
- 用例标题名称
    - 功能点1
      - 子功能1-1
      - 子功能1-2
        - 功能xxx
    - 功能点2
      - 子功能2-1
    - 功能点3
      - 子功能3-1
```

要求：

- 只输出 Markdown 列表内容，不要加解释。
- 第一行根节点就是用例标题。
- 不要出现空行。
- 如果需求上下文中包含流程图，必须显式覆盖：
  - 每个开始入口
  - 每个“是否/审批/判断”节点的全部分支
  - 每条从入口到结束节点的完整路径
  - 分支汇合后的后续处理
- 当流程图和正文存在差异时，优先同时保留并在用例树中分别覆盖，不要擅自丢弃任一来源的信息。

## Excel 生成提示词

使用下面这段提示词生成 Markdown 表格：

```text
你是一名资深的软件测试专家，擅长从需求文档中整理所有的文字信息、图片OCR信息、流程图节点与路径信息并设计测试用例。请基于我提供的“需求上下文”输出markdown表格测试用例，确保正文、图片、流程图中的内容都被覆盖，尤其要覆盖每个入口、每个判定分支、每条主路径、每条异常路径和每个结束结果。输出内容格式如下：
| 项目类型 | 项目 | 模块  | 子模块 | 功能点  | 用例标题        | 前置条件     | 优先级 | 测试步骤                             | 期望结果                                                      | 是否自动化 | 关联需求 | 是否准入用例 | 测试结果 | 用例作者 | 备注 | 附件图片 |
|------|----|-----|-----|------|-------------|----------|-----|----------------------------------|-----------------------------------------------------------|-------|------|--------|------|------|----|------|
|      |    | 智能体 | 入口  | 常驻入口 | 首页常驻入口芒小宝引导 | 接口下发引导数据 | P0  | 1、启动app,进入首页，查看搜索框 2、切换tab页，回到首页 | 1、首页顶部搜索框展示芒小宝动态引导，引导结束后展示芒小宝立绘态成为常驻入口<br>2、常驻入口依然展示且展示正常 |       |      |        |      |      |    |      |
|      |    | 智能体 | 入口  | 点播入口 | 点播页点击芒小宝    | 进入点播页    | P1  | 1、启动app,进入首页，查看搜索框 2、切换tab页，回到首页 | 1、首页顶部搜索框展示芒小宝动态引导，引导结束后展示芒小宝立绘态成为常驻入口<br>2、常驻入口依然展示且展示正常 |       |      |        |      |      |    |      |
注意以下规则:
1、除了模块、子模块、功能点、用例标题、前置条件、优先级、测试步骤、期望结果这几个字段生成内容，其他字段都为空,但是需要保留这些为空的字段。
2、直接输出表格数据，不需要其他描述语言。
3、不要输出整行为空的内容,注意对齐内容和字段位置。
4、优先级那一列P0代表高，P1代表中、P2代表低，总共三个档位。
```

要求：

- 只输出 Markdown 表格，不要加解释。
- 表头必须和示例完全一致。
- 不能漏空字段列。
- 如果需求上下文中包含流程图：
  - 至少为每个流程入口生成一组用例
  - 至少为每个判定节点的每个分支生成一条用例
  - 至少为每条完整业务路径生成一条端到端用例
  - 对自动注册、绑定手机号、审批流转、失败回退等中间节点，要补充前置条件和期望结果

## 文件命名与冲突处理

### Markdown 源文件（`assets/<用例标题>/markdown/`）

- **存储位置**：`assets/<用例标题>/markdown/`（与该用例的资源文件同目录管理）
- **命名规则**：`{用例标题}_{用例类型}.md`
  - 用例类型为 `xmind` 或 `excel`，示例：`组织迁移-角色权限_测试用例_xmind.md`
- **重名冲突处理**：
  - 首次导出：`xxx用例_xmind.md`
  - 重名时：自动追加时间戳 `xxx用例_xmind_2026-04-29_16-36-38.md`
  - 同秒重复：继续追加序号 `xxx用例_xmind_2026-04-29_16-36-38_01.md`

### 导出文件（`exports/`）

- **XMind 文件**：
  - 首次：`xxx用例.xmind`
  - 重名：`xxx用例_2026-04-29_16-36-38.xmind`
- **Excel 文件**：
  - 首次：`xxx用例.xlsx`
  - 重名：`xxx用例_2026-04-29_16-36-38.xlsx`
- **冲突解决**：所有版本自动保留，便于历史对比和追溯

## 导出结果

- Markdown 存储目录：`/Users/klyg/.trae/skills/ai_case_generate/assets/<用例标题>/markdown/`
- 导出目录：`/Users/klyg/.trae/skills/ai_case_generate/exports/`

## 示例

- 解析并导出 Excel：
  - “解析这个文档并生成 Excel 格式的测试用例：`/path/to/doc.docx`”
- 解析并导出 XMind：
  - “根据这个需求生成 XMind 脑图：https://example.com/spec.pdf”
- 自动生成并导出两种格式：
  - `python3 /Users/klyg/.trae/skills/ai_case_generate/generate_cases.py '/path/to/spec.docx' both --title '登录注册流程'`
- 只查看自动生成 prompt：
  - `python3 /Users/klyg/.trae/skills/ai_case_generate/generate_cases.py 'https://example.com/spec.pdf' xmind --prompt-only`
