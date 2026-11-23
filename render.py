"""布吉岛查询结果的图片渲染。

模块化渲染：调用方传一组 block，渲染器按顺序排下来。
支持的 block：
  {"type": "hero",    "label": "总场次", "value": "57", "unit": "场"}
  {"type": "kpi",     "items": [{"label": "胜场", "value": "48"}, ...]}   # 一行三个
  {"type": "bars",    "items": [{"label": "胜率", "value": 85.7, "text": "85.7%"}]}
  {"type": "rows",    "items": [{"label": "K/D", "value": "0.72"}]}
  {"type": "table",   "columns": ["时间", "模式", "结果"], "rows": [["11-20", "起床", "胜"]]}
  {"type": "section", "text": "最近对局"}
  {"type": "note",    "text": "数据来源：BuGLand API"}
  {"type": "divider"}

对外：render_blocks() 主入口，render_report() 是兼容旧调用的薄封装。
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "wqy-microhei.ttc")

WIDTH = 900
PAD = 40
PAGE_BG = (255, 255, 255)
BG = (255, 255, 255)
BORDER = (229, 232, 237)
SOFT = (246, 247, 249)
TEXT = (23, 26, 31)
MUTED = (126, 132, 142)
ACCENT = (45, 106, 235)
LINE = (238, 240, 243)

_ALLOWED_SYMBOLS = set("·、，。！？：；（）《》「」【】—–-/|%.:#+*()[]{}<>\"' ")


def sanitize(text: object) -> str:
    """去掉字体里没有的字符，避免渲染出豆腐块。"""
    out = []
    for ch in str(text):
        o = ord(ch)
        if (
            0x20 <= o <= 0x7E
            or 0x4E00 <= o <= 0x9FFF
            or 0x3000 <= o <= 0x303F
            or 0xFF00 <= o <= 0xFFEF
            or ch in _ALLOWED_SYMBOLS
        ):
            out.append(ch)
    return "".join(out)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATH, size, index=0)


def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _ellipsis(
    draw: ImageDraw.ImageDraw, text: object, font: ImageFont.FreeTypeFont, max_w: int
) -> str:
    text = sanitize(text)
    if _text_w(draw, text, font) <= max_w:
        return text
    while text and _text_w(draw, text + "…", font) > max_w:
        text = text[:-1]
    return text + "…" if text else ""


F_TITLE = _font(30)
F_SUB = _font(17)
F_SECTION = _font(19)
F_KPI_LABEL = _font(16)
F_KPI_VALUE = _font(26)
F_ROW = _font(19)
F_HERO_VALUE = _font(44)
F_NOTE = _font(17)
F_TABLE = _font(18)
F_TABLE_HEAD = _font(16)

RADIUS = 8


def _card(d: ImageDraw.ImageDraw, box, radius: int = RADIUS, fill=None) -> None:
    d.rounded_rectangle(
        box,
        radius=radius,
        fill=fill if fill is not None else BG,
        outline=BORDER,
        width=1,
    )


def _block_height(block: dict, draw: ImageDraw.ImageDraw, inner_w: int) -> int:
    kind = str(block.get("type") or "").lower()
    if kind == "hero":
        return 116
    if kind == "kpi":
        items = block.get("items") or []
        rows = (len(items) + 2) // 3
        return rows * 94 + 10 if items else 0
    if kind == "bars":
        return len(block.get("items") or []) * 56 + 8
    if kind == "rows":
        items = block.get("items") or []
        return len(items) * 42 + 10 if items else 0
    if kind == "table":
        cols = block.get("columns") or []
        rows = block.get("rows") or []
        return 42 + len(rows) * 38 + 12 if cols else 0
    if kind == "section":
        return 52
    if kind == "note":
        return 40
    if kind == "divider":
        return 26
    return 0


def _draw_block(
    block: dict,
    d: ImageDraw.ImageDraw,
    probe: ImageDraw.ImageDraw,
    y: int,
    inner_w: int,
) -> int:
    kind = str(block.get("type") or "").lower()

    if kind == "hero":
        label = _ellipsis(probe, block.get("label") or "", F_KPI_LABEL, inner_w - 44)
        value = _ellipsis(probe, block.get("value") or "", F_HERO_VALUE, inner_w - 160)
        unit = _ellipsis(probe, block.get("unit") or "", F_ROW, 90)
        _card(d, [PAD, y, PAD + inner_w, y + 100])
        d.rectangle([PAD, y + 1, PAD + 3, y + 99], fill=ACCENT)
        d.text((PAD + 20, y + 16), label, font=F_KPI_LABEL, fill=MUTED)
        d.text((PAD + 20, y + 38), value, font=F_HERO_VALUE, fill=TEXT)
        if unit:
            vw = _text_w(probe, value, F_HERO_VALUE)
            d.text((PAD + 28 + vw, y + 58), unit, font=F_ROW, fill=MUTED)
        return y + 116

    if kind == "kpi":
        items = list(block.get("items") or [])
        cols = min(3, max(1, len(items)))
        gap = 16
        cell_w = (inner_w - gap * (cols - 1)) // cols
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            c, r = idx % cols, idx // cols
            x = PAD + c * (cell_w + gap)
            cy = y + r * 94
            _card(d, [x, cy, x + cell_w, cy + 80])
            d.text(
                (x + 16, cy + 13),
                _ellipsis(probe, item.get("label") or "", F_KPI_LABEL, cell_w - 32),
                font=F_KPI_LABEL,
                fill=MUTED,
            )
            d.text(
                (x + 16, cy + 36),
                _ellipsis(probe, item.get("value") or "", F_KPI_VALUE, cell_w - 32),
                font=F_KPI_VALUE,
                fill=TEXT,
            )
        rows_n = (len(items) + cols - 1) // cols if items else 0
        return y + rows_n * 94 + 10

    if kind == "bars":
        for item in block.get("items") or []:
            if not isinstance(item, dict):
                continue
            label = _ellipsis(probe, item.get("label") or "", F_ROW, inner_w - 180)
            raw = item.get("value")
            try:
                ratio = float(raw)
                if ratio > 1:
                    ratio = ratio / 100.0
            except Exception:
                ratio = 0.0
            ratio = max(0.0, min(1.0, ratio))
            text = item.get("text")
            if text in (None, ""):
                text = f"{ratio * 100:.1f}%"
            text = _ellipsis(probe, text, F_ROW, 140)
            d.text((PAD, y + 2), label, font=F_ROW, fill=TEXT)
            d.text(
                (PAD + inner_w - _text_w(probe, text, F_ROW), y + 2),
                text,
                font=F_ROW,
                fill=MUTED,
            )
            bar_y = y + 30
            d.rounded_rectangle([PAD, bar_y, PAD + inner_w, bar_y + 8], radius=4, fill=SOFT)
            fill_w = int(inner_w * ratio)
            if fill_w > 1:
                d.rounded_rectangle(
                    [PAD, bar_y, PAD + fill_w, bar_y + 8], radius=4, fill=ACCENT
                )
            y += 56
        return y + 8

    if kind == "rows":
        items = list(block.get("items") or [])
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                label, value = item.get("label"), item.get("value")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                label, value = item[0], item[1]
            else:
                continue
            d.text(
                (PAD, y + 6),
                _ellipsis(probe, label or "", F_ROW, inner_w // 2),
                font=F_ROW,
                fill=MUTED,
            )
            val = _ellipsis(probe, value if value is not None else "", F_ROW, inner_w // 2)
            d.text(
                (PAD + inner_w - _text_w(probe, val, F_ROW), y + 6),
                val,
                font=F_ROW,
                fill=TEXT,
            )
            y += 42
            if idx < len(items) - 1:
                d.line([PAD, y - 8, PAD + inner_w, y - 8], fill=LINE, width=1)
        return y + 10

    if kind == "table":
        cols = [sanitize(c) for c in (block.get("columns") or [])]
        rows = block.get("rows") or []
        if not cols:
            return y
        col_w = inner_w // len(cols)
        d.rectangle([PAD, y, PAD + inner_w, y + 38], fill=SOFT)
        d.line([PAD, y, PAD + inner_w, y], fill=BORDER, width=1)
        d.line([PAD, y + 38, PAD + inner_w, y + 38], fill=BORDER, width=1)
        for i, col in enumerate(cols):
            d.text(
                (PAD + i * col_w + 14, y + 10),
                _ellipsis(probe, col, F_TABLE_HEAD, col_w - 28),
                font=F_TABLE_HEAD,
                fill=MUTED,
            )
        y += 38
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            for i, cell in enumerate(row[: len(cols)]):
                d.text(
                    (PAD + i * col_w + 14, y + 9),
                    _ellipsis(probe, cell, F_TABLE, col_w - 28),
                    font=F_TABLE,
                    fill=TEXT if i == 0 else MUTED,
                )
            y += 38
            d.line([PAD, y, PAD + inner_w, y], fill=LINE, width=1)
        return y + 12

    if kind == "section":
        text = _ellipsis(probe, block.get("text") or "", F_SECTION, inner_w - 40)
        d.rectangle([PAD, y + 12, PAD + 3, y + 30], fill=ACCENT)
        d.text((PAD + 14, y + 10), text, font=F_SECTION, fill=TEXT)
        tw = _text_w(probe, text, F_SECTION)
        line_x = PAD + 14 + tw + 14
        if line_x < PAD + inner_w:
            d.line([line_x, y + 21, PAD + inner_w, y + 21], fill=LINE, width=1)
        return y + 52

    if kind == "note":
        d.text(
            (PAD, y + 8),
            _ellipsis(probe, block.get("text") or "", F_NOTE, inner_w),
            font=F_NOTE,
            fill=MUTED,
        )
        return y + 40

    if kind == "divider":
        d.line([PAD, y + 12, PAD + inner_w, y + 12], fill=BORDER, width=1)
        return y + 26

    return y


def render_blocks(
    title: str,
    blocks: Sequence[dict] = (),
    subtitle: str = "",
    footer: str = "",
) -> str:
    """按 block 列表画一张卡片图，返回文件路径。"""
    probe_img = Image.new("RGB", (WIDTH, 10))
    probe = ImageDraw.Draw(probe_img)

    inner_w = WIDTH - PAD * 2
    height = 40 + 46
    if subtitle:
        height += 26
    height += 34
    for block in blocks:
        if isinstance(block, dict):
            height += _block_height(block, probe, inner_w)
    if footer:
        height += 38
    height += 32
    height = max(height, 200)

    img = Image.new("RGB", (WIDTH, height), PAGE_BG)
    d = ImageDraw.Draw(img)

    y = 36
    d.rounded_rectangle([PAD, y + 1, PAD + 26, y + 27], radius=6, fill=ACCENT)
    d.line([PAD + 7, y + 12, PAD + 19, y + 12], fill=(255, 255, 255), width=2)
    d.line([PAD + 7, y + 17, PAD + 14, y + 17], fill=(205, 219, 255), width=2)
    d.text(
        (PAD + 38, y - 3),
        _ellipsis(probe, title, F_TITLE, inner_w - 38),
        font=F_TITLE,
        fill=TEXT,
    )
    y += 46
    if subtitle:
        d.text(
            (PAD + 38, y),
            _ellipsis(probe, subtitle, F_SUB, inner_w - 38),
            font=F_SUB,
            fill=MUTED,
        )
        y += 26
    y += 12
    d.line([0, y, WIDTH, y], fill=BORDER, width=1)
    y += 22

    for block in blocks:
        if isinstance(block, dict):
            y = _draw_block(block, d, probe, y, inner_w)

    if footer:
        fy = height - 32
        d.line([0, fy - 16, WIDTH, fy - 16], fill=BORDER, width=1)
        d.text(
            (PAD, fy - 2),
            _ellipsis(probe, footer, F_SUB, inner_w),
            font=F_SUB,
            fill=MUTED,
        )

    out_dir = os.path.join(tempfile.gettempdir(), "bugland_cards")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"card_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    return path


def render_report(
    title: str,
    subtitle: str = "",
    sections: Sequence[tuple] = (),
    footer: str = "",
) -> str:
    """兼容旧的 (类型, 数据) 写法。"""
    blocks: list[dict] = []
    for sec in sections:
        if not isinstance(sec, (list, tuple)) or not sec:
            continue
        kind, data = sec[0], (sec[1] if len(sec) > 1 else None)
        if kind == "bars":
            blocks.append(
                {
                    "type": "bars",
                    "items": [
                        {"label": a, "value": b, "text": c}
                        for a, b, c in (data or [])
                    ],
                }
            )
        elif kind == "rows":
            blocks.append(
                {
                    "type": "rows",
                    "items": [
                        {"label": a, "value": b} for a, b in (data or [])
                    ],
                }
            )
        elif kind == "kpi":
            blocks.append(
                {
                    "type": "kpi",
                    "items": [
                        {"label": a, "value": b} for a, b in (data or [])
                    ],
                }
            )
        elif kind == "note":
            blocks.append({"type": "note", "text": data})
    return render_blocks(title, blocks, subtitle, footer)


LABELS = {
    "win/game": "胜场 / 场次",
    "total_win/total_game": "总胜场 / 总场次",
    "lose/game": "负场 / 场次",
    "kills/deaths": "击杀 / 死亡",
    "K/D": "K/D",
}


def _label(text: str) -> str:
    text = text.strip()
    return LABELS.get(text, text)


def parse_stat_lines(
    lines: Iterable[str],
) -> tuple[list[tuple[str, str]], list[tuple[str, float, str]]]:
    """把 "label: a/b ≈ 85.7%" 这类统计行拆成进度条或普通行。"""
    bars: list[tuple[str, float, str]] = []
    rows: list[tuple[str, str]] = []
    for line in lines:
        if "≈" not in line:
            if ":" in line:
                k, _, v = line.partition(":")
                rows.append((_label(k), v.strip()))
            else:
                rows.append((line, ""))
            continue
        head, _, tail = line.partition("≈")
        label, _, value = head.partition(":")
        raw_tail = tail.strip()
        percent = raw_tail.endswith("%")
        try:
            num = float(raw_tail.rstrip("%"))
        except Exception:
            rows.append((_label(label), value.strip()))
            continue
        if percent:
            bars.append((_label(label), num / 100.0, f"{num:.1f}%"))
        else:
            rows.append((_label(label), f"{value.strip()}  ({num:.2f})"))
    return rows, bars


def stats_blocks(stats_lines: Iterable[str]) -> list[dict]:
    """把统计行直接转成 block，方便复用。"""
    rows, bars = parse_stat_lines(stats_lines)
    blocks: list[dict] = []
    if bars:
        blocks.append(
            {
                "type": "bars",
                "items": [{"label": a, "value": b, "text": c} for a, b, c in bars],
            }
        )
    if rows:
        blocks.append(
            {"type": "rows", "items": [{"label": a, "value": b} for a, b in rows]}
        )
    return blocks


def normalize_blocks(raw: Any) -> list[dict]:
    """校验并清洗外部传进来的 block，丢掉不认识的东西。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    allowed = {"hero", "kpi", "bars", "rows", "table", "section", "note", "divider"}
    for block in raw[:20]:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "").lower()
        if kind not in allowed:
            continue
        clean: dict[str, Any] = {"type": kind}
        if kind == "hero":
            clean["label"] = str(block.get("label") or "")[:40]
            clean["value"] = str(block.get("value") or "")[:24]
            clean["unit"] = str(block.get("unit") or "")[:10]
        elif kind in ("kpi", "rows", "table"):
            if kind == "table":
                cols = block.get("columns") or []
                clean["columns"] = [str(c)[:18] for c in cols[:5]]
                rows = []
                for row in (block.get("rows") or [])[:12]:
                    if isinstance(row, (list, tuple)):
                        rows.append([str(c)[:20] for c in list(row)[:5]])
                clean["rows"] = rows
            else:
                items = []
                for item in (block.get("items") or [])[:9 if kind == "kpi" else 14]:
                    if isinstance(item, dict):
                        items.append(
                            {
                                "label": str(item.get("label") or "")[:24],
                                "value": str(item.get("value") or "")[:28],
                            }
                        )
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        items.append({"label": str(item[0])[:24], "value": str(item[1])[:28]})
                clean["items"] = items
        elif kind == "bars":
            items = []
            for item in (block.get("items") or [])[:8]:
                if not isinstance(item, dict):
                    continue
                try:
                    val = float(item.get("value"))
                except Exception:
                    continue
                items.append(
                    {
                        "label": str(item.get("label") or "")[:24],
                        "value": val,
                        "text": str(item.get("text") or "")[:12],
                    }
                )
            clean["items"] = items
        elif kind in ("section", "note"):
            clean["text"] = str(block.get("text") or "")[:200]
        out.append(clean)
    return out
