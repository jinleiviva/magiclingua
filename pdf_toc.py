#!/usr/bin/env python3
"""
PDF 文章目录提取 —— 为「勾选文章 → 只翻译选中项」提供目录数据。

设计原则：普适、简单。不针对任何特定杂志调参。

## 提取目录的三层逻辑（通用，与具体杂志无关）

1. **先看真书签**：`doc.get_toc()`。部分 PDF 真带 /Outlines，直接用，最准。
   但实测：网络流出的杂志 PDF 绝大多数是打印版，书签为 0 条。

2. **没有书签 → 走字号法**：正文 9.0pt，标题一律 ≥ 1.8 倍（24pt 之类）。
   这是跨杂志都成立的版面信号，不依赖任何版式特征。

3. **手工兜底**：自动识别失败时绝不阻塞，UI 仍可输入 `1,3-5` 页码范围。

## 已知边界（所有杂志通用，非缺陷而是本质限制）

  * 同一页有两篇文章时（杂志常见），页级粒度分不开，选任一篇都拿整页。
  * 图表页 / 广告页会被当成「文章」列出，用户不勾即可。
  * 极少数 PDF 用自定义编码字体，标题会是乱码 → 标 low_confidence。

用法：
    from pdf_toc import extract_toc
    r = extract_toc("/path/to/magazine.pdf")
    for a in r["articles"]:
        print(a["start"], a["end"], a["title"])
"""

from __future__ import annotations

import collections
import re

import pymupdf

# 字号达到正文字号的这个倍数，才认为该页有标题
TITLE_RATIO = 1.8

# 识别正文字号时，超过这个占比的页面（无文字）会被忽略
_WORD_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")
_ALLOWED_RE = re.compile(r"[^\sA-Za-z0-9\u4e00-\u9fff.,;:!?%&'\"()\-\u2019\u201c\u201d/]")


def _detect_body_size(pages_spans):
    """正文字号 = 按字符数加权的众数。

    直接取 span 众数会被页脚小字污染（Barron's 页脚 5.5pt 极多），
    按字符数加权后稳定为 9.0。
    """
    counter = collections.Counter()
    for spans in pages_spans:
        for size, txt, _bb in spans:
            counter[size] += len(txt)
    if not counter:
        return 10.0
    return counter.most_common(1)[0][0]


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def _is_garbage(text):
    if not text:
        return True
    bad = len(_ALLOWED_RE.findall(text))
    return bad / max(len(text), 1) > 0.25


def _page_spans(page):
    out = []
    try:
        data = page.get_text("dict")
    except Exception:
        return out
    for b in data.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for sp in line.get("spans", []):
                txt = sp.get("text", "").strip()
                if len(txt) >= 2:
                    bb = sp.get("bbox")
                    out.append((round(sp.get("size", 0.0), 1), txt, tuple(bb) if bb else ()))
    return out


def _masthead_lines(page_texts, sample=6):
    """找出每页都重复的页眉/页脚行（刊名 + 日期），供摘要过滤。

    通用做法：不去猜页眉长什么样，只统计出现频率——真页眉必然在
    绝大多数页出现。跨语言、跨版式都成立。
    """
    freq = collections.Counter()
    for t in page_texts:
        lines = [re.sub(r"\s+", " ", l).strip() for l in t.split("\n")]
        lines = [l for l in lines if l][:sample]
        for l in set(lines):
            freq[l] += 1
    thresh = max(3, int(len(page_texts) * 0.3))
    return {l for l, c in freq.items() if c >= thresh}


# 注：这里曾经有一个 _snippet_of()（取开头 230 字做摘要），已移除。
# 实际效果不好：过滤短行会把正文首行也滤掉，摘要常从半句话开始
# （"could further maul Britain's..."），还有 PDF 换行丢空格
# （"theirhighest"）、与标题重复等问题。判定为无用，删掉比修到勉强能用更划算。


def _join_spans(sel, size):
    """把同字号 span 拼成标题，按 x 间距决定补空格。

    为什么要自己拼而不是 page.get_text(clip=并集bbox)：
    同页两篇文章字号相同，并集 bbox 横跨整页，clip 会把正文也捞进来。
    为什么不简单 " ".join：标题常被切成细 span，词间距 > 字间距，
    按 x 间隙补空格才得到 "Takeaways From Retail's Big Week"。
    """
    items = sorted(sel, key=lambda x: (x[2][1], x[2][0]))
    lines, cur, cur_y = [], [], None
    for s, t, bb in items:
        if not bb:
            continue
        if cur_y is None:
            cur_y = bb[1]
        if abs(bb[1] - cur_y) > size * 0.8:
            lines.append(cur)
            cur, cur_y = [], bb[1]
        cur.append(bb + (t,))
    if cur:
        lines.append(cur)

    out = []
    for ln in lines:
        ln = sorted(ln, key=lambda x: x[0])
        buf = ln[0][4]
        for i in range(1, len(ln)):
            gap = ln[i][0] - ln[i - 1][2]
            buf += (" " if gap > size * 0.15 else "") + ln[i][4]
        out.append(buf)
    return " ".join(out)


def _title_of_page(spans, body_size):
    """取一页标题 = 最大字号且成句的那组文本。

    逐档下探，跳过首字下沉：杂志正文首字常是超大单字母（"H" / "V"），
    成句字符数 ≤3 的档位视为首字下沉，跳到下一档。
    """
    if not spans:
        return None, 0.0
    sizes = sorted({s for s, _, _ in spans}, reverse=True)
    for mx in sizes:
        if mx < body_size * TITLE_RATIO:
            return None, 0.0
        sel = [(s, t, bb) for s, t, bb in spans if abs(s - mx) <= 0.5 and len(bb) == 4]
        if not sel:
            continue
        text = _clean(_join_spans(sel, mx))[:120]
        if len(text) <= 3 or not _WORD_RE.search(text) or _is_garbage(text):
            continue  # 首字下沉 / 单字 / 乱码 → 看下一档
        return text, mx
    return None, 0.0


def extract_toc(pdf_path, progress_cb=None):
    """提取文章目录。

    返回 dict：
        page_count, body_font_size
        articles   [{index, title, start, end, pages, font_ratio,
                    chars, flags, confidence}]
        source     "bookmarks" | "typography"   —— 用了哪条提取路径
        confidence high / medium / low
        warnings  [str]

    start / end 是 1-based 闭区间，可直接喂 babeldoc --pages。
    """
    doc = pymupdf.open(pdf_path)
    page_count = doc.page_count

    # 每页纯文本 + 页眉词表：摘要的来源（摘要才是用户判断"讲什么"的依据）
    page_texts = []
    for i in range(page_count):
        try:
            page_texts.append(doc[i].get_text("text"))
        except Exception:
            page_texts.append("")
    mast = _masthead_lines(page_texts)

    # ---- 第 1 层：真书签 ----
    toc = doc.get_toc()
    rows = []
    for it in toc:
        # get_toc() 返回 [层级, 标题, 页码] 三元组，不是二元组。
        # 曾经按二元组解包，导致凡是带书签的 PDF 一律崩溃——修在这里。
        if len(it) >= 3:
            lvl, title, pnum = it[0], it[1], it[2]
        elif len(it) == 2:
            lvl, title, pnum = 1, it[0], it[1]
        else:
            continue
        try:
            lvl, pnum = int(lvl), int(pnum)
        except (TypeError, ValueError):
            continue
        if 1 <= pnum <= page_count and title:
            rows.append((lvl, title.strip(), pnum))

    # 剔除自动生成的"水书签"：转曲 / 扫描转出来的 PDF 会给每页挂一条
    # "xxx_页面_003" 之类的书签，看着有目录，实际每页一条、毫无信息量。
    # 这类直接判为不可用，让它落到字号法去。
    junk = sum(1 for _, t, _ in rows
               if re.search(r"_(页面|page)_?\d+$", t, re.I)
               or re.fullmatch(r"pages?\s*\d+", t, re.I))
    if rows and junk / len(rows) >= 0.4:
        rows = []

    if rows:
        rows.sort(key=lambda x: x[2])
        # 只留一二级标题：296 页技术手册有 963 条书签，全摊开会变成
        # 216 项的清单，反而没法选。
        top = [r for r in rows if r[0] <= 2]
        rows = top if len(top) >= 3 else rows

        articles = []
        for i, (_lvl, title, pnum) in enumerate(rows):
            end = rows[i + 1][2] - 1 if i + 1 < len(rows) else page_count
            if end >= pnum:
                articles.append({"title": title, "start": pnum, "end": end})
        if len(articles) >= 3:
            for idx, a in enumerate(articles):
                a["index"] = idx
                a["pages"] = a["end"] - a["start"] + 1
                a["font_ratio"] = 0.0
                a["chars"] = 0
                a["flags"] = []
                a["confidence"] = "high"
            return {
                "page_count": page_count,
                "body_font_size": 0.0,
                "articles": articles,
                "source": "bookmarks",
                "confidence": "high",
                "warnings": [],
            }

    # ---- 第 2 层：字号法 ----
    pages_spans = []
    for i in range(page_count):
        pages_spans.append(_page_spans(doc[i]))
        if progress_cb and i % 10 == 0:
            progress_cb(f"解析版面 {i}/{page_count}")

    body_size = _detect_body_size(pages_spans)
    heads = []
    for i in range(page_count):
        spans = pages_spans[i]
        title, size = _title_of_page(spans, body_size)
        has_body = bool(spans) and any(s >= body_size * 0.8 for s, _, _ in spans)
        heads.append({
            "title": title,
            "size": size,
            "has_body": has_body,
            "chars": sum(len(t) for s, t, _ in spans),
        })

    # 合并：无标题页归入上一篇（处理跨页续文）
    articles = []
    cur = None
    for i, h in enumerate(heads):
        if h["title"] and h["has_body"]:
            if cur:
                articles.append(cur)
            cur = {"title": h["title"], "start": i + 1, "end": i + 1,
                   "font_size": h["size"], "chars": h["chars"]}
        elif cur:
            cur["end"] = i + 1
            cur["chars"] += h["chars"]
    if cur:
        articles.append(cur)

    result = []
    for idx, a in enumerate(articles):
        n_pages = a["end"] - a["start"] + 1
        flags = []
        if a["chars"] < 200:
            flags.append("few_text")
        if a["chars"] / max(n_pages, 1) < 400:
            flags.append("short")
        ratio = round(a["font_size"] / body_size, 1) if body_size else 0
        if ratio > 6 and a["chars"] < 600:
            flags.append("likely_ad")

        conf = "high"
        if "likely_ad" in flags or "few_text" in flags:
            conf = "low"
        elif "short" in flags:
            conf = "medium"

        result.append({
            "index": idx,
            "title": a["title"],
            "start": a["start"],
            "end": a["end"],
            "pages": n_pages,
            "font_ratio": ratio,
            "chars": a["chars"],
            "flags": flags,
            "confidence": conf,
        })

    # ---- 第 3 层：等距分段兜底 ----
    # 有些 PDF 既没书签，标题也不比正文大多少（技术文档、习题集、纯文本
    # 排版），字号法会切出 0 篇。这时按固定页数分段，标题取该段首页里
    # 最像标题的一行，保证用户永远有得选，而不是面对一个空列表。
    source = "typography"
    if len(result) < 3 and page_count >= 4:
        chunk = 4 if page_count <= 120 else 8
        fallback = []
        start = 1
        while start <= page_count:
            end = min(start + chunk - 1, page_count)
            name = ""
            for raw in page_texts[start - 1].split("\n"):
                line = re.sub(r"\s+", " ", raw).strip()
                if 6 <= len(line) <= 70 and line not in mast:
                    name = line[:60]
                    break
            fallback.append({
                "index": len(fallback),
                "title": name or f"第 {start}-{end} 页",
                "start": start,
                "end": end,
                "pages": end - start + 1,
                "font_ratio": 0.0,
                "chars": sum(len(page_texts[p - 1]) for p in range(start, end + 1)),
                "flags": ["chunked"],
                "confidence": "low",
            })
            start = end + 1
        result, source = fallback, "chunks"

    warnings = []
    good = [a for a in result if a["confidence"] == "high"]
    if source == "chunks":
        warnings.append("未能识别文章结构，已按固定页数分段；需要精确范围请直接填页码")
        confidence = "low"
    elif len(good) / max(len(result), 1) > 0.6:
        confidence = "high"
    else:
        confidence = "medium"

    covered = sum(a["pages"] for a in result)
    if covered < page_count * 0.9:
        warnings.append(f"有 {page_count - covered} 页未归入任何文章（多为广告/空白页）")

    doc.close()
    return {
        "page_count": page_count,
        "body_font_size": body_size,
        "articles": result,
        "source": source,
        "confidence": confidence,
        "warnings": warnings,
    }


def selection_to_pages(articles, selected_indices):
    """把用户勾选的文章下标转成 babeldoc --pages 页码串。

    例：选中 [3,7] -> "12-14,25"。相邻区间自动合并。
    """
    ranges = sorted(
        (a["start"], a["end"]) for i, a in enumerate(articles) if i in selected_indices
    )
    if not ranges:
        return ""
    merged = [list(ranges[0])]
    for s, e in ranges[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return ",".join(f"{s}-{e}" if e > s else str(s) for s, e in merged)


if __name__ == "__main__":
    import json
    import sys
    import time

    path = sys.argv[1]
    t0 = time.time()
    r = extract_toc(path)
    print(f"\n{r['page_count']} 页 · 来源 {r['source']} · 正文字号 {r['body_font_size']} · "
          f"切出 {len(r['articles'])} 篇 · 可信度 {r['confidence']} · {time.time()-t0:.2f}s")
    for w in r["warnings"]:
        print(f"  ⚠ {w}")
    for a in r["articles"]:
        span = f"{a['start']}-{a['end']}" if a["end"] > a["start"] else str(a["start"])
        mark = "" if a["confidence"] == "high" else f"  <{','.join(a['flags']) or 'medium'}>"
        print(f"\n  p{span:>7} ({a['pages']:>2}p) {a['title'][:58]}{mark}")
    print("-" * 70)
    print("全选页码串:", selection_to_pages(r["articles"], range(len(r["articles"]))))
