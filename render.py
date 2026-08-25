"""HLTV 图片渲染模块（Pillow 纯 Python 实现，无浏览器/无头浏览器依赖）

与 NoneBot 原版功能一致：以图片形式展示赛事列表、比赛、结果、比赛数据、
帮助与开赛提醒。视觉风格沿用原 HTML 模板的深色主题配色，信息完整等价。
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import pytz
from PIL import Image, ImageDraw, ImageFont

from .config import plugin_config
from .data_source import EventInfo, MatchInfo, ResultInfo, MatchStats

# -------------------- 配色（与原 HTML 模板一致） --------------------

BG_TOP = (26, 26, 46)
BG_BOTTOM = (22, 33, 62)
CARD = (30, 30, 50)
CARD_ALT = (40, 40, 60)
PANEL = (16, 18, 30)  # 深色面板
TEXT = (224, 224, 224)
TEXT_DIM = (136, 136, 136)
TEXT_DARK = (102, 102, 102)
WHITE = (255, 255, 255)
ORANGE = (255, 152, 0)
ORANGE_DARK = (245, 124, 0)
GREEN = (39, 174, 96)
RED = (231, 76, 60)
BLUE = (52, 152, 219)
PURPLE = (155, 89, 182)
GOLD = (255, 199, 82)
BRONZE = (205, 127, 50)

# 外边距
PAD = 20

# -------------------- 字体管理 --------------------

# 常见系统中文字体路径（按优先级）
_FONT_CANDIDATES = {
    "regular": [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "bold": [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
}

_font_cache: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """获取字体（缓存）；找不到中文字体时回退 Pillow 默认字体"""
    key = (bold, size)
    if key in _font_cache:
        return _font_cache[key]
    path = None
    for p in _FONT_CANDIDATES["bold" if bold else "regular"]:
        try:
            with open(p, "rb"):
                path = p
                break
        except OSError:
            continue
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# -------------------- 绘制辅助 --------------------


def _canvas(width: int, height: int) -> Image.Image:
    """创建深色渐变背景画布"""
    img = Image.new("RGB", (width, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    top, bottom = BG_TOP, BG_BOTTOM
    step = max(1, height - 1)
    for y in range(height):
        t = y / step
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    return img


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return _text_size(draw, text, font)[0]


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """按像素宽度截断文本并加省略号"""
    if not text:
        return ""
    if _text_width(draw, text, font) <= max_width:
        return text
    ell = "…"
    while text and _text_width(draw, text + ell, font) > max_width:
        text = text[:-1]
    return text + ell


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font,
    fill=TEXT,
    anchor: str = "la",
) -> None:
    """绘制文本。anchor: la=左中对齐, ma=水平居中, ra=右对齐"""
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _rounded_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int] = CARD,
    radius: int = 8,
    outline: Optional[tuple[int, int, int]] = None,
    outline_width: int = 1,
) -> None:
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=outline,
        width=outline_width,
    )


def _badge(
    draw: ImageDraw.ImageDraw,
    center_xy: tuple[int, int],
    text: str,
    font,
    bg: tuple[int, int, int],
    fg: tuple[int, int, int],
) -> int:
    """绘制圆角胶囊徽章，返回其宽度"""
    w, h = _text_size(draw, text, font)
    pad_x, pad_y = 8, 3
    x, y = center_xy
    left = x - w // 2 - pad_x
    right = x + w // 2 + pad_x
    top = y - h // 2 - pad_y
    bottom = y + h // 2 + pad_y
    _rounded_card(draw, (left, top, right, bottom), fill=bg, radius=(bottom - top) // 2)
    _draw_text(draw, (x, y), text, font, fill=fg, anchor="mm")
    return right - left


def _section_title(draw: ImageDraw.ImageDraw, x: int, y: int, title: str, font) -> None:
    """绘制带左侧竖条的橙色小节标题"""
    draw.rounded_rectangle((x, y, x + 4, y + 16), radius=2, fill=ORANGE)
    _draw_text(draw, (x + 12, y - 2), title, font, fill=ORANGE)


def _footer(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, footer_text: str) -> None:
    """绘制底部水印 + 时间戳 + 署名"""
    draw.line([(x, y), (x + width - 2 * PAD, y)], fill=(51, 51, 51), width=1)
    y += 12
    watermark = plugin_config.hltv_watermark_text
    if watermark:
        for line in str(watermark).split("\n"):
            _draw_text(draw, (x, y), line, _font(12), fill=TEXT_DIM)
            y += 16
    _draw_text(draw, (x, y), footer_text, _font(12), fill=TEXT_DARK)


def get_timestamp() -> str:
    """获取当前时间戳"""
    tz = pytz.timezone(plugin_config.hltv_timezone)
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# -------------------- 头部 --------------------

HEADER_H = 54


def _draw_header(draw: ImageDraw.ImageDraw, x: int, y: int, icon: str, title: str, subtitle: str) -> None:
    """绘制页面头部（含底部橙色分隔线）"""
    icon_size = 40
    _rounded_card(draw, (x, y, x + icon_size, y + icon_size), fill=ORANGE_DARK, radius=8)
    _draw_text(draw, (x + icon_size // 2, y + icon_size // 2), icon, _font(16, bold=True), fill=(26, 26, 46), anchor="mm")
    _draw_text(draw, (x + icon_size + 15, y + 2), title, _font(22, bold=True), fill=WHITE)
    _draw_text(draw, (x + icon_size + 15, y + 30), subtitle, _font(13), fill=TEXT_DIM)
    draw.line([(x, y + icon_size + 14), (x + HEADER_H - 14 + icon_size, y + icon_size + 14)], fill=ORANGE, width=2)


def _inner_width(width: int) -> int:
    return width - 2 * PAD


# -------------------- 渲染：赛事列表 --------------------


async def render_events(
    ongoing_events: list[EventInfo],
    upcoming_events: list[EventInfo],
    subscribed_ids: list[str],
) -> bytes:
    """渲染赛事列表图片"""
    width = 650
    inner = _inner_width(width)

    item_h = 52
    h = PAD + HEADER_H + 14
    if ongoing_events:
        h += 28 + item_h * len(ongoing_events) + 16
    if upcoming_events:
        h += 28 + item_h * len(upcoming_events) + 16
    if not ongoing_events and not upcoming_events:
        h += 30 + 16
    h += 70 + 46 + PAD  # tip + footer

    img = _canvas(width, h)
    draw = ImageDraw.Draw(img)
    x, y = PAD, PAD

    _draw_header(draw, x, y, "E", "HLTV 赛事列表", "Big Events • 近期大型赛事")
    y += HEADER_H + 14

    if ongoing_events:
        _section_title(draw, x, y, "正在进行", _font(16, bold=True))
        y += 28
        for e in ongoing_events:
            _event_item(draw, x, y, inner, e, "进行中", GREEN, e.id in subscribed_ids)
            y += item_h
        y += 16

    if upcoming_events:
        _section_title(draw, x, y, "即将举行", _font(16, bold=True))
        y += 28
        for e in upcoming_events:
            _event_item(draw, x, y, inner, e, "即将开始", BLUE, e.id in subscribed_ids)
            y += item_h
        y += 16

    if not ongoing_events and not upcoming_events:
        _draw_text(draw, (x, y), "暂无大型赛事", _font(14), fill=TEXT_DIM)
        y += 30 + 16

    # 使用说明
    _rounded_card(draw, (x, y, x + inner, y + 54), fill=(45, 35, 20), radius=8)
    draw.rounded_rectangle((x, y, x + 3, y + 54), radius=2, fill=ORANGE)
    _draw_text(draw, (x + 12, y + 6), "● 使用说明", _font(13, bold=True), fill=ORANGE)
    _draw_text(draw, (x + 12, y + 28), "event订阅 [ID] 订阅赛事 · event取消订阅 [ID] 取消订阅 · ★ 标记已订阅", _font(12), fill=(204, 204, 204))
    y += 70

    _footer(draw, x, y, width, f"HLTV SUB PLUGIN • {get_timestamp()}")
    return _to_png(img)


def _event_item(draw, x: int, y: int, inner: int, e: EventInfo, status: str, status_color, subscribed: bool) -> None:
    """单个赛事条目"""
    item_h = 46
    _rounded_card(draw, (x, y, x + inner, y + item_h), fill=CARD_ALT, radius=8)
    cy = y + item_h // 2

    # ID 徽章（左）
    id_font = _font(13)
    id_w, id_h = _text_size(draw, f"#{e.id}", id_font)
    bx = x + 12
    _rounded_card(draw, (bx, cy - id_h // 2 - 4, bx + id_w + 16, cy + id_h // 2 + 4), fill=(18, 20, 32), radius=4)
    _draw_text(draw, (bx + 8, cy), f"#{e.id}", id_font, fill=TEXT_DIM, anchor="lm")

    # 状态徽章（右，右边界固定）
    st_font = _font(12)
    st_w, _ = _text_size(draw, status, st_font)
    badge_right = x + inner - 12
    badge_left = badge_right - st_w - 16  # 胶囊总宽 = st_w + 2*8
    _badge(draw, ((badge_left + badge_right) // 2, cy), status, st_font, status_color, WHITE)

    # 订阅星标（徽章左侧）；文本区右边界随之收紧
    text_right = badge_left - 10
    if subscribed:
        _draw_text(draw, (badge_left - 8, cy), "★", _font(15), fill=ORANGE, anchor="rm")
        text_right = badge_left - 24

    # 标题 + 日期
    text_left = bx + id_w + 26
    available = max(24, text_right - text_left)
    title_font = _font(15, bold=True)
    title = _truncate(draw, e.title, title_font, available)
    _draw_text(draw, (text_left, y + 8), title, title_font, fill=WHITE)
    date_text = f"{e.start_date} ~ {e.end_date}" if e.start_date and e.end_date else ""
    _draw_text(draw, (text_left, y + item_h - 22), _truncate(draw, date_text, _font(12), available), _font(12), fill=(153, 153, 153))


# -------------------- 渲染：比赛列表 --------------------


async def render_matches(
    matches_by_event: dict[str, list[MatchInfo]],
    live_count: int,
    upcoming_count: int,
) -> bytes:
    """渲染比赛列表图片"""
    width = 700
    inner = _inner_width(width)

    h = PAD + HEADER_H + 14
    if live_count > 0 or upcoming_count > 0:
        h += 44
    for matches in matches_by_event.values():
        h += 26 + 40 * len(matches) + 12
    if not matches_by_event:
        h += 90
    h += 46 + PAD

    img = _canvas(width, h)
    draw = ImageDraw.Draw(img)
    x, y = PAD, PAD

    _draw_header(draw, x, y, "M", "比赛列表", "已订阅赛事的比赛")
    y += HEADER_H + 14

    if live_count > 0 or upcoming_count > 0:
        _rounded_card(draw, (x, y, x + inner, y + 34), fill=PANEL, radius=8)
        cy = y + 17
        sx = x + 20
        if live_count > 0:
            _draw_text(draw, (sx, cy), str(live_count), _font(18, bold=True), fill=ORANGE, anchor="lm")
            w = _text_width(draw, str(live_count), _font(18, bold=True))
            _draw_text(draw, (sx + w + 4, cy), " 场直播中  ", _font(13), fill=TEXT_DIM, anchor="lm")
            sx += w + 4 + _text_width(draw, " 场直播中  ", _font(13))
        _draw_text(draw, (sx, cy), str(upcoming_count), _font(18, bold=True), fill=ORANGE, anchor="lm")
        w = _text_width(draw, str(upcoming_count), _font(18, bold=True))
        _draw_text(draw, (sx + w + 4, cy), " 场即将开始", _font(13), fill=TEXT_DIM, anchor="lm")
        y += 44

    for event_name, matches in matches_by_event.items():
        _section_title(draw, x, y, event_name, _font(14, bold=True))
        y += 26
        for m in matches:
            _match_item(draw, x, y, inner, m)
            y += 40
        y += 12

    if not matches_by_event:
        _draw_text(draw, (x, y), "暂无比赛", _font(14, bold=True), fill=TEXT_DIM)
        y += 24
        _draw_text(draw, (x, y), "请先订阅赛事，或等待比赛公布", _font(12), fill=TEXT_DARK)
        y += 50

    _footer(draw, x, y, width, f"HLTV SUB PLUGIN • {get_timestamp()}")
    return _to_png(img)


def _match_item(draw, x: int, y: int, inner: int, m: MatchInfo) -> None:
    """单个比赛条目"""
    item_h = 38
    box = (x, y, x + inner, y + item_h)
    if m.is_live:
        _rounded_card(draw, box, fill=(46, 28, 30), radius=8, outline=RED, outline_width=1)
    elif m.is_grand_final:
        _rounded_card(draw, box, fill=(42, 36, 22), radius=8, outline=GOLD, outline_width=1)
    elif m.is_third_place:
        _rounded_card(draw, box, fill=(40, 32, 22), radius=8, outline=BRONZE, outline_width=1)
    else:
        _rounded_card(draw, box, fill=CARD_ALT, radius=8)
    cy = y + item_h // 2

    # 右侧信息区（固定宽度，右对齐）
    rx = x + inner - 12
    rx_left = x + inner - 100

    # 队伍区（左右边界）
    team_left = x + 80
    team_right = rx_left - 10
    mid = (team_left + team_right) // 2

    # 时间 / LIVE（左列）
    if m.is_live:
        _badge(draw, (x + 44, cy), "LIVE", _font(12, bold=True), RED, WHITE)
    else:
        _draw_text(draw, (x + 42, cy - 9), m.time, _font(14, bold=True), fill=WHITE, anchor="ma")
        _draw_text(draw, (x + 42, cy + 7), m.date, _font(11), fill=TEXT_DIM, anchor="ma")

    # 队伍：team1 紧贴 VS 左侧（右对齐），team2 紧贴 VS 右侧（左对齐）
    team_font = _font(14, bold=True)
    vs_label = "VS"
    vs_font = _font(12, bold=True)
    vs_w = _text_width(draw, vs_label, vs_font)
    t1_right = mid - vs_w // 2 - 10
    t2_left = mid + vs_w // 2 + 10
    t1_avail = max(20, t1_right - team_left - 8)
    t2_avail = max(20, team_right - t2_left - 8)
    _draw_text(draw, (t1_right, cy), _truncate(draw, m.team1, team_font, t1_avail), team_font, fill=WHITE, anchor="rm")
    _draw_text(draw, (t2_left, cy), _truncate(draw, m.team2, team_font, t2_avail), team_font, fill=WHITE, anchor="lm")
    _draw_text(draw, (mid, cy), vs_label, vs_font, fill=TEXT_DARK, anchor="mm")

    # 右侧信息（BO / 编号 / 阶段，右对齐到 rx，不越 rx_left）
    small_font = _font(11)
    right_texts = []
    if m.is_grand_final:
        right_texts.append(("GRAND FINAL", GOLD))
    elif m.is_third_place:
        right_texts.append(("3RD PLACE", BRONZE))
    if m.maps:
        right_texts.append((f"BO{m.maps}", TEXT_DIM))
    right_texts.append((f"#{m.id}", (85, 85, 85)))
    ry = cy - (len(right_texts) * 11) // 2
    for text, color in right_texts:
        _draw_text(draw, (rx, ry), text, small_font, fill=color, anchor="rm")
        ry += 11


# -------------------- 渲染：结果列表 --------------------


async def render_results(results_by_event: dict[str, list[ResultInfo]]) -> bytes:
    """渲染结果列表图片"""
    width = 700
    inner = _inner_width(width)

    h = PAD + HEADER_H + 14
    for results in results_by_event.values():
        h += 26 + 40 * len(results) + 12
    if not results_by_event:
        h += 80
    h += 60 + 46 + PAD  # tip + footer

    img = _canvas(width, h)
    draw = ImageDraw.Draw(img)
    x, y = PAD, PAD

    _draw_header(draw, x, y, "R", "比赛结果", "已订阅赛事的已完成比赛")
    y += HEADER_H + 14

    for event_name, results in results_by_event.items():
        _section_title(draw, x, y, event_name, _font(14, bold=True))
        y += 26
        for r in results:
            _result_item(draw, x, y, inner, r)
            y += 40
        y += 12

    if not results_by_event:
        _draw_text(draw, (x, y), "暂无比赛结果", _font(14, bold=True), fill=TEXT_DIM)
        y += 24
        _draw_text(draw, (x, y), "请先订阅赛事", _font(12), fill=TEXT_DARK)
        y += 40

    # tip
    _rounded_card(draw, (x, y, x + inner, y + 44), fill=(36, 28, 40), radius=8)
    draw.rounded_rectangle((x, y, x + 3, y + 44), radius=2, fill=PURPLE)
    _draw_text(draw, (x + 12, y + 13), "发送  stats [比赛ID]  查看比赛详细数据", _font(13), fill=(204, 204, 204))
    y += 60

    _footer(draw, x, y, width, f"HLTV SUB PLUGIN • {get_timestamp()}")
    return _to_png(img)


def _result_item(draw, x: int, y: int, inner: int, r: ResultInfo) -> None:
    """单个结果条目"""
    item_h = 38
    _rounded_card(draw, (x, y, x + inner, y + item_h), fill=CARD_ALT, radius=8)
    cy = y + item_h // 2

    # 日期
    _draw_text(draw, (x + 14, cy), r.date, _font(13), fill=TEXT_DIM, anchor="lm")

    # 比分（居中）
    try:
        s1, s2 = int(r.score1), int(r.score2)
        t1_won = s1 > s2
    except (TypeError, ValueError):
        t1_won = False
    score_font = _font(18, bold=True)
    sep_font = _font(16, bold=True)
    s1_w = _text_width(draw, r.score1, score_font)
    sep_w = _text_width(draw, ":", sep_font)
    s2_w = _text_width(draw, r.score2, score_font)
    total = s1_w + sep_w + s2_w
    mid = x + inner // 2
    sx = mid - total // 2
    _draw_text(draw, (sx, cy), r.score1, score_font, fill=GREEN if t1_won else TEXT_DIM, anchor="lm")
    sx += s1_w
    _draw_text(draw, (sx, cy), ":", sep_font, fill=(85, 85, 85), anchor="lm")
    sx += sep_w
    _draw_text(draw, (sx, cy), r.score2, score_font, fill=GREEN if not t1_won else TEXT_DIM, anchor="lm")

    # 队伍（比分两侧）
    team_font = _font(14, bold=True)
    left_edge = mid - total // 2 - 14
    right_edge = mid + total // 2 + 14
    _draw_text(draw, (left_edge - 8, cy), _truncate(draw, r.team1, team_font, left_edge - x - 100), team_font, fill=GREEN if t1_won else (153, 153, 153), anchor="rm")
    _draw_text(draw, (right_edge + 8, cy), _truncate(draw, r.team2, team_font, inner - (right_edge + 8 - x) - 80), team_font, fill=GREEN if not t1_won else (153, 153, 153), anchor="lm")

    # ID（右端）
    id_font = _font(11)
    _draw_text(draw, (x + inner - 12, cy), f"#{r.id}", id_font, fill=(85, 85, 85), anchor="rm")


# -------------------- 渲染：比赛数据 --------------------


def _players_section_h(players) -> int:
    """选手数据区块高度：标题 + 列头 + 行数 + 间距"""
    n1 = len([p for p in players if p.team == "team1"])
    n2 = len([p for p in players if p.team == "team2"])
    rows = max(1, n1, n2)
    return 26 + 24 + 22 * rows + 6 + 12


async def render_stats(stats: Optional[MatchStats]) -> bytes:
    """渲染比赛数据图片"""
    width = 800
    inner = _inner_width(width)

    if not stats:
        img = _canvas(width, 300)
        draw = ImageDraw.Draw(img)
        _draw_text(draw, (width // 2, 150), "无法获取比赛数据", _font(16), fill=TEXT_DIM, anchor="mm")
        return _to_png(img)

    played_maps = [m for m in stats.maps if m.score_team1 != "-" and m.score_team2 != "-"]
    has_single_map_details = (
        len(played_maps) == 1 and played_maps[0].map_name in stats.map_stats_details
    )
    show_total_overview = not has_single_map_details
    map_names = {m.map_name for m in stats.maps}

    # 高度预计算
    h = PAD + HEADER_H + 14 + 96 + 26
    if stats.vetos:
        h += 26 + 18 * len(stats.vetos) + 16 + 12
    if played_maps:
        h += 26 + 74 + 12
    if stats.players and show_total_overview:
        h += _players_section_h(stats.players)
    for map_name, players in stats.map_stats_details.items():
        if map_name in map_names:
            h += _players_section_h(players)
    h += 46 + PAD

    img = _canvas(width, h)
    draw = ImageDraw.Draw(img)
    x, y = PAD, PAD

    _draw_header(draw, x, y, "S", "比赛数据", stats.event or "比赛详情")
    y += HEADER_H + 14

    # 比分头
    _rounded_card(draw, (x, y, x + inner, y + 80), fill=CARD_ALT, radius=12)
    cy = y + 44
    try:
        s1, s2 = int(stats.score1), int(stats.score2)
        has_winner = s1 != s2
        t1_won = s1 > s2
    except (TypeError, ValueError):
        has_winner, t1_won = False, False
    team_font = _font(20, bold=True)
    t1_color = GREEN if (has_winner and t1_won) else WHITE
    t2_color = GREEN if (has_winner and not t1_won) else WHITE
    _draw_text(draw, (x + 20, cy), _truncate(draw, stats.team1, team_font, 240), team_font, fill=t1_color, anchor="lm")
    _draw_text(draw, (x + inner - 20, cy), _truncate(draw, stats.team2, team_font, 240), team_font, fill=t2_color, anchor="rm")
    score_text = f"{stats.score1}:{stats.score2}"
    _draw_text(draw, (width // 2, cy), score_text, _font(40, bold=True), fill=WHITE, anchor="mm")
    _draw_text(draw, (width // 2, y + 66), stats.status or "", _font(13), fill=TEXT_DIM, anchor="ma")
    y += 96

    # 比赛流程（BP）
    if stats.vetos:
        _section_title(draw, x, y, "比赛流程 (BP)", _font(15, bold=True))
        y += 26
        _rounded_card(draw, (x, y, x + inner, y + 18 * len(stats.vetos) + 16), fill=CARD_ALT, radius=8)
        vy = y + 10
        for veto in stats.vetos:
            _draw_text(draw, (x + 16, vy), veto, _font(13), fill=(204, 204, 204))
            vy += 18
        y += 18 * len(stats.vetos) + 16 + 12

    # 地图比分
    if played_maps:
        _section_title(draw, x, y, "地图比分", _font(15, bold=True))
        y += 26
        _draw_map_items(draw, x, y, inner, played_maps)
        y += 74 + 12

    # 选手数据（总览 + 单图）
    if stats.players and show_total_overview:
        _players_grid(draw, x, y, inner, stats, stats.players, "(总览)")
        y += _players_section_h(stats.players)
    for map_name, players in stats.map_stats_details.items():
        if map_name in map_names:
            _players_grid(draw, x, y, inner, stats, players, f"- {map_name}")
            y += _players_section_h(players)

    _footer(draw, x, y, width, f"HLTV SUB PLUGIN • {get_timestamp()}")
    return _to_png(img)


def _draw_map_items(draw, x: int, y: int, inner: int, maps) -> None:
    """地图比分卡片（一行横向排列）"""
    card_w = 150
    gap = 10
    n = min(len(maps), max(1, (inner + gap) // (card_w + gap)))
    for i, m in enumerate(maps[:n]):
        cx = x + i * (card_w + gap)
        _rounded_card(draw, (cx, y, cx + card_w, y + 72), fill=CARD_ALT, radius=8)
        _draw_text(draw, (cx + card_w // 2, y + 10), _truncate(draw, m.map_name, _font(13, bold=True), card_w - 20), _font(13, bold=True), fill=ORANGE, anchor="ma")
        try:
            ms1, ms2 = int(m.score_team1), int(m.score_team2)
            m1_won = ms1 > ms2
        except (TypeError, ValueError):
            m1_won = False
        score_font = _font(18, bold=True)
        s1_w = _text_width(draw, m.score_team1, score_font)
        sep_w = _text_width(draw, ":", _font(16, bold=True))
        total = s1_w + sep_w + _text_width(draw, m.score_team2, score_font)
        sx = cx + card_w // 2 - total // 2
        _draw_text(draw, (sx, y + 40), m.score_team1, score_font, fill=GREEN if m1_won else TEXT_DIM, anchor="lm")
        _draw_text(draw, (sx + s1_w, y + 40), ":", _font(16, bold=True), fill=(85, 85, 85), anchor="lm")
        _draw_text(draw, (sx + s1_w + sep_w, y + 40), m.score_team2, score_font, fill=GREEN if not m1_won else TEXT_DIM, anchor="lm")
        pick_text = f"{m.pick_by} pick" if m.pick_by else "Decider"
        _draw_text(draw, (cx + card_w // 2, y + 58), pick_text, _font(11), fill=PURPLE if not m.pick_by else TEXT_DARK, anchor="ma")


def _players_grid(draw, x: int, y: int, inner: int, stats, players, title_suffix: str) -> None:
    """选手数据双列网格"""
    _section_title(draw, x, y, f"选手数据 {title_suffix}", _font(15, bold=True))
    y += 26
    col_w = (inner - 15) // 2
    team1 = [p for p in players if p.team == "team1"]
    team2 = [p for p in players if p.team == "team2"]
    _player_column(draw, x, y, col_w, stats.team1, team1, BLUE)
    _player_column(draw, x + col_w + 15, y, col_w, stats.team2, team2, RED)


def _player_column(draw, x: int, y: int, col_w: int, team_name: str, players, accent) -> None:
    """单队选手列"""
    row_h = 22
    header_h = 24
    total_h = header_h + row_h * max(1, len(players)) + 6
    _rounded_card(draw, (x, y, x + col_w, y + total_h), fill=CARD_ALT, radius=8)
    draw.rounded_rectangle((x, y, x + 3, y + header_h), radius=2, fill=accent)
    _draw_text(draw, (x + 12, y + 3), _truncate(draw, team_name, _font(13, bold=True), col_w - 24), _font(13, bold=True), fill=WHITE)
    ry = y + header_h + 3
    small = _font(11)
    for p in players:
        nickname = _truncate(draw, p.nickname, _font(12, bold=True), col_w - 300)
        _draw_text(draw, (x + 12, ry), nickname, _font(12, bold=True), fill=WHITE)
        kd = f"{p.kills}/{p.deaths}"
        swing = p.swing or ""
        adr = p.adr or ""
        kast = p.kast or ""
        rating = p.rating or ""
        try:
            rv = float(str(rating))
        except (TypeError, ValueError):
            rv = 0.0
        try:
            swing_val = float(str(swing).replace("%", "").replace("+", ""))
        except (TypeError, ValueError):
            swing_val = 0.0
        # 从右往左绘制统计值（Rating KAST ADR Swing K/D）
        items = [(rating, GREEN if rv > 1.05 else RED if rv < 0.95 else TEXT_DIM),
                 (kast, TEXT_DIM),
                 (adr, TEXT_DIM),
                 (swing, GREEN if swing_val > 1 else RED if swing_val < -1 else TEXT_DIM),
                 (kd, TEXT_DIM)]
        rx = x + col_w - 12
        for text, color in items:
            w = _text_width(draw, text, small)
            _draw_text(draw, (rx, ry), text, small, fill=color, anchor="rm")
            rx -= w + 16
        ry += row_h


# -------------------- 渲染：帮助 --------------------


async def render_help(sections: list[dict]) -> bytes:
    """渲染帮助图片

    Args:
        sections: 帮助分组数据：
          [{"title": str, "note": str, "commands": [{"name","args","aliases","desc","admin_only","superuser_only"}]}]
    """
    width = 820
    inner = _inner_width(width)

    h = PAD + HEADER_H + 14
    for sec in sections:
        h += 30
        h += 40 * len(sec.get("commands", []))
        h += 10
    h += 46 + PAD

    img = _canvas(width, h)
    draw = ImageDraw.Draw(img)
    x, y = PAD, PAD

    _draw_header(draw, x, y, "?", "HLTV 帮助", "命令说明与权限标记")
    y += HEADER_H + 14

    for sec in sections:
        title = sec.get("title", "")
        note = sec.get("note", "")
        if note:
            title = f"{title}  ({note})"
        _section_title(draw, x, y, title, _font(15, bold=True))
        y += 30
        for cmd in sec.get("commands", []):
            _help_item(draw, x, y, inner, cmd)
            y += 40
        y += 10

    _footer(draw, x, y, width, f"HLTV SUB PLUGIN • {get_timestamp()}")
    return _to_png(img)


def _help_item(draw, x: int, y: int, inner: int, cmd: dict) -> None:
    """单个帮助命令条目"""
    item_h = 38
    _rounded_card(draw, (x, y, x + inner, y + item_h), fill=CARD_ALT, radius=8)
    cy = y + item_h // 2

    # 右侧权限徽章（右边界固定）
    badge_text = ""
    badge_color = None
    if cmd.get("superuser_only"):
        badge_text = "超管"
        badge_color = PURPLE
    elif cmd.get("admin_only"):
        badge_text = "管理"
        badge_color = ORANGE_DARK
    badge_right = x + inner - 12
    badge_left = badge_right
    if badge_text:
        b_font = _font(11, bold=True)
        bw = _text_width(draw, badge_text, b_font)
        badge_w = bw + 16
        badge_left = badge_right - badge_w
        _badge(draw, (badge_left + badge_w // 2, cy), badge_text, b_font, badge_color, WHITE)
    # 文本区右边界（不越过徽章）
    text_right = x + inner - 12 if not badge_text else badge_left - 14

    # 左半区：命令名 + 参数
    left_max = x + inner // 2 - 10
    nx = x + 14
    name_font = _font(14, bold=True)
    name = cmd.get("name", "")
    _draw_text(draw, (nx, cy), name, name_font, fill=ORANGE, anchor="lm")
    nx += _text_width(draw, name, name_font) + 8
    args = cmd.get("args", "")
    if args and nx < left_max:
        args_font = _font(12)
        _draw_text(draw, (nx, cy), _truncate(draw, args, args_font, max(16, left_max - nx - 6)), args_font, fill=TEXT_DIM, anchor="lm")

    # 中部：描述
    desc_x = x + inner // 2 + 10
    desc_font = _font(12)
    avail = max(16, text_right - desc_x)
    _draw_text(draw, (desc_x, cy), _truncate(draw, cmd.get("desc", ""), desc_font, avail), desc_font, fill=(204, 204, 204), anchor="lm")


# -------------------- 渲染：开赛提醒 --------------------


async def render_reminder(
    team1: str,
    team2: str,
    event_title: str,
    minutes_until: int,
    start_time_str: str = "",
    maps: str = "",
    is_grand_final: bool = False,
    is_third_place: bool = False,
) -> bytes:
    """渲染比赛开始提醒图片"""
    width = 550
    inner = _inner_width(width)

    h = PAD + HEADER_H + 14
    h += 96  # countdown
    h += 72  # event banner
    h += 108  # versus
    h += 60 if maps else 10
    h += 46 + PAD

    img = _canvas(width, h)
    draw = ImageDraw.Draw(img)
    x, y = PAD, PAD

    _draw_header(draw, x, y, "L", "比赛已开始", "Match Live Now")
    y += HEADER_H + 14

    # countdown 卡片
    _rounded_card(draw, (x, y, x + inner, y + 80), fill=PANEL, radius=10, outline=ORANGE, outline_width=2)
    _draw_text(draw, (width // 2, y + 16), start_time_str or "LIVE", _font(36, bold=True), fill=ORANGE, anchor="ma")
    _draw_text(draw, (width // 2, y + 62), "当前状态 Status", _font(12), fill=TEXT_DARK, anchor="ma")
    y += 96

    # event 横幅
    _rounded_card(draw, (x, y, x + inner, y + 56), fill=(36, 28, 18), radius=6)
    _draw_text(draw, (width // 2, y + 18), f"◆ {event_title}", _font(15, bold=True), fill=ORANGE, anchor="ma")
    if is_grand_final:
        _badge(draw, (width // 2, y + 40), "GRAND FINAL", _font(11, bold=True), GOLD, (43, 26, 0))
    elif is_third_place:
        _badge(draw, (width // 2, y + 40), "3RD PLACE", _font(11, bold=True), BRONZE, (43, 20, 0))
    y += 72

    # 队伍 VS
    _rounded_card(draw, (x, y, x + inner, y + 92), fill=PANEL, radius=10)
    cy = y + 46
    team_w = (inner - 60) // 2
    _rounded_card(draw, (x + 10, y + 12, x + 10 + team_w, y + 80), fill=CARD_ALT, radius=8)
    _draw_text(draw, (x + 10 + team_w // 2, cy), _truncate(draw, team1, _font(16, bold=True), team_w - 20), _font(16, bold=True), fill=WHITE, anchor="ma")
    _rounded_card(draw, (x + inner - 10 - team_w, y + 12, x + inner - 10, y + 80), fill=CARD_ALT, radius=8)
    _draw_text(draw, (x + inner - 10 - team_w // 2, cy), _truncate(draw, team2, _font(16, bold=True), team_w - 20), _font(16, bold=True), fill=WHITE, anchor="ma")
    _draw_text(draw, (width // 2, cy), "VS", _font(20, bold=True), fill=ORANGE, anchor="mm")
    y += 108

    if maps:
        _rounded_card(draw, (x, y, x + inner, y + 44), fill=PANEL, radius=8)
        _draw_text(draw, (width // 2, y + 22), f"◆ 比赛格式  BO{maps}", _font(14, bold=True), fill=WHITE, anchor="ma")
        y += 60

    _footer(draw, x, y, width, f"HLTV SUB PLUGIN • {get_timestamp()}")
    return _to_png(img)
