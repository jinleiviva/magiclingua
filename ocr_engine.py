#!/usr/bin/env python3
"""
扫描件 OCR 引擎 —— 基于 macOS Vision 框架（VNRecognizeTextRequest）

为什么选 Apple Vision 而不是 Tesseract / PaddleOCR：

  * 系统内置，零模型下载、零额外二进制依赖（不用 brew install tesseract）
  * 跑在 Apple Neural Engine 上，M 系列芯片原生加速
  * 实测（M5 /《经济学人》扫描页，冷启动后）：
      accurate 无 correction  dpi200  0.72s/页   80 文本段
      fast    无 correction  dpi200  0.31s/页   44 文本段
    对比 Tesseract 5 同量级页面约 1.2-2.2s/页
  * 公开基准（macocr vs Tesseract 5）字符准确率 96.3% vs 95.4%

输出的是 sandwich PDF：原页渲染图作背景，OCR 文字以可见文字层铺在上面。
这里刻意让文字层「可见」——BabelDOC 会替换这层文字，若原文字是透明的
（render_mode=3），替换后的中文也会继承透明属性导致看不见。
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

# Vision 识别等级
VN_LEVEL_ACCURATE = 0
VN_LEVEL_FAST = 1


class VisionOCRUnavailable(Exception):
    pass


def _load_vision():
    """延迟导入，避免在 Linux / 无 pyobjc 环境下启动服务就崩。"""
    try:
        import Vision  # noqa: F401
        return True
    except ImportError as e:
        raise VisionOCRUnavailable(
            "Apple Vision 不可用。需要 macOS + pyobjc：\n"
            "  pip install pyobjc-framework-Vision pyobjc-framework-Cocoa"
        ) from e


def ocr_image(png_bytes, languages=("en-US",), fast=False):
    """
    对单张 PNG 做 OCR。

    返回 [(text, x, y, w, h), ...]，坐标是归一化的（0-1），原点在左下，
    与 Vision 的 boundingBox 一致。
    """
    _load_vision()
    import Vision

    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(png_bytes, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(VN_LEVEL_FAST if fast else VN_LEVEL_ACCURATE)
    request.setRecognitionLanguages_(list(languages))
    # 关掉语言纠错：实测能省掉一半以上时间，对印刷体几乎无质量损失
    request.setUsesLanguageCorrection_(False)

    ok, error = handler.performRequests_error_([request], None)
    if error:
        raise RuntimeError(f"Vision OCR 失败: {error}")

    results = []
    for observation in request.results():
        candidate = observation.topCandidates_(1)[0]
        box = observation.boundingBox()
        results.append((
            candidate.string(),
            box.origin.x,
            box.origin.y,
            box.size.width,
            box.size.height,
        ))
    return results


def needs_ocr(pdf_path, min_chars=50):
    """判断 PDF 是否缺少文字层（扫描件）。"""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    total = 0
    for i in range(min(3, doc.page_count)):
        total += len(doc[i].get_text().strip())
    doc.close()
    return total < min_chars


# 渲染像素硬上限（最长边）。大开本页面（如《经济学人》1367×1826pt）
# 若不设限，高 dpi 会渲染出 3800px 宽的图像，BabelDOC 后续直接 OOM。
MAX_RENDER_PIXELS = 3000

# 超采样倍率。扫描件原生分辨率通常只有 72-150dpi，
# 渲染到远高于原生的 dpi 只是插值放大，不增加可识别信息，白白吃内存。
SUPERSAMPLE = 1.25

MIN_DPI = 96


def _native_dpi(page):
    """探测页面内嵌图片的最高等效 dpi（即扫描件的真实分辨率）。"""
    best = 0.0
    try:
        for img in page.get_images(full=True):
            xref = img[0]
            info = page.parent.extract_image(xref)
            w = info.get("width") or 0
            if w and page.rect.width > 0:
                best = max(best, w * 72.0 / page.rect.width)
    except Exception:
        pass
    return best


def _effective_dpi(page, requested_dpi):
    """
    决定实际渲染 dpi：
      1. 不超过扫描件原生分辨率的 SUPERSAMPLE 倍（避免无意义插值）
      2. 不低于 MIN_DPI（保证小图也有基本可识别度）
      3. 渲染像素最长边不超过 MAX_RENDER_PIXELS（内存保护）
    """
    rect = page.rect
    longest_pt = max(rect.width, rect.height)
    if longest_pt <= 0:
        return requested_dpi

    native = _native_dpi(page)
    if native > 0:
        target = min(requested_dpi, native * SUPERSAMPLE)
    else:
        target = requested_dpi

    target = max(target, MIN_DPI)

    cap_dpi = MAX_RENDER_PIXELS * 72.0 / longest_pt
    return int(max(72, min(target, cap_dpi)))


def ocr_pdf_to_searchable(
    input_path,
    output_path,
    dpi=200,
    languages=("en-US",),
    fast=False,
    progress_cb=None,
):
    """
    把扫描件 PDF 转成「带文字层」的 PDF，供 BabelDOC 排版翻译使用。

    结构：背景是原页渲染图，上层是可见的 OCR 文字（按 bbox 定位）。
    字号由 bbox 高度反推，尽量贴近原视觉大小，方便 BabelDOC 判断标题/正文。
    """
    import pymupdf

    src = pymupdf.open(input_path)
    page_count = src.page_count
    out = pymupdf.open()
    total_chars = 0
    started = time.time()

    for page_index in range(src.page_count):
        page = src[page_index]
        rect = page.rect

        # dpi 由「扫描件原生分辨率」与内存上限共同决定
        use_dpi = _effective_dpi(page, dpi)

        if progress_cb:
            progress_cb(f"OCR 第 {page_index + 1}/{page_count} 页 ({use_dpi}dpi)")

        # 1) 渲染原页
        pix = page.get_pixmap(dpi=use_dpi)

        # OCR 用无损 PNG 保证识别率
        ocr_bytes = pix.tobytes("png")
        # 背景图用 JPEG —— 只是底图，无损 PNG 会让单页 PDF 涨到近 3MB
        bg_bytes = pix.tobytes("jpeg", jpg_quality=82)

        # 2) Vision OCR
        boxes = ocr_image(ocr_bytes, languages=languages, fast=fast)

        # 3) 新建同尺寸页面：先铺背景图
        new_page = out.new_page(width=rect.width, height=rect.height)
        new_page.insert_image(rect, stream=bg_bytes)

        # 4) 按 bbox 铺文字层
        #    Vision 坐标：归一化，原点左下；PDF 坐标：点，原点左上
        for text, nx, ny, nw, nh in boxes:
            if not text.strip():
                continue

            x0 = nx * rect.width
            top_pdf = (1.0 - ny - nh) * rect.height
            height = nh * rect.height

            # 由行高反推字号：行高约为字号的 1.2 倍
            font_size = max(4.0, height / 1.2)

            # 基线位置（PDF 文本从基线开始绘制）
            baseline = top_pdf + height * 0.8

            try:
                new_page.insert_text(
                    (x0, baseline),
                    text,
                    fontsize=font_size,
                    fontname="helv",
                    color=(0, 0, 0),
                    render_mode=0,  # 可见文字
                )
            except Exception as e:
                logger.debug(f"插入文字失败（跳过）: {e}")
                continue

            total_chars += len(text)

    out.save(output_path, garbage=4, deflate=True)
    out.close()
    src.close()

    elapsed = time.time() - started
    logger.info(
        f"OCR 完成: {os.path.basename(input_path)} -> {os.path.basename(output_path)} "
        f"| {page_count} 页 {total_chars} 字符 | {elapsed:.1f}s"
    )
    return {
        "output": output_path,
        "pages": page_count,
        "chars": total_chars,
        "elapsed": round(elapsed, 2),
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 3:
        print("用法: python ocr_engine.py <输入.pdf> <输出.pdf> [dpi]")
        sys.exit(1)

    src, dst = sys.argv[1], sys.argv[2]
    use_dpi = int(sys.argv[3]) if len(sys.argv) > 3 else 200

    info = ocr_pdf_to_searchable(src, dst, dpi=use_dpi, progress_cb=lambda m: print("  " + m))
    print(f"完成: {info}")
