"""CJK 生成空格实测：判断 RENDER_CJK_SPACE_REMOVAL 能否安全启用。

背景：仅凭"汉字 + 空格 + 汉字"的字符形态无法区分生成空格与表格列边界。
真实 PDF 中同一形态大量出现于跨行字符流与表格单元格交界，直接删除会合并跨列文本。
因此本脚本同时统计字符级与几何级两类判据的差异，用于评估误删率。

用法：
    python scripts/benchmark_cjk_spaces.py <pdf 或目录> ...

输出：
- 字符级判据（当前 is_generated_cjk_space）会删除多少处
- 几何分类：跨行/负间隙/微间隙/大间隙
- 误删率估计：真生成空格仅取"微间隙"一类
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import pymupdf

from app.render.text import is_cjk_char, is_generated_cjk_space

# 微间隙阈值：同基线下间隙小于字号该比例时才视为生成空格
MICRO_GAP_RATIO = 0.12
# 判断同行/同基线的纵向容差（pt）
BASELINE_TOLERANCE = 2.0


def collect(targets: list[str]) -> list[Path]:
    paths: list[Path] = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            paths.append(path)
    return paths


def scan_document(path: Path, max_pages: int) -> dict:
    """按字符流扫描，统计字符级与几何级判据。"""
    buckets: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[tuple[str, str, float, float]]] = collections.defaultdict(list)
    chars_total = 0
    try:
        document = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "buckets": buckets, "examples": examples, "chars": 0, "pages": 0}

    scanned = min(len(document), max_pages)
    for page_index in range(scanned):
        raw = document[page_index].get_text("rawdict")
        chars: list[tuple[str, pymupdf.Rect, float, float]] = []
        for block in raw["blocks"]:
            for line in block.get("lines", []):
                baseline = line["bbox"][1]
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        chars.append((char["c"], pymupdf.Rect(char["bbox"]), baseline, span["size"]))
        chars_total += len(chars)
        for index in range(1, len(chars) - 1):
            left, space, right = chars[index - 1], chars[index], chars[index + 1]
            if space[0] != " " or not left[0].strip() or not right[0].strip():
                continue
            if not is_generated_cjk_space(left[0], space[0], right[0]):
                continue
            same_line = abs(left[2] - right[2]) < BASELINE_TOLERANCE and abs(space[2] - right[2]) < BASELINE_TOLERANCE
            gap = right[1].x0 - left[1].x1
            size = left[3] or 1.0
            ratio = gap / size
            if not same_line:
                category = "跨行/跨列"
            elif gap < 0:
                category = "负间隙"
            elif ratio < MICRO_GAP_RATIO:
                category = "微间隙(疑似生成空格)"
            elif ratio < 0.20:
                category = "中间隙(疑似列边界)"
            else:
                category = "大间隙(列边界/有意间距)"
            buckets[category] += 1
            if len(examples[category]) < 3:
                examples[category].append((left[0], right[0], gap, ratio))
    document.close()
    return {"buckets": buckets, "examples": examples, "chars": chars_total, "pages": scanned}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    paths = collect(argv)
    if not paths:
        print("未找到 PDF")
        return 1

    print(f"{'文档':46s} {'页':>4s} {'字符':>8s} {'跨行':>5s} {'负隙':>5s} {'微隙':>5s} {'中/大隙':>7s}")
    print("-" * 92)
    totals: collections.Counter[str] = collections.Counter()
    total_chars = 0
    all_examples: dict[str, list] = collections.defaultdict(list)
    for path in paths:
        stats = scan_document(path, max_pages=60)
        if "error" in stats:
            print(f"{path.name[:44]:46s} 打开失败: {stats['error'][:40]}")
            continue
        buckets = stats["buckets"]
        totals.update(buckets)
        total_chars += stats["chars"]
        for category, items in stats["examples"].items():
            all_examples[category].extend(items)
        pages = stats.get("pages") or 0
        print(
            f"{path.name[:44]:46s} {pages:4d} {stats['chars']:8d} {buckets['跨行/跨列']:5d} "
            f"{buckets['负间隙']:5d} {buckets['微间隙(疑似生成空格)']:5d} "
            f"{buckets['中间隙(疑似列边界)'] + buckets['大间隙(列边界/有意间距)']:7d}"
        )

    candidates = sum(totals.values())
    micro = totals["微间隙(疑似生成空格)"]
    print("-" * 92)
    print(f"语料规模: {total_chars} 字符；CJK␣CJK 候选 {candidates} 处")
    if candidates:
        print(f"字符级判据会删除: {candidates} 处")
        print(f"几何上像生成空格: {micro} 处")
        print(f"误删率估计: {(candidates - micro) / candidates * 100:.1f}%")
    print("\n各类样例:")
    for category in ("微间隙(疑似生成空格)", "中间隙(疑似列边界)", "大间隙(列边界/有意间距)", "跨行/跨列", "负间隙"):
        for left, right, gap, ratio in all_examples.get(category, [])[:2]:
            print(f"  {category:26s} {left}␣{right}  间隙={gap:8.2f}  比例={ratio:7.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))