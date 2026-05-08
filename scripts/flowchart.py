"""流程图检测与分析模块

使用 OCR（Tesseract）+ 几何视觉分析来识别流程图中的节点、连线和拓扑关系。
主要功能：
- 检测流程图的节点（矩形、菱形、椭圆等形状）
- 识别连线和箭头方向
- 提取节点标签和边标签
- 分析流程路径和拓扑结构
"""
import math
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF 库
import numpy as np

# ======================== 常量定义 ========================

# 支持的图像文件扩展名
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}

# 流程图关键词列表，用于检测和验证流程图置信度
FLOWCHART_KEYWORDS = (
    "开始",
    "结束",
    "完成",
    "成功",
    "失败",
    "注册",
    "登录",
    "审批",
    "流程",
    "节点",
    "是否",
    "start",
    "end",
    "success",
    "approve",
    "review",
)

# 边标签候选词，用于识别连线标签（如是/否、成功/失败）
EDGE_LABEL_CANDIDATES = {"y", "n", "yes", "no", "是", "否", "true", "false"}


def analyze_flowchart_image(image_path: str, preferred_lang: str | None = None) -> dict:
    """分析流程图图像，检测节点、连线和拓扑关系。
    
    流程：
    1. 加载并缩放图像
    2. 提取前景和形状掩码
    3. 运行 OCR 获取文本和文本框
    4. 检测节点形状和连线
    5. 构建拓扑关系（根、叶节点、路径）
    6. 计算流程图置信度
    
    Args:
        image_path: 图像文件路径
        preferred_lang: OCR 首选语言（如 'chi_sim+eng'）
    
    Returns:
        dict: 包含以下字段的分析结果
            - is_flowchart: 是否检测为流程图
            - confidence: 置信度 [0-1]
            - nodes: 节点列表
            - edges: 连线列表
            - roots/sinks: 入口/出口节点
            - paths: 检测到的路径
            - summary: 路径摘要
    """
    path = Path(image_path)
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return _empty_result("unsupported_image_type")

    image = _load_image_rgb(path)
    if image is None:
        return _empty_result("image_load_failed")

    scaled, scale_ratio = _resize_nearest(image, max_dim=720)
    foreground = _foreground_mask(scaled)
    foreground = _binary_close(foreground, iterations=1)
    shape_mask = _binary_close(_color_shape_mask(scaled), iterations=1)
    tokens = _ocr_tsv(path, preferred_lang)
    scaled_tokens = _scale_boxes(tokens, scale_ratio)
    lines = _group_tokens_to_lines(scaled_tokens)

    node_components = _detect_shape_components(shape_mask, foreground)
    nodes = _build_nodes(node_components, lines, foreground.shape)
    _populate_node_labels_from_crops(nodes, image, scale_ratio, preferred_lang)
    line_mask = _remove_node_regions(foreground, nodes)
    line_components = _connected_components(line_mask, min_area=6)
    edges = _reconstruct_edges(nodes, line_components, line_mask)
    _attach_edge_labels(edges, lines, nodes)

    node_ids_in_edges = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    filtered_nodes = []
    for node in nodes:
        if node["id"] in node_ids_in_edges or node["label"] or node["shape"] in {"decision", "terminator"}:
            filtered_nodes.append(node)
    nodes = filtered_nodes
    roots, sinks = _compute_roots_and_sinks(nodes, edges)
    paths = _enumerate_paths(nodes, edges, roots, sinks)
    confidence = _compute_confidence(nodes, edges, lines)
    is_flowchart = confidence >= 0.45 and len(nodes) >= 3 and len(edges) >= 2

    return {
        "is_flowchart": is_flowchart,
        "confidence": round(confidence, 3),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
        "roots": roots,
        "sinks": sinks,
        "paths": paths,
        "summary": _summarize_paths(paths),
        "detection_mode": "ocr+geometry",
    }


# ======================== 结果构建 ========================

def _empty_result(reason: str) -> dict:
    """返回空结果，表示无法分析或不是流程图。"""
    return {
        "is_flowchart": False,
        "confidence": 0.0,
        "node_count": 0,
        "edge_count": 0,
        "nodes": [],
        "edges": [],
        "roots": [],
        "sinks": [],
        "paths": [],
        "summary": "",
        "detection_mode": "ocr+geometry",
        "reason": reason,
    }


# ======================== 图像处理模块 ========================

def _load_image_rgb(path: Path) -> np.ndarray | None:
    """加载图像并转换为 RGB 色彩空间。
    
    支持多种图像格式和 PDF 第一页。"""
    try:
        pix = fitz.Pixmap(str(path))
        if pix.alpha:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        channels = min(pix.n, 3)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        return arr[:, :, :channels]
    except Exception:
        try:
            doc = fitz.open(str(path))
            page = doc[0]
            pix = page.get_pixmap(alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            return arr[:, :, :3]
        except Exception:
            return None


def _resize_nearest(image: np.ndarray, max_dim: int = 720) -> tuple[np.ndarray, float]:
    """最近邻插值缩放图像到指定最大尺寸。
    
    Args:
        image: 输入图像
        max_dim: 最大边长（像素）
    
    Returns:
        (缩放后图像, 缩放比率)
    """
    height, width = image.shape[:2]
    current_max = max(height, width)
    if current_max <= max_dim:
        return image, 1.0

    scale = current_max / max_dim
    new_height = max(1, int(round(height / scale)))
    new_width = max(1, int(round(width / scale)))
    ys = np.linspace(0, height - 1, new_height).astype(int)
    xs = np.linspace(0, width - 1, new_width).astype(int)
    return image[ys][:, xs], scale


def _foreground_mask(image: np.ndarray) -> np.ndarray:
    """提取前景掩码（排除白色背景）。
    
    检测：非白色像素、有色像素、淡色线条"""
    gray = image.mean(axis=2)
    rgb_range = image.max(axis=2) - image.min(axis=2)
    not_white = gray < 246
    colored = rgb_range > 8
    faded_line = (gray < 252) & (gray > 190)
    return (not_white | colored | faded_line).astype(bool)


def _color_shape_mask(image: np.ndarray) -> np.ndarray:
    """提取有色形状掩码（用于节点检测）。
    
    优先检测饱和度高的形状（如填充矩形、菱形）。"""
    gray = image.mean(axis=2)
    rgb_range = image.max(axis=2) - image.min(axis=2)
    saturation_like = rgb_range > 28
    return (gray < 245) & saturation_like


def _binary_close(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """二值形态学闭运算（先膨胀后腐蚀）。
    
    用于填充小孔洞和平滑边界。"""
    result = mask.copy()
    for _ in range(iterations):
        result = _binary_dilate(result)
    for _ in range(iterations):
        result = _binary_erode(result)
    return result


def _binary_dilate(mask: np.ndarray) -> np.ndarray:
    """二值膨胀操作（3x3 邻域）。
    
    扩展前景区域。"""
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    windows = []
    for dy in range(3):
        for dx in range(3):
            windows.append(padded[dy:dy + mask.shape[0], dx:dx + mask.shape[1]])
    return np.logical_or.reduce(windows)


def _binary_erode(mask: np.ndarray) -> np.ndarray:
    """二值腐蚀操作（3x3 邻域）。
    
    收缩前景区域。"""
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    windows = []
    for dy in range(3):
        for dx in range(3):
            windows.append(padded[dy:dy + mask.shape[0], dx:dx + mask.shape[1]])
    return np.logical_and.reduce(windows)


# ======================== OCR 模块 ========================

def _ocr_tsv(image_path: Path, preferred_lang: str | None) -> list[dict]:
    """使用 Tesseract 运行 OCR，输出 TSV 格式结果。
    
    返回每个文字的位置、置信度等信息。"""
    tesseract_path = shutil.which("tesseract")
    if not tesseract_path:
        return []

    command = [tesseract_path, str(image_path), "stdout"]
    if preferred_lang:
        command.extend(["-l", preferred_lang])
    command.extend(["--psm", "11", "tsv"])
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    lines = proc.stdout.splitlines()
    if len(lines) < 2:
        return []

    header = lines[0].split("\t")
    results = []
    for row in lines[1:]:
        parts = row.split("\t")
        if len(parts) != len(header):
            continue
        item = dict(zip(header, parts))
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(item.get("conf", "-1"))
            left = int(item.get("left", "0"))
            top = int(item.get("top", "0"))
            width = int(item.get("width", "0"))
            height = int(item.get("height", "0"))
        except ValueError:
            continue
        if width <= 0 or height <= 0:
            continue
        results.append(
            {
                "text": text,
                "conf": conf,
                "bbox": [left, top, left + width, top + height],
                "block_num": item.get("block_num", "0"),
                "par_num": item.get("par_num", "0"),
                "line_num": item.get("line_num", "0"),
            }
        )
    return results


def _scale_boxes(tokens: list[dict], scale_ratio: float) -> list[dict]:
    """根据图像缩放比例调整 OCR 文本框位置。"""
    if scale_ratio == 1.0:
        return tokens

    scaled = []
    for token in tokens:
        left, top, right, bottom = token["bbox"]
        new_token = token.copy()
        new_token["bbox"] = [
            int(round(left / scale_ratio)),
            int(round(top / scale_ratio)),
            int(round(right / scale_ratio)),
            int(round(bottom / scale_ratio)),
        ]
        scaled.append(new_token)
    return scaled


def _group_tokens_to_lines(tokens: list[dict]) -> list[dict]:
    """将 OCR 文本分组成新的文本行。
    
    遇到同一行的文本，合并为一行，并整合位置和置信度。"""
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for token in tokens:
        key = (token["block_num"], token["par_num"], token["line_num"])
        grouped[key].append(token)

    lines = []
    for items in grouped.values():
        ordered = sorted(items, key=lambda item: (item["bbox"][1], item["bbox"][0]))
        text = _clean_ocr_text(" ".join(item["text"] for item in ordered))
        xs1 = [item["bbox"][0] for item in ordered]
        ys1 = [item["bbox"][1] for item in ordered]
        xs2 = [item["bbox"][2] for item in ordered]
        ys2 = [item["bbox"][3] for item in ordered]
        confs = [item["conf"] for item in ordered if item["conf"] >= 0]
        lines.append(
            {
                "text": text,
                "conf": round(sum(confs) / len(confs), 2) if confs else -1.0,
                "bbox": [min(xs1), min(ys1), max(xs2), max(ys2)],
            }
        )
    return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))


# ======================== 连通分量算法 ========================

def _connected_components(mask: np.ndarray, min_area: int = 1) -> list[dict]:
    """使用深度优先搜索检测连通区域。
    
    Args:
        mask: 二值掩码
        min_area: 最小子区域面积
    
    Returns:
        list: 每个区域的信息（像素、边界框、面积等）
    """
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    foreground_points = np.argwhere(mask)
    components = []

    for start_y, start_x in foreground_points:
        if visited[start_y, start_x]:
            continue

        stack = [(int(start_y), int(start_x))]
        visited[start_y, start_x] = True
        pixels = []
        min_y = max_y = int(start_y)
        min_x = max_x = int(start_x)

        while stack:
            y, x = stack.pop()
            pixels.append((y, x))
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x

            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1), (y - 1, x - 1), (y - 1, x + 1), (y + 1, x - 1), (y + 1, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))

        if len(pixels) < min_area:
            continue

        pixel_array = np.array(pixels, dtype=np.int32)
        bbox = [min_x, min_y, max_x + 1, max_y + 1]
        box_width = bbox[2] - bbox[0]
        box_height = bbox[3] - bbox[1]
        components.append(
            {
                "pixels": pixel_array,
                "bbox": bbox,
                "area": len(pixels),
                "width": box_width,
                "height": box_height,
                "fill_ratio": len(pixels) / max(1, box_width * box_height),
            }
        )
    return components


# ======================== 节点检测与分类 ========================

def _detect_shape_components(primary_mask: np.ndarray, fallback_mask: np.ndarray) -> list[dict]:
    """检测节点形状的连通区域。

    优先使用主掩码，备选使用兜底前景掩码。"""
    image_area = primary_mask.shape[0] * primary_mask.shape[1]
    components = _connected_components(primary_mask, min_area=max(40, image_area // 2200))
    if not components:
        components = _connected_components(fallback_mask, min_area=max(40, image_area // 2200))
    results = []
    for comp in components:
        if _looks_like_node_component(comp, image_area):
            shape = _classify_shape(comp)
            results.append({**comp, "shape": shape})
    return results


def _looks_like_node_component(component: dict, image_area: int) -> bool:
    """判断区域是否可能是流程图节点。
    
    检查尺寸、面积比、填充比是否符合节点规格。"""
    width = component["width"]
    height = component["height"]
    fill_ratio = component["fill_ratio"]
    area = component["area"]

    if width < 18 or height < 12:
        return False
    if min(width, height) < 10:
        return False
    if area < max(40, image_area // 1800):
        return False
    if width > height * 10 or height > width * 10:
        return False
    if fill_ratio < 0.16:
        return False
    return True


def _classify_shape(component: dict) -> str:
    """分类节点形状（菱形/decision、圆角矩形/terminator、普通矩形/process）。

    根据角点填充率、宽高比来作出判定。"""
    width = component["width"]
    height = component["height"]
    fill_ratio = component["fill_ratio"]
    x1, y1, x2, y2 = component["bbox"]
    mask = np.zeros((height, width), dtype=bool)
    local_pixels = component["pixels"] - np.array([y1, x1], dtype=np.int32)
    mask[local_pixels[:, 0], local_pixels[:, 1]] = True

    corner_size_y = max(1, height // 5)
    corner_size_x = max(1, width // 5)
    corners = [
        mask[:corner_size_y, :corner_size_x].mean(),
        mask[:corner_size_y, -corner_size_x:].mean(),
        mask[-corner_size_y:, :corner_size_x].mean(),
        mask[-corner_size_y:, -corner_size_x:].mean(),
    ]
    corner_mean = sum(corners) / len(corners)
    ratio = width / max(1, height)

    if 0.75 <= ratio <= 1.75 and fill_ratio <= 0.68 and corner_mean < fill_ratio * 0.72:
        return "decision"
    if ratio >= 2.0 and corner_mean < fill_ratio * 0.95:
        return "terminator"
    return "process"


def _build_nodes(shape_components: list[dict], lines: list[dict], image_shape: tuple[int, int]) -> list[dict]:
    """构建节点对象、分配 ID、关联文本标签。

    过程：
    1. 根据形状区域创建初始节点
    2. 将 OCR 文本行分配到最近节点
    3. 为未分配文本行创建合成节点
    4. 合并重叠节点并重新分配 ID
    """
    nodes = []
    for index, component in enumerate(shape_components, start=1):
        node = {
            "id": f"node_{index}",
            "bbox": component["bbox"],
            "shape": component["shape"],
            "label": "",
            "kind": component["shape"],
            "line_indices": [],
        }
        nodes.append(node)

    for line_index, line in enumerate(lines):
        if not line["text"]:
            continue
        target = _find_best_node_for_line(nodes, line["bbox"])
        if target is not None:
            target["line_indices"].append(line_index)

    for node in nodes:
        if node["line_indices"]:
            texts = [lines[i]["text"] for i in node["line_indices"]]
            node["label"] = " ".join(dict.fromkeys(texts))
        node["kind"] = _classify_node_kind(node["label"], node["shape"])

    nodes = _add_synthetic_text_nodes(nodes, lines, image_shape)
    return _merge_overlapping_nodes(nodes)


def _populate_node_labels_from_crops(
    nodes: list[dict],
    image: np.ndarray,
    scale_ratio: float,
    preferred_lang: str | None,
) -> None:
    """对每个节点裁切原始图像区域并重新 OCR，以获取更准确的节点标签。"""
    for node in nodes:
        x1, y1, x2, y2 = node["bbox"]
        crop = _crop_original_image(image, [x1, y1, x2, y2], scale_ratio, pad=10)
        crop_text = _ocr_array(crop, preferred_lang)
        if crop_text:
            node["label"] = crop_text
            node["kind"] = _classify_node_kind(crop_text, node["shape"])


def _crop_original_image(image: np.ndarray, scaled_bbox: list[int], scale_ratio: float, pad: int = 0) -> np.ndarray:
    """从原始图像中裁切子区域。
    
    根据缩放比例再裁切。"""
    x1, y1, x2, y2 = scaled_bbox
    ox1 = max(0, int(round(x1 * scale_ratio)) - pad)
    oy1 = max(0, int(round(y1 * scale_ratio)) - pad)
    ox2 = min(image.shape[1], int(round(x2 * scale_ratio)) + pad)
    oy2 = min(image.shape[0], int(round(y2 * scale_ratio)) + pad)
    return image[oy1:oy2, ox1:ox2, :3].copy()


def _ocr_array(image: np.ndarray, preferred_lang: str | None) -> str:
    """对 numpy 数组进行 OCR，返回最佳识别结果。
    
    尝试原始和反色两种方案，返回较好的结果。"""
    if image.size == 0:
        return ""
    tesseract_path = shutil.which("tesseract")
    if not tesseract_path:
        return ""

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            candidates = []
            candidates.append(_run_tesseract_on_array(image, temp_dir, tesseract_path, preferred_lang, "crop"))
            inverted = 255 - image
            candidates.append(_run_tesseract_on_array(inverted, temp_dir, tesseract_path, preferred_lang, "crop_invert"))
            cleaned = [item for item in candidates if item]
            if not cleaned:
                return ""
            return max(cleaned, key=lambda text: (len(text.replace(" ", "")), text.count(" ")))
    except Exception:
        return ""


def _run_tesseract_on_array(
    image: np.ndarray,
    temp_dir: str,
    tesseract_path: str,
    preferred_lang: str | None,
    stem: str,
) -> str:
    """将数组写入临时图片并调用 Tesseract OCR。"""
    temp_path = Path(temp_dir) / f"{stem}.png"
    pix = fitz.Pixmap(fitz.csRGB, image.shape[1], image.shape[0], image.tobytes(), False)
    pix.save(str(temp_path))
    command = [tesseract_path, str(temp_path), "stdout"]
    if preferred_lang:
        command.extend(["-l", preferred_lang])
    command.extend(["--psm", "6"])
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    return _clean_ocr_text(proc.stdout or "")


def _find_best_node_for_line(nodes: list[dict], bbox: list[int]) -> dict | None:
    """查找与文本行最接近的节点。
    
    计算中心点在节点内的距离，返回距离最近的节点。"""
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    best_node = None
    best_score = -10**9

    for node in nodes:
        x1, y1, x2, y2 = node["bbox"]
        if x1 - 12 <= center_x <= x2 + 12 and y1 - 12 <= center_y <= y2 + 12:
            pad_score = min(center_x - x1, x2 - center_x, center_y - y1, y2 - center_y)
            if pad_score > best_score:
                best_score = pad_score
                best_node = node
    return best_node


def _classify_node_kind(label: str, shape: str) -> str:
    """分类节点类型（开始、结束、决策、处理）。
    
    根据标签内容和形状来判定节点类型。"""
    normalized = "" .join(char for char in label.lower() if char.isalnum())
    if any(keyword in label for keyword in ("开始", "启动", "入口")) or normalized in {"start", "begin"}:
        return "start"
    if any(keyword in label for keyword in ("结束", "完成", "成功", "失败")) or normalized in {"end", "finish", "success"} or "success" in normalized:
        return "end"
    if label.startswith("是否") or "?" in label or "？" in label or shape == "decision":
        return "decision"
    return "process"


def _clean_ocr_text(text: str) -> str:
    """清理 OCR 识别结果（去除干扰符号、规范化空格）。"""
    compact = " ".join((text or "").split())
    compact = compact.replace("|", " ")
    compact = compact.strip(":;[](){}<>-_ ")
    return " ".join(compact.split())


def _add_synthetic_text_nodes(nodes: list[dict], lines: list[dict], image_shape: tuple[int, int]) -> list[dict]:
    """基于未分配的文本行创建合成节点。
    
    为没有关联节点的文本创建新节点（如边标签）。"""
    existing_line_indices = {idx for node in nodes for idx in node["line_indices"]}
    height, width = image_shape
    next_index = len(nodes) + 1

    for line_index, line in enumerate(lines):
        text = line["text"].strip()
        normalized = text.lower()
        if line_index in existing_line_indices:
            continue
        if normalized in EDGE_LABEL_CANDIDATES:
            continue
        if len(text) <= 1:
            continue
        if line["conf"] >= 0 and line["conf"] < 60:
            continue

        x1, y1, x2, y2 = line["bbox"]
        line_width = x2 - x1
        line_height = y2 - y1
        if line_width > width * 0.32 or line_height > height * 0.12:
            continue
        pad_x = max(10, int((x2 - x1) * 0.45))
        pad_y = max(8, int((y2 - y1) * 0.8))
        bbox = [
            max(0, x1 - pad_x),
            max(0, y1 - pad_y),
            min(width, x2 + pad_x),
            min(height, y2 + pad_y),
        ]
        shape = "decision" if text.startswith("是否") else "process"
        nodes.append(
            {
                "id": f"node_{next_index}",
                "bbox": bbox,
                "shape": shape,
                "label": text,
                "kind": _classify_node_kind(text, shape),
                "line_indices": [line_index],
            }
        )
        next_index += 1
    return nodes


def _merge_overlapping_nodes(nodes: list[dict]) -> list[dict]:
    """合并重叠的节点，避免重复检测。
    
    基于 IoU 和包含关系来判定节点是否重叠。"""
    merged = []
    for node in sorted(nodes, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        target = None
        for existing in merged:
            if _bbox_iou(node["bbox"], existing["bbox"]) >= 0.45 or _bbox_contains(existing["bbox"], node["bbox"]) or _bbox_contains(node["bbox"], existing["bbox"]):
                target = existing
                break
        if target is None:
            merged.append(node)
            continue

        target["bbox"] = _union_bbox(target["bbox"], node["bbox"])
        target["shape"] = "decision" if "decision" in {target["shape"], node["shape"]} else target["shape"]
        label_parts = [part for part in [target["label"], node["label"]] if part]
        target["label"] = " ".join(dict.fromkeys(label_parts))
        target["kind"] = _classify_node_kind(target["label"], target["shape"])
        target["line_indices"] = sorted(set(target["line_indices"]) | set(node["line_indices"]))

    for index, node in enumerate(merged, start=1):
        node["id"] = f"node_{index}"
    return merged


def _remove_node_regions(mask: np.ndarray, nodes: list[dict]) -> np.ndarray:
    """从前景掩码中移除节点区域，保留连线。"""
    result = mask.copy()
    for node in nodes:
        x1, y1, x2, y2 = node["bbox"]
        result[max(0, y1 - 2):min(mask.shape[0], y2 + 2), max(0, x1 - 2):min(mask.shape[1], x2 + 2)] = False
    return result


# ======================== 连线检测模块 ========================

def _reconstruct_edges(nodes: list[dict], line_components: list[dict], line_mask: np.ndarray) -> list[dict]:
    """从连线像素重建连线，连接节点并推断方向。

    处理单条线和分支线，自动推断箭头方向。
    """
    edges = []
    if len(nodes) < 2:
        return edges

    node_map = {node["id"]: node for node in nodes}
    for component in line_components:
        touched = _find_component_touches(nodes, component)
        if len(touched) < 2:
            continue

        if len(touched) == 2:
            source_id, target_id, confidence, method = _orient_pair(
                node_map[touched[0]["node_id"]],
                node_map[touched[1]["node_id"]],
                touched[0],
                touched[1],
                line_mask,
            )
            edges.append(_build_edge(source_id, target_id, confidence, method))
            continue

        branch_edges = _expand_branch_component(touched, node_map, line_mask)
        edges.extend(branch_edges)

    return _dedup_edges(edges)


def _find_component_touches(nodes: list[dict], component: dict) -> list[dict]:
    """找到连通区域与节点的接触点。
    
    检测区域像素在节点框附近的位置。"""
    touches = []
    xs = component["pixels"][:, 1]
    ys = component["pixels"][:, 0]

    for node in nodes:
        x1, y1, x2, y2 = node["bbox"]
        expanded = [x1 - 14, y1 - 14, x2 + 14, y2 + 14]
        inside = (xs >= expanded[0]) & (xs <= expanded[2]) & (ys >= expanded[1]) & (ys <= expanded[3])
        if not inside.any():
            continue

        near_pixels = component["pixels"][inside]
        side = _closest_side(node["bbox"], near_pixels)
        contact = _mean_contact_point(near_pixels)
        touches.append({"node_id": node["id"], "side": side, "contact": contact})
    return touches


def _closest_side(bbox: list[int], pixels: np.ndarray) -> str:
    """检测像素群最接近节点的一侧（上、下、左、右）。"""
    x1, y1, x2, y2 = bbox
    min_distances = {
        "top": float(np.min(np.abs(pixels[:, 0] - y1))),
        "bottom": float(np.min(np.abs(pixels[:, 0] - y2))),
        "left": float(np.min(np.abs(pixels[:, 1] - x1))),
        "right": float(np.min(np.abs(pixels[:, 1] - x2))),
    }
    return min(min_distances, key=min_distances.get)


def _mean_contact_point(pixels: np.ndarray) -> list[int]:
    """计算接触像素群的中心点。"""
    mean_y = int(round(float(np.mean(pixels[:, 0]))))
    mean_x = int(round(float(np.mean(pixels[:, 1]))))
    return [mean_x, mean_y]


def _expand_branch_component(touches: list[dict], node_map: dict[str, dict], line_mask: np.ndarray) -> list[dict]:
    """展开分支线条，连接最上方节点到各下方节点。
    
    没有明确方向时推断拓扑关系。"""
    touched_nodes = [node_map[item["node_id"]] for item in touches]
    y_values = [(_center(node["bbox"])[1], node["id"]) for node in touched_nodes]
    y_values.sort()
    top_node = node_map[y_values[0][1]]
    bottom_node = node_map[y_values[-1][1]]

    if bottom_node["kind"] == "end" and (_center(bottom_node["bbox"])[1] - _center(top_node["bbox"])[1]) > 30:
        sink_id = bottom_node["id"]
        return [
            _build_edge(
                source_id=node["id"],
                target_id=sink_id,
                confidence=0.68,
                direction_method="layout_sink_inference",
            )
            for node in touched_nodes
            if node["id"] != sink_id
        ]

    if top_node["kind"] in {"start", "decision"} or (_center(bottom_node["bbox"])[1] - _center(top_node["bbox"])[1]) > 30:
        source_id = top_node["id"]
        return [
            _build_edge(
                source_id=source_id,
                target_id=node["id"],
                confidence=0.68,
                direction_method="layout_branch_inference",
            )
            for node in touched_nodes
            if node["id"] != source_id
        ]

    dominant_horizontal = (
        max(_center(node["bbox"])[0] for node in touched_nodes)
        - min(_center(node["bbox"])[0] for node in touched_nodes)
    ) > (
        max(_center(node["bbox"])[1] for node in touched_nodes)
        - min(_center(node["bbox"])[1] for node in touched_nodes)
    )
    ordered = sorted(
        touched_nodes,
        key=lambda node: _center(node["bbox"])[0] if dominant_horizontal else _center(node["bbox"])[1],
    )
    edges = []
    for left, right in zip(ordered, ordered[1:]):
        source_id, target_id, confidence, method = _orient_pair(
            left,
            right,
            next(item for item in touches if item["node_id"] == left["id"]),
            next(item for item in touches if item["node_id"] == right["id"]),
            line_mask,
        )
        edges.append(_build_edge(source_id, target_id, confidence, method))
    return edges


def _orient_pair(node_a: dict, node_b: dict, touch_a: dict, touch_b: dict, line_mask: np.ndarray) -> tuple[str, str, float, str]:
    """推断两个节点之间的方向，推断箭头方向。
    
    根据：箭头几何、节点类型、位置优先级。"""
    score_a = _arrow_score(node_a["bbox"], touch_a["side"], line_mask)
    score_b = _arrow_score(node_b["bbox"], touch_b["side"], line_mask)
    if abs(score_a - score_b) >= 2.0:
        if score_b > score_a:
            return node_a["id"], node_b["id"], 0.82, "arrow_geometry"
        return node_b["id"], node_a["id"], 0.82, "arrow_geometry"

    center_a = _center(node_a["bbox"])
    center_b = _center(node_b["bbox"])
    if node_a["kind"] == "start" and node_b["kind"] != "start":
        return node_a["id"], node_b["id"], 0.72, "node_kind_inference"
    if node_b["kind"] == "start" and node_a["kind"] != "start":
        return node_b["id"], node_a["id"], 0.72, "node_kind_inference"
    if node_b["kind"] == "end" and node_a["kind"] != "end":
        return node_a["id"], node_b["id"], 0.72, "node_kind_inference"
    if node_a["kind"] == "end" and node_b["kind"] != "end":
        return node_b["id"], node_a["id"], 0.72, "node_kind_inference"
    if node_a["kind"] == "decision" and node_b["kind"] != "decision":
        return node_a["id"], node_b["id"], 0.69, "decision_layout_inference"
    if node_b["kind"] == "decision" and node_a["kind"] != "decision":
        return node_b["id"], node_a["id"], 0.69, "decision_layout_inference"

    dy = center_b[1] - center_a[1]
    dx = center_b[0] - center_a[0]
    if abs(dy) >= abs(dx):
        if dy >= 0:
            return node_a["id"], node_b["id"], 0.62, "vertical_layout_inference"
        return node_b["id"], node_a["id"], 0.62, "vertical_layout_inference"
    if dx >= 0:
        return node_a["id"], node_b["id"], 0.58, "horizontal_layout_inference"
    return node_b["id"], node_a["id"], 0.58, "horizontal_layout_inference"


def _arrow_score(bbox: list[int], side: str, line_mask: np.ndarray) -> float:
    """检测箭头方向推断：流线是否从节点指向出口。
    
    根据节点附近线条强度变化判断。"""
    x1, y1, x2, y2 = bbox
    height, width = line_mask.shape
    if side == "top":
        anchor = max(0, y1 - 1)
        x_start = max(0, x1 - 16)
        x_end = min(width, x2 + 16)
        near = line_mask[max(0, anchor - 2):anchor + 1, x_start:x_end]
        far = line_mask[max(0, anchor - 10):max(0, anchor - 4), x_start:x_end]
    elif side == "bottom":
        anchor = min(height - 1, y2 + 1)
        x_start = max(0, x1 - 16)
        x_end = min(width, x2 + 16)
        near = line_mask[anchor:min(height, anchor + 3), x_start:x_end]
        far = line_mask[min(height, anchor + 4):min(height, anchor + 10), x_start:x_end]
    elif side == "left":
        anchor = max(0, x1 - 1)
        y_start = max(0, y1 - 16)
        y_end = min(height, y2 + 16)
        near = line_mask[y_start:y_end, max(0, anchor - 2):anchor + 1]
        far = line_mask[y_start:y_end, max(0, anchor - 10):max(0, anchor - 4)]
    else:
        anchor = min(width - 1, x2 + 1)
        y_start = max(0, y1 - 16)
        y_end = min(height, y2 + 16)
        near = line_mask[y_start:y_end, anchor:min(width, anchor + 3)]
        far = line_mask[y_start:y_end, min(width, anchor + 4):min(width, anchor + 10)]

    near_width = _dominant_span(near)
    far_width = _dominant_span(far)
    return float(far_width - near_width)


def _dominant_span(mask: np.ndarray) -> int:
    """计算掩码在主轴方向上的最大跨度。"""
    if mask.size == 0:
        return 0
    if mask.shape[0] >= mask.shape[1]:
        values = mask.sum(axis=0)
    else:
        values = mask.sum(axis=1)
    return int(values.max()) if values.size else 0


def _build_edge(source_id: str, target_id: str, confidence: float, direction_method: str) -> dict:
    """构建边结构体。"""
    return {
        "source": source_id,
        "target": target_id,
        "label": "",
        "direction_confidence": round(confidence, 3),
        "direction_method": direction_method,
    }


def _dedup_edges(edges: list[dict]) -> list[dict]:
    """删除重复边，保留方向置信度最高的版本。"""
    best_by_pair = {}
    for edge in edges:
        if edge["source"] == edge["target"]:
            continue
        key = (edge["source"], edge["target"])
        if key not in best_by_pair or edge["direction_confidence"] > best_by_pair[key]["direction_confidence"]:
            best_by_pair[key] = edge
    return list(best_by_pair.values())


def _attach_edge_labels(edges: list[dict], lines: list[dict], nodes: list[dict]) -> None:
    """附加边标签（如"是"、"否"）到连线。
    
    将浮在节点间的标签分配给最近的连线。"""
    if not edges:
        return

    node_boxes = [node["bbox"] for node in nodes]
    for line in lines:
        text = line["text"].strip()
        normalized = text.lower()
        if normalized not in EDGE_LABEL_CANDIDATES:
            continue
        if any(_bbox_iou(line["bbox"], bbox) > 0.1 for bbox in node_boxes):
            continue

        label_center = _center(line["bbox"])
        best_edge = None
        best_distance = math.inf
        for edge in edges:
            source_node = next(node for node in nodes if node["id"] == edge["source"])
            target_node = next(node for node in nodes if node["id"] == edge["target"])
            midpoint = [
                (_center(source_node["bbox"])[0] + _center(target_node["bbox"])[0]) / 2,
                (_center(source_node["bbox"])[1] + _center(target_node["bbox"])[1]) / 2,
            ]
            distance = math.dist(label_center, midpoint)
            if distance < best_distance:
                best_distance = distance
                best_edge = edge
        if best_edge is not None and best_distance < 90:
            best_edge["label"] = text


# ======================== 拓扑分析模块 ========================

def _compute_roots_and_sinks(nodes: list[dict], edges: list[dict]) -> tuple[list[str], list[str]]:
    """计算入度为0的节点（入口）和出度为0的节点（出口）。
    
    Returns:
        (入口节点 ID 列表, 出口节点 ID 列表)
    """
    indegree = {node["id"]: 0 for node in nodes}
    outdegree = {node["id"]: 0 for node in nodes}
    for edge in edges:
        indegree[edge["target"]] = indegree.get(edge["target"], 0) + 1
        outdegree[edge["source"]] = outdegree.get(edge["source"], 0) + 1
    roots = [node_id for node_id, degree in indegree.items() if degree == 0]
    sinks = [node_id for node_id, degree in outdegree.items() if degree == 0]
    return roots, sinks


def _enumerate_paths(nodes: list[dict], edges: list[dict], roots: list[str], sinks: list[str]) -> list[list[str]]:
    """从入口节点枚举所有路径到出口节点。
    
    使用深度优先搜索，限制最多 20 条路径，避免无限循环。"""
    adjacency = defaultdict(list)
    labels = {node["id"]: node["label"] or node["id"] for node in nodes}
    for edge in edges:
        adjacency[edge["source"]].append(edge["target"])

    results = []
    seen = set()

    def dfs(node_id: str, path: list[str]) -> None:
        if len(results) >= 20:
            return
        label = labels.get(node_id, node_id)
        path = path + [label]
        if node_id in sinks or not adjacency[node_id]:
            signature = tuple(path)
            if signature not in seen:
                seen.add(signature)
                results.append(path)
            return
        for target in adjacency[node_id]:
            if labels.get(target, target) in path:
                continue
            dfs(target, path)

    for root in roots[:8]:
        dfs(root, [])
    return results


def _summarize_paths(paths: list[list[str]]) -> str:
    """将路径列表格式化为字符串摘要。
    
    最多显示 8 条路径。"""
    if not paths:
        return ""
    return "\n".join(" -> ".join(path) for path in paths[:8])


def _compute_confidence(nodes: list[dict], edges: list[dict], lines: list[dict]) -> float:
    """计算流程图检测置信度 [0-1]。
    
    综合评分：节点数、连线数、关键词、决策和端点出现情况。"""
    if not nodes:
        return 0.0

    node_score = min(1.0, len(nodes) / 6)
    edge_score = min(1.0, len(edges) / max(1, len(nodes) - 1))
    keyword_hits = 0
    for line in lines:
        lower = line["text"].lower()
        if any(keyword in lower for keyword in FLOWCHART_KEYWORDS):
            keyword_hits += 1
    keyword_score = min(1.0, keyword_hits / 3)
    decision_bonus = 0.15 if any(node["kind"] == "decision" for node in nodes) else 0.0
    endpoint_bonus = 0.15 if any(node["kind"] == "start" for node in nodes) and any(node["kind"] == "end" for node in nodes) else 0.0
    return min(1.0, 0.35 * node_score + 0.25 * edge_score + 0.1 * keyword_score + decision_bonus + endpoint_bonus)


# ======================== 几何工具函数 ========================

def _bbox_iou(a: list[int], b: list[int]) -> float:
    """计算两个边界框的 IoU（Intersection over Union）。
    
    返回值 [0-1]，0 表示不相交，1 表示完全重合。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x1 >= inter_x2 or inter_y1 >= inter_y2:
        return 0.0
    intersection = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return intersection / float(area_a + area_b - intersection)


def _bbox_contains(outer: list[int], inner: list[int]) -> bool:
    """检查 outer 边界框是否完全包含 inner。"""
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _union_bbox(a: list[int], b: list[int]) -> list[int]:
    """计算两个边界框的并集。"""
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _center(bbox: list[int]) -> list[float]:
    """计算边界框的中心坐标。"""
    return [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]
