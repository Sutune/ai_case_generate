"""
自动用例生成模块 (generate_cases.py)

功能：
  - 读取 parser.py 解析结果中的 llm_context.prompt_ready_context
  - 拼接 XMind/Excel 生成提示词，调用 OpenAI 兼容接口生成 Markdown
  - 将生成结果传给 exporter.py 导出为 XMind 或 Excel 文件

使用方式：
  python3 generate_cases.py <URL或本地路径> <xmind|excel|both> [--title 标题]
  python3 generate_cases.py <URL或本地路径> xmind --prompt-only     # 只查看 prompt
  python3 generate_cases.py <URL或本地路径> xmind --markdown-file <已有Markdown路径>

配置（任选其一）：
  环境变量：OPENAI_API_KEY / LLM_API_KEY，OPENAI_BASE_URL / LLM_BASE_URL
  命令行参数：--api-key，--base-url，--model
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.exporter as exporter
import scripts.parser as requirement_parser


DEFAULT_MODEL = "gpt-4.1"
DEFAULT_TIMEOUT = 180
SYSTEM_PROMPT = (
    "你是一名资深的软件测试专家。"
    "你的任务是基于需求上下文输出高质量测试用例。"
    "严格遵守用户给出的输出格式要求，只输出最终 Markdown，不要解释。"
)

XMIND_PROMPT_TEMPLATE = """请基于下面的需求上下文生成 XMind 用例 Markdown。

要求：
1. 覆盖正文、图片OCR、流程图中的全部有效信息。
2. 如果存在流程图，必须覆盖每个入口、每个判定节点的全部分支、每条完整路径、每个汇合点、每个结束节点。
3. 同时覆盖主流程、异常流程、边界条件、逆向流程。
4. 只输出 markdown 无序列表，使用 '-' 作为列表标记，不要有空行，不要有解释。
5. 第一行必须是用例标题，标题使用：{case_title}

输出示例：
- 用例标题名称
    - 功能点1
      - 子功能1-1
      - 子功能1-2
        - 功能xxx
    - 功能点2
      - 子功能2-1

需求上下文如下：
{context}
"""

EXCEL_PROMPT_TEMPLATE = """请基于下面的需求上下文生成 Excel 用例 Markdown 表格。

要求：
1. 覆盖正文、图片OCR、流程图中的全部有效信息。
2. 如果存在流程图，至少为每个流程入口生成一组用例，为每个判定节点的每个分支生成一条用例，为每条完整路径生成一条端到端用例。
3. 对自动注册、绑定手机号、审批流转、失败回退等中间节点，要补充前置条件和期望结果。
4. 只输出 Markdown 表格，不要解释。
5. 除了模块、子模块、功能点、用例标题、前置条件、优先级、测试步骤、期望结果这几个字段生成内容，其他字段都为空但必须保留列。
6. 优先级只允许 P0、P1、P2。
7. 不要输出整行为空的行。

表头必须严格如下：
| 项目类型 | 项目 | 模块 | 子模块 | 功能点 | 用例标题 | 前置条件 | 优先级 | 测试步骤 | 期望结果 | 是否自动化 | 关联需求 | 是否准入用例 | 测试结果 | 用例作者 | 备注 | 附件图片 |
|------|----|-----|-----|------|-------------|----------|-----|----------------------------------|-----------------------------------------------------------|-------|------|--------|------|------|----|------|

需求上下文如下：
{context}
"""


class GenerationError(Exception):
    """自动用例生成过程中的业务异常。"""
    pass


def main() -> None:
    """命令行入口：解析参数、执行生成流程并以 JSON 形式输出结果。"""
    args = parse_args()
    try:
        result = generate_cases(
            source=args.source,
            export_format=args.format,
            case_title=args.title,
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
            prompt_only=args.prompt_only,
            markdown_file=args.markdown_file,
            timeout=args.timeout,
        )
        print(json.dumps(result, ensure_ascii=False))
    except GenerationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"error": f"生成失败: {exc}"}, ensure_ascii=False))
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    """定义并解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="基于 parser.llm_context 自动生成 XMind/Excel 测试用例并导出。"
    )
    parser.add_argument("source", help="需求文档 URL、本地文档路径或图片路径")
    parser.add_argument("format", choices=["xmind", "excel", "both"], help="导出格式")
    parser.add_argument("--title", help="用例标题，默认根据文件名/URL 自动生成")
    parser.add_argument("--model", default=resolve_model(), help="OpenAI 兼容接口模型名")
    parser.add_argument("--api-key", default=resolve_api_key(), help="OpenAI 兼容接口 API Key")
    parser.add_argument("--base-url", default=resolve_base_url(), help="OpenAI 兼容接口 Base URL，例如 https://api.openai.com/v1")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="LLM 请求超时时间（秒）")
    parser.add_argument("--prompt-only", action="store_true", help="只输出 prompt，不调用 LLM，也不导出文件")
    parser.add_argument(
        "--markdown-file",
        help="跳过 LLM，直接读取已有 Markdown 文件作为生成结果。仅支持单一格式（xmind 或 excel）。",
    )
    return parser.parse_args()


def generate_cases(
    source: str,
    export_format: str,
    case_title: str | None,
    model: str,
    api_key: str | None,
    base_url: str | None,
    prompt_only: bool,
    markdown_file: str | None,
    timeout: int,
) -> dict:
    """
    生成并导出测试用例。

    流程：
    1. 调用 parser.py 解析需求源，优先取 llm_context.prompt_ready_context。
    2. 根据目标格式构建 XMind/Excel prompt。
    3. prompt-only 模式直接返回 prompt。
    4. markdown-file 模式跳过模型调用，直接导出已有 Markdown。
    5. 普通模式调用 OpenAI 兼容接口生成 Markdown 后导出文件。
    """
    parsed = parse_source(source)
    if parsed.get("error"):
        raise GenerationError(parsed["error"])

    prompt_context = ((parsed.get("llm_context") or {}).get("prompt_ready_context") or "").strip()
    if not prompt_context:
        fallback_text = (parsed.get("text") or "").strip()
        if not fallback_text:
            raise GenerationError("需求上下文为空，无法生成测试用例。")
        prompt_context = fallback_text

    resolved_title = case_title or derive_case_title(source, parsed)
    formats = ["xmind", "excel"] if export_format == "both" else [export_format]

    prompts = {
        fmt: build_generation_prompt(fmt, resolved_title, prompt_context)
        for fmt in formats
    }

    result = {
        "source": source,
        "case_title": resolved_title,
        "format": export_format,
        "prompt_context_preview": collapse_text(prompt_context, 1200),
    }

    if prompt_only:
        result["prompts"] = prompts
        return result

    if markdown_file and len(formats) != 1:
        raise GenerationError("--markdown-file 仅支持单一格式，请不要与 both 一起使用。")

    generated_outputs = {}
    exports = []
    for fmt in formats:
        if markdown_file:
            markdown_text = Path(markdown_file).read_text(encoding="utf-8")
        else:
            if not api_key:
                raise GenerationError("未提供 API Key。请设置 --api-key 或环境变量 OPENAI_API_KEY/LLM_API_KEY。")
            if not base_url:
                raise GenerationError("未提供 Base URL。请设置 --base-url 或环境变量 OPENAI_BASE_URL/LLM_BASE_URL。")
            markdown_text = invoke_openai_compatible_llm(
                prompt=prompts[fmt],
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )

        generated_outputs[fmt] = markdown_text
        export_result = exporter.export_from_markdown(fmt, resolved_title, markdown_text)
        exports.append(export_result)

    result["generated_markdown"] = generated_outputs
    result["exports"] = exports
    return result


def parse_source(source: str) -> dict:
    """根据 source 类型分发到 URL 解析或本地文件解析。"""
    if source.startswith(("http://", "https://")):
        return requirement_parser.parse_url(source)
    return requirement_parser.parse_local_path(source)


def derive_case_title(source: str, parsed: dict) -> str:
    """从解析结果、下载文件名或 URL 路径中推导用例标题。"""
    parsed_title = (parsed.get("case_title") or "").strip()
    if parsed_title:
        return exporter.sanitize_filename(parsed_title, "测试用例")
    source_path = parsed.get("downloaded_file") or parsed.get("source") or source
    if isinstance(source_path, str) and source_path.startswith(("http://", "https://")):
        stem = Path(urlparse(source_path).path).stem or "测试用例"
    else:
        stem = Path(str(source_path)).stem or "测试用例"
    title = exporter.sanitize_filename(stem, "测试用例")
    return title


def build_generation_prompt(export_format: str, case_title: str, context: str) -> str:
    """按目标格式组装最终发送给 LLM 的提示词。"""
    if export_format == "xmind":
        return XMIND_PROMPT_TEMPLATE.format(case_title=case_title, context=context)
    if export_format == "excel":
        return EXCEL_PROMPT_TEMPLATE.format(context=context)
    raise GenerationError(f"不支持的生成格式: {export_format}")


def invoke_openai_compatible_llm(
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout: int,
) -> str:
    """
    调用 OpenAI 兼容 Chat Completions 接口并返回 Markdown 文本。

    兼容部分服务商返回的 content list 形式，并会剥离 Markdown 代码围栏。
    """
    endpoint = normalize_base_url(base_url) + "/chat/completions"
    payload = {
        "model": model or DEFAULT_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise GenerationError(f"LLM 请求失败: {exc}") from exc

    if response.status_code >= 400:
        raise GenerationError(f"LLM 请求失败: HTTP {response.status_code} {response.text[:500]}")

    try:
        data = response.json()
    except ValueError as exc:
        raise GenerationError(f"LLM 返回了非 JSON 内容: {response.text[:500]}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise GenerationError(f"LLM 返回结果缺少 choices: {data}")

    content = ((choices[0].get("message") or {}).get("content")) or ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    markdown = strip_markdown_fences(str(content).strip())
    if not markdown:
        raise GenerationError("LLM 未返回有效 Markdown。")
    return markdown


def strip_markdown_fences(text: str) -> str:
    """移除模型可能额外包裹的 ```markdown 代码围栏。"""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def resolve_model() -> str:
    """从环境变量读取模型名，未配置时使用默认模型。"""
    return (
        os.getenv("LLM_MODEL")
        or os.getenv("OPENAI_MODEL")
        or DEFAULT_MODEL
    )


def resolve_api_key() -> str | None:
    """从环境变量读取 OpenAI 兼容接口 API Key。"""
    return (
        os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )


def resolve_base_url() -> str | None:
    """从环境变量读取 OpenAI 兼容接口 Base URL。"""
    return (
        os.getenv("LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
    )


def normalize_base_url(base_url: str) -> str:
    """将根地址统一规范化到 /v1，便于拼接 /chat/completions。"""
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/v1"):
        return cleaned
    return cleaned + "/v1"


def collapse_text(text: str, max_chars: int) -> str:
    """压缩多余空白并按最大字符数截断，主要用于 JSON 预览。"""
    compact = " ".join((text or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


if __name__ == "__main__":
    main()
