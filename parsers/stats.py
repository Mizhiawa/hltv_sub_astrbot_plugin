"""HLTV match stats 页面解析（/matches/{id}/...）。"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..log_compat import logger
from ..models import (
    HalfScore,
    InviteChance,
    MapStats,
    MatchStats,
    PlayerStats,
    VrsChange,
    VrsForecast,
    VrsInvite,
)
from .common import (
    absolute_url,
    extract_id_from_url,
    flag_of,
    format_date,
    is_grand_final_text,
    is_third_place_text,
    make_soup,
    pick_logo,
    text_of,
)


def _parse_player_table(table: Tag, team: str) -> list[PlayerStats]:
    """按表头找列：有的比赛有 eADR、没有 Swing。K-D、ADR、Rating 缺了就是页面变了。"""
    header_row = table.find("tr", class_="header-row")
    if header_row is None:
        return []
    headers = [
        cell.get_text(strip=True).upper()
        for cell in header_row.find_all(["th", "td"])
    ]

    def column(name: str, exclude: str = "") -> int:
        return next(
            (
                i
                for i, h in enumerate(headers)
                if name in h and not (exclude and exclude in h)
            ),
            -1,
        )

    idx_kd, idx_adr, idx_rating = (
        column("K-D"),
        column("ADR", "EADR"),
        column("RATING"),
    )
    idx_swing, idx_kast = column("SWING"), column("KAST", "EKAST")
    if -1 in (idx_kd, idx_adr, idx_rating):
        raise ValueError(f"HLTV 选手表缺少 K-D / ADR / Rating 列: {headers}")

    players: list[PlayerStats] = []
    rows = [
        r
        for r in table.find_all("tr")
        if not r.find("th") and "header-row" not in (r.get("class") or [])
    ]
    for row in rows[:5]:
        cells = row.find_all("td")
        kd_text = cells[idx_kd].get_text(strip=True)
        if "-" not in kd_text:
            continue
        kills, deaths = kd_text.split("-", 1)
        player_link = row.find("a", href=re.compile(r"/player/\d+/"))
        nick_elem = row.find("span", class_="player-nick")
        country, country_name = flag_of(row.find("img", class_="flag"))
        players.append(
            PlayerStats(
                id=extract_id_from_url(player_link["href"]) if player_link else "",
                nickname=text_of(nick_elem) or text_of(player_link),
                team=team,
                kills=kills.strip(),
                deaths=deaths.strip(),
                adr=cells[idx_adr].get_text(strip=True),
                rating=cells[idx_rating].get_text(strip=True),
                swing=cells[idx_swing].get_text(strip=True) if idx_swing >= 0 else "-",
                kast=cells[idx_kast].get_text(strip=True) if idx_kast >= 0 else "-",
                country=country,
                country_name=country_name,
            )
        )
    return players


def _parse_team_tables(container: Tag | None, table_class) -> list[PlayerStats]:
    """前两张表依次是 team1、team2。"""
    if container is None:
        return []
    tables = container.find_all("table", class_=table_class)[:2]
    return [
        player
        for table, team in zip(tables, ("team1", "team2"))
        for player in _parse_player_table(table, team)
    ]


def _parse_halves(map_holder: Tag) -> list[HalfScore]:
    """半场比分形如 (8:4; 5:3)，每个数字的 class 是该队当时的阵营。"""
    holder = map_holder.find("div", class_="results-center-half-score")
    if not holder:
        return []
    digits = [
        span for span in holder.find_all("span") if span.get_text(strip=True).isdigit()
    ]
    halves: list[HalfScore] = []
    for first, second in zip(digits[::2], digits[1::2]):
        side = next((c for c in (first.get("class") or []) if c in ("ct", "t")), "")
        halves.append(
            HalfScore(
                score1=first.get_text(strip=True),
                score2=second.get_text(strip=True),
                team1_side=side,
            )
        )
    return halves


def _parse_team_box(soup: BeautifulSoup, index: int) -> tuple[str, str, str]:
    """返回 (队标, 旗帜代码, 国家/地区名)。"""
    box = soup.select_one(f".teamsBox .team{index}-gradient")
    if box is None:
        return "", "", ""
    country, country_name = flag_of(box.parent.find("img", class_=f"team{index}"))
    return pick_logo(box.find_all("img", class_="logo")), country, country_name


def _vrs_container(soup: BeautifulSoup, header: str) -> Tag | None:
    container = soup.select_one(".vrs-forecast-container")
    middle = (
        container.select_one(".vrs-forecast-middle .vrs-forecast-header")
        if container
        else None
    )
    if middle is None or header not in middle.get_text(" ", strip=True).lower():
        return None
    return container


def _vrs_column(container: Tag, selector: str) -> list[tuple[int, int] | None]:
    rows: list[tuple[int, int] | None] = []
    for wrapper in container.select(f"{selector} .vrs-forecast-numbers-wrapper"):
        points = re.search(
            r"[+-]?\d+", text_of(wrapper.select_one(".vrs-forecast-points"))
        )
        rank = re.search(r"\d+", text_of(wrapper.select_one(".vrs-forecast-ranking")))
        rows.append(
            (int(points.group()), int(rank.group())) if points and rank else None
        )
    return rows


def _parse_vrs_result(soup: BeautifulSoup) -> tuple[VrsChange | None, VrsChange | None]:
    container = _vrs_container(soup, "result")
    if container is None:
        return None, None
    before = _vrs_column(container, ".vrs-forecast-left-numbers")
    after = _vrs_column(container, ".vrs-forecast-middle")
    if len(before) != 2 or len(after) != 2 or None in before + after:
        return None, None
    team1, team2 = (
        VrsChange(
            points_before=b[0], rank_before=b[1], points_delta=a[0], rank_after=a[1]
        )
        for b, a in zip(before, after)
    )
    return team1, team2


def _parse_vrs_forecast(
    soup: BeautifulSoup,
) -> tuple[VrsForecast | None, VrsForecast | None]:
    container = _vrs_container(soup, "team wins")
    if container is None:
        return None, None
    columns = [
        _vrs_column(container, selector)
        for selector in (
            ".vrs-forecast-left-numbers",
            ".vrs-forecast-middle",
            ".vrs-forecast-right",
        )
    ]
    if any(len(column) != 2 for column in columns):
        return None, None
    team1, team2 = (
        VrsForecast(
            points=current[0],
            rank=current[1],
            win_delta=win[0],
            win_rank=win[1],
            lose_delta=lose[0],
            lose_rank=lose[1],
        )
        if current and win and lose
        else None
        for current, win, lose in zip(*columns)
    )
    return team1, team2


INVITE_CHANCE = re.compile(r"[<>]?\d+(?:\.\d+)?%")


def _invite_chance(row: Tag) -> InviteChance | None:
    cells = row.select(".vrs-invite-stakes-chance")
    values = [INVITE_CHANCE.search(cell.get_text(" ", strip=True)) for cell in cells]
    if None in values:
        return None
    found = [value.group() for value in values if value is not None]
    if len(cells) == 3:
        return InviteChance(*found)
    if len(cells) == 2 and "either-way" in (cells[1].get("class") or []):
        return InviteChance(now=found[0], win=found[1], lose=found[1])
    return None


def _parse_vrs_invites(soup: BeautifulSoup) -> list[VrsInvite]:
    invites: list[VrsInvite] = []
    for block in soup.select(".vrs-invite-stakes-invite"):
        title = block.select_one(".vrs-invite-stakes-title")
        chances = [
            _invite_chance(row)
            for row in block.select(
                ".vrs-invite-stakes-row:not(.vrs-invite-stakes-labels)"
            )
        ]
        if title is None or len(chances) != 2 or None in chances:
            logger.warning(f"[HLTV] 跳过结构不明的 VRS 邀请: {text_of(title)!r}")
            continue
        invites.append(
            VrsInvite(
                event=text_of(title),
                logo=pick_logo(title.find_all("img")),
                team1=chances[0],  # type: ignore[arg-type]
                team2=chances[1],  # type: ignore[arg-type]
            )
        )
    return invites


def parse_match_stats(
    html: str,
    match_id: str,
    tz: Any,
    team1: str = "",
    team2: str = "",
    event_title: str = "",
) -> MatchStats:
    """解析 match 页面。赛前、进行中、赛后结构不同，只有队名是必需的，其余缺了就空着。"""
    soup = make_soup(html)

    team_names = soup.find_all("div", class_="teamName")
    if len(team_names) < 2:
        raise ValueError(f"HLTV 比赛 {match_id} 页面缺少队名，页面结构可能已变")
    team1 = team1 or team_names[0].get_text(strip=True)
    team2 = team2 or team_names[1].get_text(strip=True)

    team_boxes = soup.find_all("div", class_="team")[:2]
    raw_scores = [
        text_of(box.find(class_=re.compile(r"score|won|lost"))) for box in team_boxes
    ]
    while len(raw_scores) < 2:
        raw_scores.append("0")
    score1 = raw_scores[0] if raw_scores[0].isdigit() else "0"
    score2 = raw_scores[1] if raw_scores[1].isdigit() else "0"

    team1_logo, team1_country, team1_country_name = _parse_team_box(soup, 1)
    team2_logo, team2_country, team2_country_name = _parse_team_box(soup, 2)
    team1_vrs, team2_vrs = _parse_vrs_result(soup)
    team1_vrs_forecast, team2_vrs_forecast = _parse_vrs_forecast(soup)

    status = text_of(soup.find("div", class_="countdown")) or "完成"
    date_elem = soup.select_one(".timeAndEvent .date[data-unix]")
    match_date = format_date(date_elem["data-unix"], tz) if date_elem else ""
    event_title = event_title or text_of(soup.select_one(".timeAndEvent .event a"))

    vetos: list[str] = []
    format_line = ""
    stage = ""
    is_grand_final = False
    for box in soup.select("div.veto-box:not(.simulated-veto-only)"):
        lines = [
            line.strip() for line in box.get_text("\n").split("\n") if line.strip()
        ]
        if box.find(class_="preformatted-text"):
            for line in lines:
                if line.startswith("*"):
                    stage = stage or line.lstrip("* ").strip()
                    is_grand_final = is_grand_final or is_grand_final_text(line)
                else:
                    format_line = format_line or line
            continue
        vetos.extend(line for line in lines if not line.startswith("*"))

    maps = [
        map_info
        for map_info in map(
            _parse_map, soup.select("div.mapholder:not(.simulated-map)")
        )
        if map_info is not None
    ]
    map_stats_details: dict[str, list[PlayerStats]] = {}
    for map_info in maps:
        if map_info.stats_id and (
            players := _parse_team_tables(
                soup.find("div", id=f"{map_info.stats_id}-content"),
                re.compile(r"stats-table|totalstats"),
            )
        ):
            map_stats_details[map_info.map_name] = players

    return MatchStats(
        match_id=match_id,
        team1=team1,
        team2=team2,
        score1=score1,
        score2=score2,
        status=status,
        maps=maps,
        players=_parse_team_tables(soup.find("div", id="all-content"), "totalstats"),
        map_stats_details=map_stats_details,
        vetos=vetos,
        event=event_title,
        team1_logo=team1_logo,
        team2_logo=team2_logo,
        team1_country=team1_country,
        team1_country_name=team1_country_name,
        team2_country=team2_country,
        team2_country_name=team2_country_name,
        format_line=format_line,
        stage=stage,
        is_grand_final=is_grand_final,
        is_third_place=is_third_place_text(stage),
        date=match_date,
        team1_vrs=team1_vrs,
        team2_vrs=team2_vrs,
        team1_vrs_forecast=team1_vrs_forecast,
        team2_vrs_forecast=team2_vrs_forecast,
        vrs_invites=_parse_vrs_invites(soup),
    )


def _parse_map(map_holder: Tag) -> MapStats | None:
    """未定的图（TBA）返回 None；没打的图比分是 '-'。"""
    map_name = text_of(map_holder.find("div", class_="mapname"))
    if not map_name or map_name == "TBA":
        return None
    stats_link = map_holder.find("a", href=re.compile(r"mapstatsid/\d+"))
    left = map_holder.find(["div", "span"], class_="results-left")
    right = map_holder.find(["div", "span"], class_="results-right")

    def score(side: Tag | None) -> str:
        text = text_of(side.find("div", class_="results-team-score")) if side else ""
        return re.sub(r"[^\d-]", "", text) or "-"

    pick_by = ""
    if left and "pick" in (left.get("class") or []):
        pick_by = "team1"
    elif right and "pick" in (right.get("class") or []):
        pick_by = "team2"
    minimap = map_holder.find("img", class_="minimap")
    m_stats = (
        re.search(r"mapstatsid/(\d+)", stats_link["href"]) if stats_link else None
    )
    return MapStats(
        map_name=map_name,
        pick_by=pick_by,
        score_team1=score(left),
        score_team2=score(right),
        stats_id=m_stats.group(1) if m_stats else "",
        halves=_parse_halves(map_holder),
        image=absolute_url(minimap.get("src", "")) if minimap else "",
    )
