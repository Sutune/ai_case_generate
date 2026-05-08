"""
文档解析模块 (parser.py)

功能：
  - 支持多格式文档解析（URL、PDF、DOCX、HTML、纯文本、图片等）
  - OCR 识别图片中的文字（支持中英文）
  - 流程图智能识别和分析
  - 安全校验（防止 SSRF、路径穿越、超大文件下载）
  - 生成 LLM 上下文（整合正文、OCR、流程图分析、测试重点）

安全特性：
  - SSRF 防护：只允许 http/https，阻止内网 IP（10.x, 192.168.x, 169.254.x 等）
  - 大小限制：远程下载不超过 50MB，防止 OOM
  - 路径穿越防护：HTML 本地图片需在指定目录内
  - 合并单元格去重：DOCX 表格处理
"""

import base64
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.flowchart import analyze_flowchart_image

try:
    import fitz  # PyMuPDF - PDF 解析库
except ImportError:
    fitz = None

try:
    import docx  # python-docx - Word 文档解析库
    from docx.oxml.ns import qn
except ImportError:
    docx = None
    qn = None


# ============ 目录和文件配置 ============
ASSETS_ROOT = PROJECT_ROOT / "assets"      # 统一资源目录

# ============ 文件扩展名分类 ============
TEXT_EXTENSIONS = {".md", ".txt"}          # 纯文本格式
HTML_EXTENSIONS = {".html", ".htm"}        # 网页格式
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}  # 图片格式

# ============ 网络请求配置 ============
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ============ 安全限制参数 ============
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024      # 最大下载文件大小：50MB（防止 OOM）

# ============ SSRF 防护：被阻止的主机和 IP 段 ============
_BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
}
_BLOCKED_PREFIXES = (
    "10.", "192.168.",                     # 私有 IP 段
    "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
    "169.254.",                            # 云元数据服务（AWS、GCP、阿里云）
    "fd", "fe80",                          # IPv6 私有段和链路本地
)


class ParserError(Exception):
    """解析错误异常类"""
    pass


# ── 安全校验 ────────────────────────────────────────────────────────────────

def _validate_url(url: str) -> None:
    """校验 URL scheme 及目标主机，阻止 SSRF。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ParserError(f"不支持的 URL scheme: {parsed.scheme!r}，仅允许 http/https。")
    host = parsed.hostname or ""
    if host in _BLOCKED_HOSTS:
        raise ParserError(f"禁止访问内网地址: {host}")
    if any(host.startswith(prefix) for prefix in _BLOCKED_PREFIXES):
        raise ParserError(f"禁止访问内网地址: {host}")


def _validate_local_path(candidate: Path, base_dir: Path) -> None:
    """校验路径是否在允许目录内，阻止路径穿越。"""
    try:
        candidate.resolve().relative_to(base_dir.resolve())
    except ValueError:
        raise ParserError(f"路径穿越攻击被阻止: {candidate}")


# ── 工具函数 ────────────────────────────────────────────────────────────────

def sanitize_filename(name: str, fallback: str) -> str:
    """替换文件名中的非法字符为下划线，确保跨平台可用。"""
    cleaned = "".join("_" if char in '<>:"/\\|?*\n\r\t' else char for char in name).strip()
    cleaned = cleaned.rstrip(".")
    return cleaned or fallback


def sanitize_case_title(name: str | None, fallback: str = "测试用例") -> str:
    """对用例标题做文件名净化，并折叠多余空白；为空时回退到 fallback。"""
    title = sanitize_filename(name or "", fallback)
    title = re.sub(r"\s+", " ", title).strip()
    return title or fallback


def create_resource_dir(case_title: str, now: datetime | None = None) -> Path:
    """创建并返回固定资源目录 assets/<用例标题>/resources/，同一文档多次解析复用同一目录。"""
    folder = ASSETS_ROOT / sanitize_case_title(case_title) / "resources"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def guess_extension(filename: str | None, content_type: str | None, default: str) -> str:
    """优先从文件名推断扩展名，其次从 Content-Type 推断，均失败时使用默认扩展名。"""
    if filename:
        suffix = Path(unquote(filename)).suffix.lower()
        if suffix:
            return suffix

    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip().lower())
        if guessed:
            return guessed

    return default


def save_bytes(folder: Path, payload: bytes, suffix: str, prefix: str) -> Path:
    """将二进制内容保存到资源目录，文件名追加短 UUID 避免覆盖。"""
    filename = f"{sanitize_filename(prefix, prefix)}_{uuid.uuid4().hex[:8]}{suffix}"
    output_path = folder / filename
    output_path.write_bytes(payload)
    return output_path


def _stream_download(url: str, timeout: int = 30) -> bytes:
    """带大小限制的流式下载，超过 MAX_DOWNLOAD_BYTES 则报错。"""
    _validate_url(url)
    with requests.get(url, headers=REQUEST_HEADERS, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ParserError(
                        f"远程资源超过大小限制 {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB，已中止下载。"
                    )
                chunks.append(chunk)
        return b"".join(chunks)


# ── 结果构建 ────────────────────────────────────────────────────────────────

def build_result(
    source: str,
    source_type: str,
    document_type: str,
    case_title: str,
    resource_dir: str,
    text: str,
    images: list[str],
    image_ocr: dict[str, str],
    downloaded_file: str | None = None,
) -> dict:
    """统一组装解析结果，并对提取图片补充 OCR、结构化内容和流程图分析。"""
    image_analysis = []
    preferred_lang = pick_ocr_language()
    for path in images:
        ocr_text = image_ocr.get(path, "")
        flowchart = analyze_flowchart_image(path, preferred_lang)
        contains_structured_content = has_structured_content(ocr_text) or flowchart.get("is_flowchart", False)
        image_analysis.append(
            {
                "path": path,
                "ocr_text": ocr_text,
                "contains_structured_content": contains_structured_content,
                "structure_type": "flowchart" if flowchart.get("is_flowchart") else ("structured_image" if contains_structured_content else "generic_image"),
                "flowchart": flowchart,
            }
        )

    llm_context = build_llm_context(text, image_analysis)

    return {
        "source": source,
        "source_type": source_type,
        "document_type": document_type,
        "case_title": case_title,
        "resource_dir": resource_dir,
        "downloaded_file": downloaded_file,
        "text": text,
        "images": images,
        "image_ocr": image_ocr,
        "image_analysis": image_analysis,
        "llm_context": llm_context,
    }


def build_llm_context(text: str, image_analysis: list[dict]) -> dict:
    """
    将正文、OCR 文本、流程图分析整合为 LLM 可直接使用的 prompt_ready_context。

    返回字段：
      - prompt_ready_context：聚合后的完整上下文字符串
      - ocr_sections：每张图片的 OCR 摘要列表
      - flowchart_sections：每张流程图的结构化描述列表
      - testcase_focus：基于流程图推导的测试设计重点列表
    """
    ocr_sections = []
    flowchart_sections = []
    testcase_focus = []

    for index, item in enumerate(image_analysis, start=1):
        ocr_text = (item.get("ocr_text") or "").strip()
        if ocr_text:
            compact_ocr = collapse_text(ocr_text, max_chars=500)
            ocr_sections.append(f"图片{index} OCR：{compact_ocr}")

        flowchart = item.get("flowchart") or {}
        if flowchart.get("is_flowchart"):
            flowchart_sections.append(format_flowchart_context(index, flowchart))
            testcase_focus.extend(derive_flowchart_case_hints(flowchart))

    testcase_focus = dedupe_keep_order(testcase_focus)
    prompt_sections = []
    normalized_text = text.strip()

    if normalized_text:
        prompt_sections.append("【需求正文】\n" + normalized_text)
    if ocr_sections:
        prompt_sections.append("【图片OCR补充】\n" + "\n".join(f"- {section}" for section in ocr_sections))
    if flowchart_sections:
        prompt_sections.append("【流程图分析】\n" + "\n\n".join(flowchart_sections))
    if testcase_focus:
        prompt_sections.append("【测试设计重点】\n" + "\n".join(f"- {item}" for item in testcase_focus))

    prompt_ready_context = "\n\n".join(section for section in prompt_sections if section).strip()
    return {
        "prompt_ready_context": prompt_ready_context,
        "ocr_sections": ocr_sections,
        "flowchart_sections": flowchart_sections,
        "testcase_focus": testcase_focus,
    }


def format_flowchart_context(index: int, flowchart: dict) -> str:
    """将单张流程图的节点/边/路径信息格式化为可读文本，供 prompt 使用。"""
    nodes = flowchart.get("nodes", [])
    edges = flowchart.get("edges", [])
    paths = flowchart.get("paths", [])
    node_labels = [describe_flowchart_node(node) for node in nodes]
    decision_lines = []

    adjacency = {}
    node_name_map = {node.get("id"): describe_flowchart_node(node) for node in nodes}
    for edge in edges:
        adjacency.setdefault(edge["source"], []).append(edge)

    for node in nodes:
        if node.get("kind") != "decision":
            continue
        next_steps = []
        for edge in adjacency.get(node["id"], []):
            target_name = node_name_map.get(edge["target"], edge["target"])
            edge_label = (edge.get("label") or "").strip()
            if edge_label:
                next_steps.append(f"{edge_label} -> {target_name}")
            else:
                next_steps.append(target_name)
        if next_steps:
            decision_lines.append(f"{node_name_map.get(node['id'], node['id'])}: " + " / ".join(next_steps))

    lines = [
        f"图片{index} 流程图：识别置信度 {flowchart.get('confidence', 0)}，节点 {flowchart.get('node_count', 0)} 个，连线 {flowchart.get('edge_count', 0)} 条。",
    ]
    if node_labels:
        lines.append("节点：" + " -> ".join(node_labels))
    if decision_lines:
        lines.append("判定节点：" + "；".join(decision_lines))
    if paths:
        for path_index, path in enumerate(paths[:8], start=1):
            normalized_path = [normalize_path_node_name(step, node_name_map) for step in path]
            lines.append(f"路径{path_index}：" + " -> ".join(normalized_path))
    return "\n".join(lines)


def derive_flowchart_case_hints(flowchart: dict) -> list[str]:
    """从流程图数据推导出测试设计重点（入口、判定节点分支、完整路径、结束节点）。"""
    nodes = flowchart.get("nodes", [])
    edges = flowchart.get("edges", [])
    paths = flowchart.get("paths", [])
    hints = []
    node_name_map = {node.get("id"): describe_flowchart_node(node) for node in nodes}
    adjacency = {}
    for edge in edges:
        adjacency.setdefault(edge["source"], []).append(edge)

    roots = flowchart.get("roots", [])
    if roots:
        root_names = [node_name_map.get(root, root) for root in roots]
        hints.append("覆盖每个流程入口：" + "、".join(root_names))

    for node in nodes:
        if node.get("kind") != "decision":
            continue
        options = []
        for edge in adjacency.get(node["id"], []):
            target_name = node_name_map.get(edge["target"], edge["target"])
            edge_label = (edge.get("label") or "").strip()
            if edge_label:
                options.append(f"{edge_label} -> {target_name}")
            else:
                options.append(target_name)
        if options:
            hints.append(f"覆盖判定节点“{node_name_map.get(node['id'], node['id'])}”的所有分支：" + "、".join(options))

    for path in paths[:8]:
        if len(path) >= 2:
            normalized_path = [normalize_path_node_name(step, node_name_map) for step in path]
            hints.append("覆盖完整业务路径：" + " -> ".join(normalized_path))

    end_nodes = [node for node in nodes if node.get("kind") == "end"]
    for node in end_nodes:
        hints.append(f"验证流程最终落点“{node.get('label') or node.get('id')}”的成功/完成结果是否符合预期")

    if not hints and nodes:
        hints.append("结合流程图节点顺序补充主流程、分支流和异常流测试")
    return hints


def describe_flowchart_node(node: dict) -> str:
    """返回节点的可读名称：优先用 label，否则根据 kind 映射为中文描述。"""
    label = (node.get("label") or "").strip()
    if label:
        return label

    kind = node.get("kind") or node.get("shape") or "node"
    kind_map = {
        "start": "开始节点",
        "end": "结束节点",
        "decision": "判定节点",
        "process": "流程节点",
        "terminator": "终止节点",
    }
    return f"{kind_map.get(kind, '未命名节点')}({node.get('id', 'unknown')})"


def normalize_path_node_name(name: str, node_name_map: dict[str, str]) -> str:
    """将流程路径中的节点 ID 替换为可读节点名。"""
    normalized = (name or "").strip()
    return node_name_map.get(normalized, normalized)


def collapse_text(text: str, max_chars: int = 500) -> str:
    """压缩文本空白并按最大字符数截断，用于 OCR/JSON 预览。"""
    compact = " ".join((text or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def dedupe_keep_order(items: list[str]) -> list[str]:
    """按原始顺序去重，保留第一次出现的非空字符串。"""
    seen = set()
    result = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


# ── OCR ────────────────────────────────────────────────────────────────────

@cache
def get_tesseract_langs() -> list[str]:
    """查询已安装的 Tesseract 语言包，结果全局缓存（无参数函数，@cache 语义最清晰）。"""
    tesseract_path = shutil.which("tesseract")
    if not tesseract_path:
        return []

    proc = subprocess.run(
        [tesseract_path, "--list-langs"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if line.strip() and not line.lower().startswith("list of available")
    ]


def pick_ocr_language() -> str | None:
    """按可用语言包选择 OCR 语言，优先中英混合，其次中文或英文。"""
    langs = set(get_tesseract_langs())
    if {"chi_sim", "eng"}.issubset(langs):
        return "chi_sim+eng"
    if "chi_sim" in langs:
        return "chi_sim"
    if "eng" in langs:
        return "eng"
    return None


def ocr_image(image_path: str) -> str:
    """调用 Tesseract 对单张图片做 OCR；未安装或无语言包时返回空字符串。"""
    tesseract_path = shutil.which("tesseract")
    language = pick_ocr_language()
    if not tesseract_path or not language:
        return ""

    proc = subprocess.run(
        [tesseract_path, image_path, "stdout", "-l", language],
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout or "").strip()


def has_structured_content(text: str) -> bool:
    """判断 OCR 文本是否包含结构化内容（表格、权限矩阵、按钮文案等）。"""
    if not text:
        return False

    # 中文关键词在原始文本中匹配
    cn_keywords = ["新增", "编辑", "删除", "查询", "权限", "角色", "按钮", "状态", "菜单", "审批", "开始", "结束", "是否", "流程", "登录", "注册"]
    # 英文关键词在小写文本中匹配，避免大小写干扰
    en_keywords = ["read", "write", "delete", "admin", "owner", "member", "start", "end", "flow", "approve"]

    table_like = text.count("|") >= 2 or text.count("\t") >= 2
    has_cn_keyword = any(kw in text for kw in cn_keywords)
    has_en_keyword = any(kw in text.lower() for kw in en_keywords)

    return table_like or has_cn_keyword or has_en_keyword


def ocr_images(image_paths: list[str]) -> dict[str, str]:
    """批量 OCR 图片，过滤不支持的格式，返回 {路径: OCR文本} 字典。"""
    image_ocr: dict[str, str] = {}
    for image_path in image_paths:
        if Path(image_path).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        text = ocr_image(image_path)
        if text:
            image_ocr[image_path] = text
    return image_ocr


# ── HTML 解析 ───────────────────────────────────────────────────────────────

def extract_html_images(
    soup: BeautifulSoup,
    resource_dir: Path,
    base_url: str | None = None,
    local_dir: Path | None = None,
) -> list[str]:
    """提取 HTML 中的图片，支持 data URI、远程图片和本地相对路径图片。"""
    image_paths: list[str] = []

    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue

        try:
            if src.startswith("data:image/"):
                header, encoded = src.split(",", 1)
                media_type = header.split(";")[0]
                ext = guess_extension(None, media_type.replace("data:", "", 1), ".png")
                image_bytes = base64.b64decode(encoded)
                if len(image_bytes) > MAX_DOWNLOAD_BYTES:
                    continue
                saved = save_bytes(resource_dir, image_bytes, ext, "html_image")
                image_paths.append(str(saved))
                continue

            if base_url:
                resource = urljoin(base_url, src)
                image_bytes = _stream_download(resource, timeout=20)
                ext = guess_extension(resource, None, ".png")
                saved = save_bytes(resource_dir, image_bytes, ext, Path(urlparse(resource).path).stem or "html_image")
                image_paths.append(str(saved))
                continue

            if local_dir:
                candidate = (local_dir / unquote(src)).resolve()
                # 路径穿越校验：仅允许 local_dir 范围内的文件
                try:
                    candidate.relative_to(local_dir.resolve())
                except ValueError:
                    continue
                if candidate.exists() and candidate.is_file():
                    ext = candidate.suffix.lower() or ".png"
                    saved = save_bytes(resource_dir, candidate.read_bytes(), ext, candidate.stem or "html_image")
                    image_paths.append(str(saved))
        except Exception:
            continue

    return image_paths


def parse_html_content(
    html_content: str,
    source: str,
    source_type: str,
    document_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
    base_url: str | None = None,
    local_dir: Path | None = None,
) -> dict:
    """解析 HTML 内容：提取图片、去除脚本样式标签、抽取正文并补充 OCR。"""
    soup = BeautifulSoup(html_content, "html.parser")
    image_paths = extract_html_images(soup, resource_dir=resource_dir, base_url=base_url, local_dir=local_dir)

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    image_ocr = ocr_images(image_paths)
    return build_result(source, source_type, document_type, case_title, str(resource_dir), text, image_paths, image_ocr, downloaded_file)


# ── PDF 解析 ────────────────────────────────────────────────────────────────

def parse_pdf(
    file_path: Path,
    source: str,
    source_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
) -> dict:
    """解析 PDF：抽取每页正文和内嵌图片，随后对图片做 OCR/流程图分析。"""
    if not fitz:
        raise ParserError("PyMuPDF 未安装，无法解析 PDF。")

    text_parts: list[str] = []
    image_paths: list[str] = []
    document = fitz.open(file_path)

    try:
        for page in document:
            page_text = page.get_text().strip()
            if page_text:
                text_parts.append(page_text)

            for image_info in page.get_images(full=True):
                base_image = document.extract_image(image_info[0])
                ext = f".{base_image.get('ext', 'png').lower()}"
                saved = save_bytes(
                    resource_dir,
                    base_image["image"],
                    ext,
                    f"{file_path.stem}_page_{page.number + 1}",
                )
                image_paths.append(str(saved))
    finally:
        document.close()

    image_ocr = ocr_images(image_paths)
    return build_result(
        source,
        source_type,
        ".pdf",
        case_title,
        str(resource_dir),
        "\n\n".join(text_parts),
        image_paths,
        image_ocr,
        downloaded_file,
    )


# ── DOCX 解析 ───────────────────────────────────────────────────────────────

def _iter_docx_block_items(document):
    """
    按文档原始顺序迭代段落和表格，保持 body 中的真实顺序。
    返回 ('paragraph', paragraph) 或 ('table', table) 的元组。
    """
    from docx.oxml.ns import qn as _qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body:
        if child.tag == _qn("w:p"):
            yield "paragraph", Paragraph(child, document)
        elif child.tag == _qn("w:tbl"):
            yield "table", Table(child, document)


def _dedup_row_cells(row):
    """去除合并单元格导致的重复 cell，保留唯一 cell（按 XML element 去重）。"""
    seen = set()
    result = []
    for cell in row.cells:
        cell_id = id(cell._tc)
        if cell_id not in seen:
            seen.add(cell_id)
            result.append(cell)
    return result


def parse_docx(
    file_path: Path,
    source: str,
    source_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
) -> dict:
    """解析 DOCX：按原文顺序提取段落/表格文本，并导出内嵌图片用于 OCR。"""
    if not docx:
        raise ParserError("python-docx 未安装，无法解析 DOCX。")

    document = docx.Document(file_path)
    text_parts: list[str] = []

    # 按原始顺序遍历段落和表格，避免所有段落在前、所有表格在后的问题
    for block_type, block in _iter_docx_block_items(document):
        if block_type == "paragraph":
            if block.text.strip():
                text_parts.append(block.text.strip())
        else:  # table
            for row in block.rows:
                cells = _dedup_row_cells(row)
                row_values = [c.text.strip() for c in cells if c.text.strip()]
                if row_values:
                    text_parts.append(" | ".join(row_values))

    image_paths: list[str] = []
    for rel in document.part.rels.values():
        if "image" not in rel.target_ref:
            continue
        ext = Path(rel.target_ref).suffix.lower() or ".png"
        saved = save_bytes(resource_dir, rel.target_part.blob, ext, file_path.stem or "docx_image")
        image_paths.append(str(saved))

    image_ocr = ocr_images(image_paths)
    return build_result(
        source,
        source_type,
        ".docx",
        case_title,
        str(resource_dir),
        "\n".join(text_parts),
        image_paths,
        image_ocr,
        downloaded_file,
    )


# ── DOC 转换 ────────────────────────────────────────────────────────────────

def convert_doc_to_docx(file_path: Path, temp_dir: Path) -> Path:
    """调用系统 textutil 将 .doc 文件转换为 .docx，转换失败时抛出 ParserError。"""
    output_path = temp_dir / f"{file_path.stem}.docx"
    proc = subprocess.run(
        [
            "/usr/bin/textutil",
            "-convert",
            "docx",
            "-output",
            str(output_path),
            str(file_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not output_path.exists():
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise ParserError(f".doc 转换失败：{stderr or 'textutil 未生成 docx 文件'}")
    return output_path


def parse_doc(
    file_path: Path,
    source: str,
    source_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
) -> dict:
    """解析 .doc：先转换为临时 .docx，再复用 DOCX 解析流程。"""
    with TemporaryDirectory() as temp_dir:
        converted = convert_doc_to_docx(file_path, Path(temp_dir))
        result = parse_docx(converted, source, source_type, case_title, resource_dir, downloaded_file)
        result["document_type"] = ".doc"
        return result


# ── 纯文本 / HTML 文件解析 ─────────────────────────────────────────────────

def parse_image_file(
    file_path: Path,
    source: str,
    source_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
) -> dict:
    """解析单张图片：复制到资源目录、执行 OCR，并作为正文参与 LLM 上下文构建。"""
    saved_image = save_bytes(
        resource_dir,
        file_path.read_bytes(),
        file_path.suffix.lower() or ".png",
        file_path.stem or "image",
    )
    image_path = str(saved_image)
    image_ocr = ocr_images([image_path])
    text = "\n".join(value for value in image_ocr.values() if value)
    return build_result(source, source_type, file_path.suffix.lower(), case_title, str(resource_dir), text, [image_path], image_ocr, downloaded_file)


def parse_text(
    file_path: Path,
    source: str,
    source_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
) -> dict:
    """解析纯文本/Markdown 文件，优先按 UTF-8 读取，编码异常时忽略错误字符。"""
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    return build_result(source, source_type, file_path.suffix.lower(), case_title, str(resource_dir), text, [], {}, downloaded_file)


def parse_html_file(
    file_path: Path,
    source: str,
    source_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
    base_url: str | None = None,
) -> dict:
    """读取 HTML 文件，传入 local_dir 用于解析同目录下的相对路径图片。"""
    html_content = file_path.read_text(encoding="utf-8", errors="ignore")
    return parse_html_content(
        html_content,
        source=source,
        source_type=source_type,
        document_type=file_path.suffix.lower() or ".html",
        case_title=case_title,
        resource_dir=resource_dir,
        downloaded_file=downloaded_file,
        base_url=base_url,
        local_dir=file_path.parent,
    )


# ── 分发 ────────────────────────────────────────────────────────────────────

def dispatch_local_parser(
    file_path: Path,
    source: str,
    source_type: str,
    case_title: str,
    resource_dir: Path,
    downloaded_file: str | None = None,
    base_url: str | None = None,
) -> dict:
    """根据本地文件扩展名分发到对应解析器。"""
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(file_path, source, source_type, case_title, resource_dir, downloaded_file)
    if ext == ".docx":
        return parse_docx(file_path, source, source_type, case_title, resource_dir, downloaded_file)
    if ext == ".doc":
        return parse_doc(file_path, source, source_type, case_title, resource_dir, downloaded_file)
    if ext in IMAGE_EXTENSIONS:
        return parse_image_file(file_path, source, source_type, case_title, resource_dir, downloaded_file)
    if ext in TEXT_EXTENSIONS:
        return parse_text(file_path, source, source_type, case_title, resource_dir, downloaded_file)
    if ext in HTML_EXTENSIONS:
        return parse_html_file(file_path, source, source_type, case_title, resource_dir, downloaded_file, base_url=base_url)
    raise ParserError(f"暂不支持的文件格式: {ext or '未知'}")


def infer_remote_extension(url: str, content_type: str | None) -> str:
    """优先从 URL 路径推断文件扩展名，其次根据 Content-Type 映射，均失败时默认 .html。"""
    path = urlparse(url).path
    suffix = Path(unquote(path)).suffix.lower()
    if suffix in {".pdf", ".doc", ".docx", ".html", ".htm", ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        return suffix

    if content_type:
        content_type = content_type.split(";")[0].strip().lower()
        mapping = {
            "text/html": ".html",
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "text/plain": ".txt",
            "text/markdown": ".md",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/gif": ".gif",
            "image/tiff": ".tif",
        }
        if content_type in mapping:
            return mapping[content_type]

    return ".html"


def _extract_html_title(html_content: str) -> str | None:
    """从 HTML 中提取 <title> 标签文本，用作用例标题候选。"""
    soup = BeautifulSoup(html_content, "html.parser")
    title = (soup.title.string if soup.title and soup.title.string else "") or ""
    return title.strip() or None


def _derive_title_from_url(url: str) -> str:
    """从 URL 路径 stem 推导用例标题；路径为空时回退为“测试用例”。"""
    stem = Path(unquote(urlparse(url).path)).stem
    return sanitize_case_title(stem, "测试用例")


def download_remote_source(url: str) -> tuple[Path, str, Path]:
    """下载远程资源并存入统一资源目录，返回文件路径、用例标题和资源目录。"""
    _validate_url(url)
    try:
        with requests.get(url, headers=REQUEST_HEADERS, timeout=30, stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type")
            suffix = infer_remote_extension(url, content_type)
            filename = sanitize_filename(
                Path(unquote(urlparse(url).path)).stem or "downloaded_source",
                "downloaded_source",
            )
            # 流式读取，控制大小
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ParserError(
                            f"远程文档超过大小限制 {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB，已中止下载。"
                        )
                    chunks.append(chunk)
            payload = b"".join(chunks)
            case_title = _derive_title_from_url(url)
            if suffix in HTML_EXTENSIONS:
                html_content = payload.decode("utf-8", errors="ignore")
                case_title = sanitize_case_title(_extract_html_title(html_content) or case_title, case_title)
            resource_dir = create_resource_dir(case_title)
            return save_bytes(resource_dir, payload, suffix, filename), case_title, resource_dir
    except ParserError:
        raise
    except requests.RequestException as exc:
        raise ParserError(f"URL 无法访问: {exc}") from exc


def parse_url(url: str) -> dict:
    """解析 URL：先下载到资源目录，再按下载文件类型分发解析。"""
    downloaded_file, case_title, resource_dir = download_remote_source(url)
    return dispatch_local_parser(
        downloaded_file,
        source=url,
        source_type="url",
        case_title=case_title,
        resource_dir=resource_dir,
        downloaded_file=str(downloaded_file),
        base_url=url if downloaded_file.suffix.lower() in HTML_EXTENSIONS else None,
    )


def parse_local_path(input_source: str) -> dict:
    """解析本地路径：校验文件存在后创建资源目录，并按扩展名分发解析。"""
    file_path = Path(input_source).expanduser().resolve()
    if not file_path.exists():
        raise ParserError(f"本地文件不存在: {input_source}")
    case_title = sanitize_case_title(file_path.stem, "测试用例")
    resource_dir = create_resource_dir(case_title)
    return dispatch_local_parser(
        file_path,
        source=str(file_path),
        source_type="local",
        case_title=case_title,
        resource_dir=resource_dir,
    )


def main() -> None:
    """命令行入口：解析 URL 或本地路径，并以 JSON 输出解析结果或错误信息。"""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python parser.py <url_or_file_path>"}, ensure_ascii=False))
        sys.exit(1)

    input_source = sys.argv[1]
    try:
        if input_source.startswith(("http://", "https://")):
            result = parse_url(input_source)
        else:
            result = parse_local_path(input_source)
        print(json.dumps(result, ensure_ascii=False))
    except ParserError as exc:
        print(json.dumps({"error": str(exc), "source": input_source}, ensure_ascii=False))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"error": f"解析失败: {exc}", "source": input_source}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
