# SPDX-License-Identifier: MIT (script). Output data: ODbL 1.0, see LICENSE.
"""Импорт зданий района из OpenStreetMap в чистый Luau-модуль (KC-434).

Источник: input/raw.osm (лицензия ODbL, © OpenStreetMap contributors).
Дизайн: docs/design/district-osm.md §1, §3.

Только стандартная библиотека Python, детерминированно: два прогона дают
побайтно одинаковый файл (флаг --check это проверяет).

Конвейер (design §3.2):
  1. Разбор XML, проекция lat/lon в метры от (57.180, 65.641), рамка.
  2. Здания частного сектора (building=house/detached) выбрасываются целиком —
     на их месте в KC-439 встанут «сады», не отдельные дома.
  3. Все теги имён/адресов/брендов игнорируются, в Luau не попадают.
  4. RDP-упрощение контуров (0.5 м).
  5. Этажность: тег -> «та же серия» (соседнее здание той же ширины в радиусе 150 м) ->
     площадь -> фиксированные умолчания по типу (design §3.2 п.5).
  6. overrides.csv, если заполнен, побеждает всё.
  7. Здания без тега и без соседа серии из интересных категорий уходят в photo-check.md.

Вывод: src/ReplicatedStorage/Shared/World/District/OsmBuildings.luau.
OSM id остаются только в docs/ (photo-check.md) — в Luau их нет вовсе, только
наш собственный порядковый id.
"""

import argparse
import csv
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Public map-data package layout: input/ (raw.osm you download + our corrections), data/ (output).
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
VERSION_DIR = SCRIPT_DIR  # stylua.toml lives next to this script

RAW_OSM = os.path.join(REPO_ROOT, "input", "raw.osm")
OVERRIDES_CSV = os.path.join(REPO_ROOT, "input", "overrides.csv")
PHOTO_CHECK_MD = os.path.join(REPO_ROOT, "data", "photo-check.md")
OUTPUT_LUAU = os.path.join(REPO_ROOT, "data", "OsmBuildings.luau")

REF_LAT = 57.180
REF_LON = 65.641
SCALE = 2.5  # studs на метр (design §1)

# Рамка (design §2, §3.1), метры от точки отсчёта.
FRAME_MIN_X, FRAME_MAX_X = -450.0, 800.0
FRAME_MIN_Y, FRAME_MAX_Y = -700.0, 700.0

FLOOR_HEIGHT_M = 3.0
GARAGE_HEIGHT_M = 2.7
MIN_SHED_AREA_M2 = 60.0
SAME_SERIES_RADIUS_M = 150.0
SAME_SERIES_WIDTH_TOL_M = 1.0
RDP_EPSILON_BUILDING_M = 0.5

# Приватный сектор — не строится поштучно, на его месте будут «сады» (KC-439, AreaPlan).
PRIVATE_BUILDING_TAGS = {"house", "detached"}

KIND_APT = "apt"
KIND_TWO_STORY = "twoStory"
KIND_SCHOOL = "school"
KIND_KINDERGARTEN = "kindergarten"
KIND_GARAGE_ROW = "garageRow"
KIND_SHOP = "shop"
KIND_COMMERCIAL = "commercial"
KIND_INDUSTRIAL = "industrial"
KIND_CONSTRUCTION = "construction"
KIND_YES = "yes"
KIND_OTHER = "other"

ALLOWED_KINDS = [
    KIND_APT,
    KIND_TWO_STORY,
    KIND_SCHOOL,
    KIND_KINDERGARTEN,
    KIND_GARAGE_ROW,
    KIND_SHOP,
    KIND_COMMERCIAL,
    KIND_INDUSTRIAL,
    KIND_CONSTRUCTION,
    KIND_YES,
    KIND_OTHER,
]

FACADE_PANEL = "panel"
FACADE_BRICK = "brick"
FACADE_PLASTER = "plaster"
FACADE_SIDING = "siding"
FACADE_OTHER = "other"
FACADE_MATERIALS = [FACADE_PANEL, FACADE_BRICK, FACADE_PLASTER, FACADE_SIDING, FACADE_OTHER]

ROOF_FLAT = "flat"
ROOF_PITCHED = "pitched"
ROOF_KINDS = [ROOF_FLAT, ROOF_PITCHED]

# KC-471 (district-likeness-v2.md §3): узлы входов, тип балконов/первого этажа/рисунка фасада.
ENTRANCE_STAIRCASE = "staircase"
ENTRANCE_MAIN = "main"
ENTRANCE_SHOP = "shop"
ENTRANCE_SERVICE = "service"
ENTRANCE_KINDS = [ENTRANCE_STAIRCASE, ENTRANCE_MAIN, ENTRANCE_SHOP, ENTRANCE_SERVICE]

# entrance=yes — обезличенный подъезд без уточнения; design §2 п.2 считает его в общих ~226
# узлах вместе со staircase/main, здесь относим к самому частому исходу (staircase).
# entrance=emergency (аварийный выход, 6 узлов) — не подъезд для облика, узел отбрасывается.
ENTRANCE_TAG_TO_KIND = {
    "staircase": ENTRANCE_STAIRCASE,
    "main": ENTRANCE_MAIN,
    "yes": ENTRANCE_STAIRCASE,
    "shop": ENTRANCE_SHOP,
    "service": ENTRANCE_SERVICE,
}
ENTRANCE_MAX_DIST_STUDS = 60.0  # подъезд может стоять чуть в стороне (козырёк/крыльцо)

BALCONY_NONE = "none"
BALCONY_OPEN = "open"
BALCONY_GLAZED_LOGGIA = "glazed_loggia"
BALCONY_MIXED = "mixed"
BALCONY_KINDS = [BALCONY_NONE, BALCONY_OPEN, BALCONY_GLAZED_LOGGIA, BALCONY_MIXED]

GROUND_RESIDENTIAL = "residential"
GROUND_SHOPS = "shops"
GROUND_OFFICE = "office"
GROUND_KINDS = [GROUND_RESIDENTIAL, GROUND_SHOPS, GROUND_OFFICE]

FACADE_PATTERN_PLAIN = "plain"
FACADE_PATTERN_END_CONTRAST = "end_contrast"
FACADE_PATTERN_FLOOR_BELTS = "floor_belts"
FACADE_PATTERN_WHITE_TRIM = "white_trim"
FACADE_PATTERN_BALCONY_STRIPES = "balcony_stripes"
FACADE_PATTERNS = [
    FACADE_PATTERN_PLAIN,
    FACADE_PATTERN_END_CONTRAST,
    FACADE_PATTERN_FLOOR_BELTS,
    FACADE_PATTERN_WHITE_TRIM,
    FACADE_PATTERN_BALCONY_STRIPES,
]

# amenity, при которых точка — не магазин первого этажа, а отдельный объект (design §2 п.5
# считает только «магазины первого этажа», школа/садик уже строятся отдельными зданиями).
SHOP_POINT_EXCLUDED_AMENITY = {"school", "kindergarten"}

# Палитра района (design §4.2, docs/research/district-osm/survey.csv): без реальных названий
# домов/серий, только обезличенные обмеренные по фото тона. Единственный источник цвета для
# зданий без записи в overrides.csv — «по серии» (design §4.2 строка 132).
PALETTE = {
    "brickRed": (190, 90, 55),
    "brickBeige": (210, 195, 165),
    "brickAccentWhite": (230, 225, 215),
    "brickAccentTerracotta": (190, 100, 70),
    "plasterBeige": (215, 195, 160),
    "plasterLight": (225, 222, 215),
    "panelLight": (210, 205, 195),
    "concreteGray": (150, 150, 150),
    "roofDark": (60, 60, 65),
    "roofTan": (150, 120, 85),
    "roofTile": (110, 90, 70),
}

ROOF_COLOR_NAMES = {"dark": PALETTE["roofDark"], "tan": PALETTE["roofTan"]}


def series_default_appearance(kind, levels, variety_index):
    """Облик «по серии» — без записи в overrides.csv (design §4.2: «без поправки — по серии»).
    variety_index (0/1) чередует вариант в пределах палитры района, чтобы соседние однотипные
    дома не были одинаковыми; считается из osm_id вызывающей стороной, не из random."""
    if kind == KIND_GARAGE_ROW:
        return {
            "wall": PALETTE["concreteGray"],
            "accent": None,
            "facade": FACADE_OTHER,
            "roof": ROOF_FLAT,
            "roofColor": None,
        }
    if kind in (KIND_SCHOOL, KIND_KINDERGARTEN):
        return {
            "wall": PALETTE["plasterLight"],
            "accent": None,
            "facade": FACADE_PLASTER,
            "roof": ROOF_FLAT,
            "roofColor": None,
        }
    if kind == KIND_TWO_STORY:
        return {
            "wall": PALETTE["plasterBeige"],
            "accent": None,
            "facade": FACADE_PLASTER,
            "roof": ROOF_PITCHED,
            "roofColor": PALETTE["roofTile"] if variety_index == 0 else PALETTE["roofDark"],
        }
    if levels is not None and levels >= 7:
        if variety_index == 0:
            return {
                "wall": PALETTE["panelLight"],
                "accent": None,
                "facade": FACADE_PANEL,
                "roof": ROOF_FLAT,
                "roofColor": None,
            }
        return {
            "wall": PALETTE["brickBeige"],
            "accent": PALETTE["brickAccentTerracotta"],
            "facade": FACADE_BRICK,
            "roof": ROOF_FLAT,
            "roofColor": None,
        }
    # Пятиэтажки и всё, что осталось (design §4.2: «до 2000 г. — белый силикатный/красный кирпич»).
    if variety_index == 0:
        return {
            "wall": PALETTE["brickRed"],
            "accent": PALETTE["brickAccentWhite"],
            "facade": FACADE_BRICK,
            "roof": ROOF_FLAT,
            "roofColor": None,
        }
    return {
        "wall": PALETTE["plasterBeige"],
        "accent": None,
        "facade": FACADE_PLASTER,
        "roof": ROOF_FLAT,
        "roofColor": None,
    }


def parse_rgb_field(value):
    parts = [int(p) for p in value.strip().split()]
    if len(parts) != 3:
        raise ValueError("ожидалось «R G B», получено: %r" % value)
    return tuple(parts)


def parse_override_palette(value):
    """overrides.csv `palette`: «R G B» (стена) или «R G B;R G B» (стена;акцент)."""
    value = (value or "").strip()
    if not value:
        return None, None
    fields = value.split(";")
    wall = parse_rgb_field(fields[0])
    accent = parse_rgb_field(fields[1]) if len(fields) > 1 and fields[1].strip() else None
    return wall, accent


def parse_override_roof(value):
    """overrides.csv `roof`: «flat», «pitched dark», «pitched tan», «flat R G B»,
    «pitched R G B» (KC-471: расширение — RGB прямо от фото, не только именованный тон)
    или пусто (по серии)."""
    value = (value or "").strip()
    if not value:
        return None, None
    parts = value.split()
    roof = parts[0]
    if roof not in ROOF_KINDS:
        raise ValueError("неизвестный roof в overrides.csv: %r" % value)
    roof_color = None
    if len(parts) == 2:
        roof_color = ROOF_COLOR_NAMES.get(parts[1])
        if roof_color is None:
            raise ValueError("неизвестный цвет крыши в overrides.csv: %r" % value)
    elif len(parts) == 4:
        try:
            roof_color = (int(parts[1]), int(parts[2]), int(parts[3]))
        except ValueError:
            raise ValueError("неизвестный цвет крыши (RGB) в overrides.csv: %r" % value)
        if not all(0 <= c <= 255 for c in roof_color):
            raise ValueError("цвет крыши (RGB) вне 0..255 в overrides.csv: %r" % value)
    elif len(parts) != 1:
        raise ValueError("неизвестный формат roof в overrides.csv: %r" % value)
    return roof, roof_color


def parse_rgb_optional(value):
    value = (value or "").strip()
    if not value:
        return None
    return parse_rgb_field(value)


def parse_override_entrances(value):
    """overrides.csv `entrances`: целое 0..20 (design §3) или пусто (число берётся из узлов OSM)."""
    value = (value or "").strip()
    if not value:
        return None
    if not value.lstrip("-").isdigit():
        raise ValueError("entrances в overrides.csv должно быть целым 0..20: %r" % value)
    n = int(value)
    if not (0 <= n <= 20):
        raise ValueError("entrances в overrides.csv вне диапазона 0..20: %r" % value)
    return n


def parse_override_balconies(value):
    value = (value or "").strip()
    if not value:
        return BALCONY_NONE
    if value not in BALCONY_KINDS:
        raise ValueError("неизвестный balconies в overrides.csv: %r" % value)
    return value


def parse_override_ground(value):
    value = (value or "").strip()
    if not value:
        return GROUND_RESIDENTIAL
    if value not in GROUND_KINDS:
        raise ValueError("неизвестный ground в overrides.csv: %r" % value)
    return value


def parse_override_pattern(value):
    value = (value or "").strip()
    if not value:
        return [FACADE_PATTERN_PLAIN]
    tokens = [t.strip() for t in value.split(";") if t.strip()]
    for t in tokens:
        if t not in FACADE_PATTERNS:
            raise ValueError("неизвестный pattern в overrides.csv: %r" % t)
    return tokens if tokens else [FACADE_PATTERN_PLAIN]


def with_context(osm_id, fn, *args):
    """Оборачивает ValueError парсера overrides.csv в понятное сообщение с id здания
    (требование брифа KC-471: «отвергает с понятным сообщением»)."""
    try:
        return fn(*args)
    except ValueError as exc:
        raise ValueError("здание %s: %s" % (osm_id, exc)) from exc


def variety_index_from_osm_id(osm_id):
    """Чередование варианта в пределах серии, детерминированное по osm_id (без random).
    osm_id сам в Luau не попадает (ADR-0018) — используется только как устойчивый seed."""
    if osm_id.isdigit():
        return int(osm_id) % 2
    return sum(ord(ch) for ch in osm_id) % 2


def resolve_appearance(kind, levels, osm_id, override):
    variety_index = variety_index_from_osm_id(osm_id)
    appearance = series_default_appearance(kind, levels, variety_index)
    if override:
        wall, accent = parse_override_palette(override.get("palette", ""))
        if wall is not None:
            appearance["wall"] = wall
            appearance["accent"] = accent
            appearance["photoColor"] = True
        facade = (override.get("facade") or "").strip()
        if facade:
            if facade not in FACADE_MATERIALS:
                raise ValueError("неизвестный facade в overrides.csv: %r" % facade)
            appearance["facade"] = facade
        roof, roof_color = parse_override_roof(override.get("roof", ""))
        if roof is not None:
            appearance["roof"] = roof
            appearance["roofColor"] = roof_color
    return appearance


RESIDENTIAL_BUILDING_TAGS = {"apartments", "residential", "dormitory"}
GARAGE_BUILDING_TAGS = {"garages", "garage", "carport"}
# KC-472: число боксов в ряду — capacity из OSM, иначе по длине фасада, шаг 8 studs
# (design district-likeness-v2.md §2 п.6: «ворота на каждый бокс, шаг 8 studs»).
# Верхний потолок 24 на ряд — дело GaragePlan (KC-478), не импортёра: здесь только сырое число.
GARAGE_BOX_SPAN_STUDS = 8.0


def garage_box_count(tags, footprint):
    """tags: теги way. footprint: контур в studs (после RDP). capacity побеждает, если это
    положительное целое; иначе длина = длинная сторона bbox контура, боксов = floor(L/8), не менее 1."""
    cap_raw = tags.get("capacity")
    if cap_raw is not None:
        try:
            cap = int(cap_raw)
        except ValueError:
            cap = None
        if cap is not None and cap > 0:
            return cap
    xs = [p[0] for p in footprint]
    ys = [p[1] for p in footprint]
    length = max(max(xs) - min(xs), max(ys) - min(ys))
    return max(1, int(length // GARAGE_BOX_SPAN_STUDS))
SCHOOL_BUILDING_TAGS = {"school"}
KINDERGARTEN_BUILDING_TAGS = {"kindergarten"}
SHOP_BUILDING_TAGS = {"retail", "supermarket"}
COMMERCIAL_BUILDING_TAGS = {"commercial"}
INDUSTRIAL_BUILDING_TAGS = {"industrial", "warehouse"}
CONSTRUCTION_BUILDING_TAGS = {"construction"}
NON_BUILDING_TAGS = {"bridge"}  # мосты в OSM иногда тегированы building=bridge — не здания


def project(lat: float, lon: float) -> tuple:
    """lat/lon (градусы) -> метры от (REF_LAT, REF_LON): x на восток, y на север."""
    lat0 = math.radians(REF_LAT)
    m_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * lat0) + 1.175 * math.cos(4 * lat0)
    m_per_deg_lon = 111412.84 * math.cos(lat0) - 93.5 * math.cos(3 * lat0)
    x = (lon - REF_LON) * m_per_deg_lon
    y = (lat - REF_LAT) * m_per_deg_lat
    return x, y


def parse_osm(path: str):
    tree = ET.parse(path)
    root = tree.getroot()
    nodes = {}
    ways = {}
    relations = []
    for child in root:
        if child.tag == "node":
            nid = child.get("id")
            nodes[nid] = (float(child.get("lat")), float(child.get("lon")))
        elif child.tag == "way":
            wid = child.get("id")
            refs = [nd.get("ref") for nd in child.findall("nd")]
            tags = {t.get("k"): t.get("v") for t in child.findall("tag")}
            ways[wid] = {"refs": refs, "tags": tags}
        elif child.tag == "relation":
            tags = {t.get("k"): t.get("v") for t in child.findall("tag")}
            members = [
                (m.get("type"), m.get("ref"), m.get("role")) for m in child.findall("member")
            ]
            relations.append({"id": child.get("id"), "tags": tags, "members": members})
    return nodes, ways, relations


def polygon_from_refs(refs, nodes):
    pts = []
    for ref in refs:
        latlon = nodes.get(ref)
        if latlon is None:
            return None
        x, y = project(*latlon)
        pts.append((x, y))
    return pts


def shoelace_area_centroid(pts):
    """pts: замкнутый или незамкнутый список (x,y) в метрах. Возвращает (area_m2, cx, cy)."""
    ring = pts if pts[0] == pts[-1] else pts + [pts[0]]
    a = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    a *= 0.5
    if abs(a) < 1e-9:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    cx /= 6 * a
    cy /= 6 * a
    return abs(a), cx, cy


def point_seg_dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    lx, ly = ax + t * dx, ay + t * dy
    return math.hypot(px - lx, py - ly)


def rdp(points, epsilon):
    if len(points) < 3:
        return points[:]
    start, end = points[0], points[-1]
    max_dist = -1.0
    index = 0
    for i in range(1, len(points) - 1):
        d = point_seg_dist(points[i], start, end)
        if d > max_dist:
            index = i
            max_dist = d
    if max_dist > epsilon:
        left = rdp(points[: index + 1], epsilon)
        right = rdp(points[index:], epsilon)
        return left[:-1] + right
    return [start, end]


def rdp_ring(ring_closed, epsilon):
    """Замкнутое кольцо (первая точка == последняя): делим на две цепочки по вершине,
    самой дальней от первой точки, упрощаем каждую отдельно, склеиваем (design §3.2 п.3:
    целое кольцо наивный RDP не упрощает)."""
    pts = ring_closed[:-1]
    n = len(pts)
    if n < 3:
        return ring_closed[:]
    far_i = max(range(1, n), key=lambda i: math.hypot(pts[i][0] - pts[0][0], pts[i][1] - pts[0][1]))
    if far_i == 0:
        return ring_closed[:]
    chain1 = pts[0 : far_i + 1]
    chain2 = pts[far_i:] + [pts[0]]
    s1 = rdp(chain1, epsilon)
    s2 = rdp(chain2, epsilon)
    merged = s1[:-1] + s2[:-1]
    if len(merged) < 3:
        return ring_closed[:]
    merged.append(merged[0])
    return merged


def convex_hull(points):
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_obb_width(points):
    """Ширина минимального ограничивающего прямоугольника (rotating calipers), метры.
    Используется только для сравнения «той же серии» (design §3.2 п.5), в вывод не идёт."""
    hull = convex_hull(points)
    n = len(hull)
    if n < 3:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(max(xs) - min(xs), max(ys) - min(ys))
    best_width = None
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        edx, edy = bx - ax, by - ay
        length = math.hypot(edx, edy)
        if length < 1e-9:
            continue
        ux, uy = edx / length, edy / length
        vx, vy = -uy, ux
        proj_u = [( (px - ax) * ux + (py - ay) * uy) for px, py in hull]
        proj_v = [((px - ax) * vx + (py - ay) * vy) for px, py in hull]
        w = max(proj_u) - min(proj_u)
        h = max(proj_v) - min(proj_v)
        short_side = min(w, h)
        if best_width is None or short_side < best_width:
            best_width = short_side
    return best_width if best_width is not None else 0.0


def classify_kind(tags):
    b = tags.get("building")
    if b in RESIDENTIAL_BUILDING_TAGS:
        levels_tag = parse_levels_tag(tags)
        if levels_tag is not None and levels_tag <= 2:
            return KIND_TWO_STORY
        return KIND_APT
    if b in GARAGE_BUILDING_TAGS:
        return KIND_GARAGE_ROW
    if b in SCHOOL_BUILDING_TAGS or tags.get("amenity") == "school":
        return KIND_SCHOOL
    if b in KINDERGARTEN_BUILDING_TAGS or tags.get("amenity") == "kindergarten":
        return KIND_KINDERGARTEN
    if b in SHOP_BUILDING_TAGS or tags.get("shop") is not None:
        return KIND_SHOP
    if b in COMMERCIAL_BUILDING_TAGS:
        return KIND_COMMERCIAL
    if b in INDUSTRIAL_BUILDING_TAGS:
        return KIND_INDUSTRIAL
    if b in CONSTRUCTION_BUILDING_TAGS:
        return KIND_CONSTRUCTION
    if b == "yes":
        return KIND_YES
    return KIND_OTHER


def parse_levels_tag(tags):
    raw = tags.get("building:levels")
    if raw is None:
        return None
    try:
        return float(raw.split(";")[0].split(",")[0])
    except ValueError:
        return None


def area_fallback_levels(area_m2):
    if area_m2 < 600.0:
        return 5.0
    if area_m2 < 1500.0:
        return 9.0
    return 10.0


# KC-492/С1 (release-check-v0.2.0-public.md, 2026-09-26): роли корпусов составного здания
# (дубликат BuildingPlan.SCHOOL_PARTS в Luau).
BUILDING_PART_ROLES = ("main", "wing", "gym")

# С1: building-parts.csv (обводка по снимку Esri) удалён — Esri разрешает обводку только для
# OSM, не для собственной геометрии. Корпуса школы теперь считаются программно из контура
# OSM самой школы: полигон здания раскладывается на прямоугольники общей декомпозицией
# ортогонального контура (decompose_orthogonal), без единого числа, снятого с картинки.
#
# Только здание SCHOOL_DECOMPOSE_OSM_ID получает корпуса — тот же охват, что раньше был
# у building-parts.csv (в файле была одна запись, на школу A района «Прудовый», id36).
# Вторая школа района (way 91762800, тоже сложный контур) корпусов не получала и не получает
# сейчас: директор ограничил разбор на корпуса одной школой, чтобы не разрастить diff за
# пределы условия С1 на здание, к которому претензии не было. Сам алгоритм при этом общий
# для любого ортогонального контура (что и требует С1), а не завязан на форму школы A.
SCHOOL_DECOMPOSE_OSM_ID = "84660123"

# Спортзал не выделен отдельным объектом/пониженным объёмом в OSM (нет соседнего
# building=school/sports рядом, нет тега уровня). Фолбэк директора (С1, «реши и объясни»):
# наименьший по площади кусок декомпозиции — спортзал, на 1 этаж ниже главного корпуса
# (не выше человеческого роста в разнице по этажу), но не ниже GYM_MIN_LEVELS.
GYM_MIN_LEVELS = 2.0
# Запас над «этажной» высотой (GYM_MIN_LEVELS * FLOOR_HEIGHT_M): без него нижний край ленты
# окон спортзала под карнизом (BuildingPlan.GYM_WINDOW_TOP_GAP_STUDS/BAND_HEIGHT) вплотную
# совпадает с линией первого этажа (FLOOR_HEIGHT_STUDS) — впритык, а не выше неё.
GYM_HEIGHT_MARGIN_M = 0.5


def _edge_length_angle_mod90(p0, p1):
    """Длина ребра и его угол по модулю 90° (для кругового среднего направления стен)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return None
    return length, math.degrees(math.atan2(dy, dx)) % 90.0


def principal_angle_deg(ring):
    """Главное направление стен контура (design-решение С1): длиной-взвешенное круговое
    среднее угла рёбер по модулю 90° — стандартный приём «удвоить угол, сложить векторы,
    поделить пополам», применим к любому (почти) ортогональному контуру, не только к школе.
    Возвращает угол в [0, 90)."""
    sx = sy = 0.0
    n = len(ring)
    for i in range(n):
        edge = _edge_length_angle_mod90(ring[i], ring[(i + 1) % n])
        if edge is None:
            continue
        length, angle = edge
        rad2 = math.radians(angle * 2.0)
        sx += length * math.cos(rad2)
        sy += length * math.sin(rad2)
    if abs(sx) < 1e-9 and abs(sy) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(sy, sx)) / 2.0 % 90.0


def _rotate_to_local(p, theta_rad, cx, cy):
    x, y = p[0] - cx, p[1] - cy
    c, s = math.cos(theta_rad), math.sin(theta_rad)
    return (x * c + y * s, -x * s + y * c)


def _rotate_to_world(p, theta_rad, cx, cy):
    c, s = math.cos(theta_rad), math.sin(theta_rad)
    x = p[0] * c - p[1] * s
    y = p[0] * s + p[1] * c
    return (x + cx, y + cy)


def _cluster_breakpoints(values, tol=0.2):
    out = []
    for v in sorted(values):
        if out and abs(v - out[-1]) <= tol:
            out[-1] = (out[-1] + v) / 2.0
        else:
            out.append(v)
    return out


def decompose_orthogonal(ring):
    """Раскладывает (почти) ортогональный контур `ring` (метры, любой порядок обхода) на
    прямоугольники общей декомпозицией — design-решение С1 (release-check-v0.2.0-public.md):
    1. Поворот к главному направлению стен (principal_angle_deg) — контур становится
       строго осеосимметричным в локальной системе.
    2. Координаты вершин дают точную сетку разбиения (без растра — по самим вершинам,
       поэтому без потери точности); каждая ячейка сетки проверяется point-in-polygon.
    3. Построчное слияние ячеек в максимальные прямоугольники: прямоугольник, начатый в
       строке r, продолжается в r+1, пока весь его диапазон столбцов остаётся закрашенным;
       иначе прямоугольник закрывается, а новые открываются на актуальных подряд идущих
       закрашенных столбцах текущей строки.
    4. Поворот прямоугольников обратно в исходную (мировую) систему координат.
    Возвращает список кусков, каждый — 4 угла (x, y) в порядке обхода локального
    прямоугольника (после поворота обратно). Сумма площадей кусков равна площади контура
    (проверяет osm_selftest.py) — декомпозиция точная, без наложений и дыр."""
    theta = math.radians(principal_angle_deg(ring))
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    local = [_rotate_to_local(p, theta, cx, cy) for p in ring]
    xs = _cluster_breakpoints([p[0] for p in local])
    ys = _cluster_breakpoints([p[1] for p in local])
    nx, ny = len(xs) - 1, len(ys) - 1
    grid = []
    for r in range(ny):
        mid_y = (ys[r] + ys[r + 1]) / 2.0
        row = [point_in_polygon(((xs[c] + xs[c + 1]) / 2.0, mid_y), local) for c in range(nx)]
        grid.append(row)

    rects_local = []
    active = []  # список {"cs": col0, "ce": col1, "rs": row_start}
    for r in range(ny):
        row = grid[r]
        covered = [False] * nx
        next_active = []
        for a in active:
            lo, hi, rs = a["cs"], a["ce"], a["rs"]
            if all(row[c] for c in range(lo, hi + 1)):
                next_active.append(a)
                for c in range(lo, hi + 1):
                    covered[c] = True
                continue
            if r - 1 >= rs:
                rects_local.append((xs[lo], ys[rs], xs[hi + 1], ys[r]))
            c = lo
            while c <= hi:
                if row[c]:
                    s = c
                    while c <= hi and row[c]:
                        covered[c] = True
                        c += 1
                    next_active.append({"cs": s, "ce": c - 1, "rs": r})
                else:
                    c += 1
        c = 0
        while c < nx:
            if row[c] and not covered[c]:
                s = c
                while c < nx and row[c] and not covered[c]:
                    c += 1
                next_active.append({"cs": s, "ce": c - 1, "rs": r})
            else:
                c += 1
        active = next_active
    for a in active:
        rects_local.append((xs[a["cs"]], ys[a["rs"]], xs[a["ce"] + 1], ys[ny]))

    rects_world = []
    for x0, y0, x1, y1 in rects_local:
        corners_local = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        rects_world.append([_rotate_to_world(p, theta, cx, cy) for p in corners_local])
    return rects_world


def build_school_corpuses(raw, orig):
    """raw: запись `kept` того же здания (raw["pts"] — контур в метрах, до RDP/studs).
    orig: уже посчитанная запись final_buildings для этого здания (даёт облик «по серии» —
    единственный законный источник после С1, фотографии Esri в код не идут). Роли:
    главный корпус — прямоугольник с самой длинной стороной (design-решение С1: «главный
    корпус — самый длинный»); спортзал — см. GYM_MIN_LEVELS; остальное — крылья."""
    rects = decompose_orthogonal(raw["pts"])
    base_levels = raw["levels_tag"] if raw["levels_tag"] is not None else orig["levels"]

    infos = []
    for poly in rects:
        sides = [math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1]) for i in range(len(poly))]
        area_m2, cx_m, cy_m = shoelace_area_centroid(poly)
        infos.append({"poly": poly, "longest": max(sides), "area": area_m2, "centroid_m": (cx_m, cy_m)})

    main_i = max(range(len(infos)), key=lambda i: infos[i]["longest"])
    gym_i = None
    if len(infos) > 1:
        rest = [i for i in range(len(infos)) if i != main_i]
        gym_i = min(rest, key=lambda i: infos[i]["area"])

    records = []
    for i, info in enumerate(infos):
        role = "main" if i == main_i else ("gym" if i == gym_i else "wing")
        if role == "gym":
            levels = max(GYM_MIN_LEVELS, base_levels - 1.0)
            height_m = levels * FLOOR_HEIGHT_M + GYM_HEIGHT_MARGIN_M
        else:
            levels = base_levels
            height_m = levels * FLOOR_HEIGHT_M
        footprint = [(round(x * SCALE, 1), round(y * SCALE, 1)) for x, y in info["poly"]]
        cx_m, cy_m = info["centroid_m"]
        record = dict(orig)
        record.update(
            {
                "levels": levels,
                "height": round(height_m * SCALE, 2),
                "centroid": (round(cx_m * SCALE, 1), round(cy_m * SCALE, 1)),
                "footprint": footprint,
                "levels_source": "geometry",
                "boxCount": None,
                "part": role,
                "part_entrance_edge": None,
            }
        )
        if role == "main":
            # Вход — сторона главного корпуса, обращённая к югу (design: у школы A это и
            # есть сторона к улице), не зависящая от угла поворота здания: минимум средней
            # y ребра в мировых координатах среди 4 сторон прямоугольника.
            n = len(info["poly"])
            record["part_entrance_edge"] = min(
                range(n), key=lambda e: (info["poly"][e][1] + info["poly"][(e + 1) % n][1]) / 2.0
            )
        records.append(record)
    records.sort(key=lambda r: 0 if r["part"] == "main" else 1)
    return records


def apply_school_corpuses(final_buildings, kept_by_id):
    """Заменяет одиночную запись школы (SCHOOL_DECOMPOSE_OSM_ID) в final_buildings на корпуса
    из build_school_corpuses: главный корпус — на месте исходного здания (id не сдвигается),
    остальные — в конец списка (как раньше делал apply_building_parts с CSV)."""
    raw = kept_by_id.get(SCHOOL_DECOMPOSE_OSM_ID)
    if raw is None:
        return final_buildings
    kept = []
    appended = []
    replaced = False
    for b in final_buildings:
        if not replaced and b["osm_id"] == SCHOOL_DECOMPOSE_OSM_ID:
            records = build_school_corpuses(raw, b)
            kept.append(records[0])
            appended.extend(records[1:])
            replaced = True
        else:
            kept.append(b)
    return kept + appended


def assign_part_entrances(final_buildings):
    """Вход корпуса — середина ребра entrance_edge (0-based, как у entrance=* из OSM)."""
    for b in final_buildings:
        edge = b.get("part_entrance_edge")
        if edge is None:
            continue
        fp = b["footprint"]
        (ax, ay), (bx, by) = fp[edge], fp[(edge + 1) % len(fp)]
        b["entrances"].append(
            {"x": round((ax + bx) / 2, 1), "y": round((ay + by) / 2, 1), "edge": edge, "kind": "main"}
        )


def load_overrides(path):
    overrides = {}
    if not os.path.exists(path):
        return overrides
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            osm_id = row.get("osm_id", "").strip()
            if not osm_id:
                continue
            overrides[osm_id] = row
    return overrides


def ensure_overrides_file(path):
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("osm_id,levels,roof,palette,facade,note,entrances,balconies,ground,pattern,end_rgb,balcony_rgb\n")


_NODE_TAGS_CACHE = None


def collect_node_tags():
    """Теги узлов raw.osm по id (KC-471): второй проход по файлу, как collect_tree_nodes —
    parse_osm тегов узлов не хранит, а entrance=*/shop=*/amenity=* сидят именно на node."""
    global _NODE_TAGS_CACHE
    if _NODE_TAGS_CACHE is not None:
        return _NODE_TAGS_CACHE
    tree = ET.parse(RAW_OSM)
    root = tree.getroot()
    out = {}
    for child in root:
        if child.tag != "node":
            continue
        tags = {t.get("k"): t.get("v") for t in child.findall("tag")}
        if tags:
            out[child.get("id")] = tags
    _NODE_TAGS_CACHE = out
    return out


def collect_entrance_positions(nodes, node_tags):
    """entrance=* узлы (design §2 п.2/§3): [(latlon, kind)]. emergency и незнакомые значения
    entrance не подъезды для облика и отбрасываются здесь же."""
    out = []
    for nid, tags in node_tags.items():
        value = tags.get("entrance")
        if value is None:
            continue
        kind = ENTRANCE_TAG_TO_KIND.get(value)
        if kind is None:
            continue
        latlon = nodes.get(nid)
        if latlon is None:
            continue
        out.append((latlon, kind))
    return out


def collect_shop_points(nodes, node_tags):
    """Число точек shop=*/amenity=* (design §2 п.5) — только счёт, без имён и брендов."""
    out = []
    for nid, tags in node_tags.items():
        amenity = tags.get("amenity")
        if "shop" not in tags and (amenity is None or amenity in SHOP_POINT_EXCLUDED_AMENITY):
            continue
        latlon = nodes.get(nid)
        if latlon is None:
            continue
        out.append(latlon)
    return out


def nearest_edge_projection(point, footprint):
    """point, footprint: studs. Возвращает (dist, edge_index, (px, py)) — ближайшая точка на
    ребре footprint[i]->footprint[i+1] (замыкая контур), design §3: «ребро контура, позиция
    вдоль ребра»."""
    n = len(footprint)
    best = None
    for i in range(n):
        ax, ay = footprint[i]
        bx, by = footprint[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 < 1e-9:
            t = 0.0
        else:
            t = ((point[0] - ax) * dx + (point[1] - ay) * dy) / length2
            t = max(0.0, min(1.0, t))
        px, py = ax + t * dx, ay + t * dy
        d = math.hypot(point[0] - px, point[1] - py)
        if best is None or d < best[0]:
            best = (d, i, (round(px, 1), round(py, 1)))
    return best


def assign_entrances(final_buildings, entrance_positions):
    """Привязка узлов entrance=* к ближайшей стене ближайшего здания (design §3, KC-471):
    здание выбирается по минимальному расстоянию от узла до любого ребра его контура."""
    for b in final_buildings:
        b["entrances"] = []
    for latlon, kind in entrance_positions:
        mx, my = project(*latlon)
        point = (round(mx * SCALE, 1), round(my * SCALE, 1))
        best_building = None
        best = None
        for b in final_buildings:
            fp = b["footprint"]
            if len(fp) < 3:
                continue
            result = nearest_edge_projection(point, fp)
            if best is None or result[0] < best[0]:
                best = result
                best_building = b
        if best_building is None or best[0] > ENTRANCE_MAX_DIST_STUDS:
            continue
        _, edge_index, proj_point = best
        best_building["entrances"].append(
            {"x": proj_point[0], "y": proj_point[1], "edge": edge_index, "kind": kind}
        )


def assign_shop_counts(final_buildings, shop_points):
    """Число точек-магазинов внутри контура здания (design §2 п.5): point-in-polygon по
    финальному footprint (studs)."""
    for b in final_buildings:
        b["shopCount"] = 0
    for latlon in shop_points:
        mx, my = project(*latlon)
        point = (round(mx * SCALE, 1), round(my * SCALE, 1))
        for b in final_buildings:
            fp = b["footprint"]
            if len(fp) < 3:
                continue
            if point_in_polygon(point, fp):
                b["shopCount"] += 1
                break


def collect_buildings(nodes, ways, relations):
    """Возвращает список сырых кандидатов (до фильтра рамки и приватного сектора)."""
    candidates = []

    for wid, way in ways.items():
        tags = way["tags"]
        if "building" not in tags:
            continue
        refs = way["refs"]
        if len(refs) < 4 or refs[0] != refs[-1]:
            continue
        pts = polygon_from_refs(refs, nodes)
        if pts is None:
            continue
        candidates.append((wid, tags, pts))

    # Две relation type=multipolygon building=* (§ разведка): берём самый длинный outer way.
    way_by_id = ways
    for rel in relations:
        tags = rel["tags"]
        if "building" not in tags:
            continue
        outer_refs = [ref for (t, ref, role) in rel["members"] if t == "way" and role == "outer"]
        best = None
        for ref in outer_refs:
            way = way_by_id.get(ref)
            if way is None:
                continue
            if best is None or len(way["refs"]) > len(way_by_id[best]["refs"]):
                best = ref
        if best is None:
            continue
        refs = way_by_id[best]["refs"]
        if len(refs) < 4 or refs[0] != refs[-1]:
            continue
        pts = polygon_from_refs(refs, nodes)
        if pts is None:
            continue
        # Настоящий id отношения OSM, а не id() объекта Python: адрес в памяти меняется от запуска
        # к запуску, и --check падал через раз (вариант облика зависит от id).
        candidates.append(("rel" + str(rel["id"]), tags, pts))

    return candidates


def build_dataset():
    nodes, ways, relations = parse_osm(RAW_OSM)
    candidates = collect_buildings(nodes, ways, relations)

    kept = []
    skipped_private = 0
    skipped_out_of_frame = 0
    skipped_small_shed = 0
    skipped_degenerate = 0

    for osm_id, tags, pts in candidates:
        b = tags.get("building")
        if b in PRIVATE_BUILDING_TAGS or b in NON_BUILDING_TAGS:
            skipped_private += 1
            continue
        area_m2, cx, cy = shoelace_area_centroid(pts)
        if area_m2 <= 0.0 or len(pts) < 4:
            skipped_degenerate += 1
            continue
        if not (FRAME_MIN_X <= cx <= FRAME_MAX_X and FRAME_MIN_Y <= cy <= FRAME_MAX_Y):
            skipped_out_of_frame += 1
            continue
        kind = classify_kind(tags)
        # Мелкие постройки (сараи, будки) — не здания-ориентиры; design §3.2 п.5 явно исключает
        # сараи < 60 м² у гаражей, здесь распространяем на любую мелочь той же природы (yes/other).
        if kind in (KIND_GARAGE_ROW, KIND_YES, KIND_OTHER) and area_m2 < MIN_SHED_AREA_M2:
            skipped_small_shed += 1
            continue
        levels_tag = parse_levels_tag(tags)
        width = min_obb_width(pts) if kind in (KIND_APT, KIND_TWO_STORY) else None
        kept.append(
            {
                "osm_id": osm_id,
                "tags": tags,
                "pts": pts,
                "area_m2": area_m2,
                "centroid_m": (cx, cy),
                "kind": kind,
                "levels_tag": levels_tag,
                "width_m": width,
            }
        )

    # "Та же серия": среди apt/twoStory с известным тегом ищем ближайшего по ширине (±1м, 150м).
    known_series = [b for b in kept if b["kind"] in (KIND_APT, KIND_TWO_STORY) and b["levels_tag"] is not None]

    overrides = load_overrides(OVERRIDES_CSV)

    photo_check = []
    final_buildings = []
    for b in kept:
        levels_source = "tag"
        override = overrides.get(b["osm_id"])
        if override and override.get("levels", "").strip():
            levels = float(override["levels"])
            levels_source = "override"
        elif b["levels_tag"] is not None:
            levels = b["levels_tag"]
            levels_source = "tag"
        elif b["kind"] in (KIND_APT, KIND_TWO_STORY):
            match = None
            match_dist = None
            for other in known_series:
                if other is b:
                    continue
                if other["width_m"] is None or b["width_m"] is None:
                    continue
                if abs(other["width_m"] - b["width_m"]) > SAME_SERIES_WIDTH_TOL_M:
                    continue
                dist = math.hypot(
                    other["centroid_m"][0] - b["centroid_m"][0],
                    other["centroid_m"][1] - b["centroid_m"][1],
                )
                if dist > SAME_SERIES_RADIUS_M:
                    continue
                if match_dist is None or dist < match_dist:
                    match = other
                    match_dist = dist
            if match is not None:
                levels = match["levels_tag"]
                levels_source = "series"
            else:
                levels = area_fallback_levels(b["area_m2"])
                levels_source = "area"
        elif b["kind"] == KIND_GARAGE_ROW:
            levels = 1.0
            levels_source = "fixed"
        elif b["kind"] in (KIND_SCHOOL,):
            levels = 3.0
            levels_source = "fixed"
        elif b["kind"] == KIND_KINDERGARTEN:
            levels = 2.0
            levels_source = "fixed"
        elif b["kind"] == KIND_YES:
            if b["area_m2"] < 300.0:
                levels = 1.0
                levels_source = "fixed"
            else:
                levels = area_fallback_levels(b["area_m2"])
                levels_source = "area"
        else:
            levels = 2.0
            levels_source = "fixed"

        if b["kind"] == KIND_GARAGE_ROW:
            height_m = GARAGE_HEIGHT_M
        else:
            height_m = levels * FLOOR_HEIGHT_M

        if levels_source in ("series", "area") and b["kind"] in (KIND_APT, KIND_TWO_STORY, KIND_YES, KIND_SHOP):
            photo_check.append((b, levels, levels_source))

        # RDP-упрощение в метрах, затем перевод в studs.
        ring = b["pts"]
        simplified = rdp_ring(ring, RDP_EPSILON_BUILDING_M)
        footprint = [(round(x * SCALE, 1), round(y * SCALE, 1)) for x, y in simplified[:-1]]
        cx_m, cy_m = b["centroid_m"]
        centroid = (round(cx_m * SCALE, 1), round(cy_m * SCALE, 1))

        appearance = resolve_appearance(b["kind"], levels, b["osm_id"], override)

        final_buildings.append(
            {
                "kind": b["kind"],
                "levels": levels,
                "height": round(height_m * SCALE, 2),
                "centroid": centroid,
                "footprint": footprint,
                "osm_id": b["osm_id"],
                "tags": b["tags"],
                "levels_source": levels_source,
                "wall": appearance["wall"],
                "accent": appearance["accent"],
                "facade": appearance["facade"],
                "roof": appearance["roof"],
                "roofColor": appearance["roofColor"],
                "photoColor": appearance.get("photoColor", False),
                "boxCount": garage_box_count(b["tags"], footprint) if b["kind"] == KIND_GARAGE_ROW else None,
            }
        )

    # KC-471: узлы entrance=* -> ближайшая стена ближайшего здания; точки shop/amenity -> счёт
    # магазинов первого этажа; balconies/ground/pattern/entrances/end_rgb/balcony_rgb — из
    # overrides.csv, каждый недопустимый токен отвергается с id здания в сообщении.
    # С1: корпуса школы — не из CSV, а из декомпозиции контура (apply_school_corpuses).
    kept_by_id = {b["osm_id"]: b for b in kept}
    final_buildings = apply_school_corpuses(final_buildings, kept_by_id)
    node_tags = collect_node_tags()
    assign_entrances(final_buildings, collect_entrance_positions(nodes, node_tags))
    assign_part_entrances(final_buildings)
    assign_shop_counts(final_buildings, collect_shop_points(nodes, node_tags))
    for b in final_buildings:
        override = overrides.get(b["osm_id"])
        entrances_raw = override.get("entrances") if override else None
        entrances_override = with_context(b["osm_id"], parse_override_entrances, entrances_raw or "")
        b["entranceCount"] = entrances_override if entrances_override is not None else len(b["entrances"])
        b["balconies"] = with_context(
            b["osm_id"], parse_override_balconies, (override.get("balconies") if override else None) or ""
        )
        b["ground"] = with_context(
            b["osm_id"], parse_override_ground, (override.get("ground") if override else None) or ""
        )
        b["pattern"] = with_context(
            b["osm_id"], parse_override_pattern, (override.get("pattern") if override else None) or ""
        )
        b["endColor"] = with_context(
            b["osm_id"], parse_rgb_optional, (override.get("end_rgb") if override else None) or ""
        )
        b["balconyColor"] = with_context(
            b["osm_id"], parse_rgb_optional, (override.get("balcony_rgb") if override else None) or ""
        )
        b["appearanceSource"] = "photo" if b["photoColor"] else "series"

    stats = {
        "candidates": len(candidates),
        "kept": len(final_buildings),
        "skipped_private": skipped_private,
        "skipped_out_of_frame": skipped_out_of_frame,
        "skipped_small_shed": skipped_small_shed,
        "skipped_degenerate": skipped_degenerate,
    }
    return final_buildings, photo_check, stats, nodes


OUTPUT_ROADS_LUAU = os.path.join(REPO_ROOT, "data", "OsmRoads.luau")
OUTPUT_AREAS_LUAU = os.path.join(REPO_ROOT, "data", "OsmAreas.luau")

RDP_EPSILON_LINE_M = 1.0  # design §3.2 п.3: 1.0 м для дорог и площадей

# Ширина дороги по классу (design §1: primary 40, tertiary 28, residential 22, service 14).
# secondary и livingStreet не расписаны в дизайне явно — оценены по месту в иерархии
# (между primary и tertiary; между residential и service) и отмечены в отчёте KC-435.
ROAD_CLASS_PRIMARY = "primary"
ROAD_CLASS_SECONDARY = "secondary"
ROAD_CLASS_TERTIARY = "tertiary"
ROAD_CLASS_RESIDENTIAL = "residential"
ROAD_CLASS_LIVING_STREET = "livingStreet"
ROAD_CLASS_SERVICE = "service"
ROAD_CLASSES = [
    ROAD_CLASS_PRIMARY,
    ROAD_CLASS_SECONDARY,
    ROAD_CLASS_TERTIARY,
    ROAD_CLASS_RESIDENTIAL,
    ROAD_CLASS_LIVING_STREET,
    ROAD_CLASS_SERVICE,
]
ROAD_CLASS_WIDTH_STUDS = {
    ROAD_CLASS_PRIMARY: 40.0,
    ROAD_CLASS_SECONDARY: 34.0,
    ROAD_CLASS_TERTIARY: 28.0,
    ROAD_CLASS_RESIDENTIAL: 22.0,
    ROAD_CLASS_LIVING_STREET: 18.0,
    ROAD_CLASS_SERVICE: 14.0,
}
# highway=* -> наш класс. proposed (дорога не построена) и raceway (гоночная трасса
# OSM, не проезд) выбрасываются из графа целиком (design §3.2 п.7 — только явные дороги).
HIGHWAY_TO_ROAD_CLASS = {
    "primary": ROAD_CLASS_PRIMARY,
    "secondary": ROAD_CLASS_SECONDARY,
    "secondary_link": ROAD_CLASS_SECONDARY,
    "tertiary": ROAD_CLASS_TERTIARY,
    "residential": ROAD_CLASS_RESIDENTIAL,
    "unclassified": ROAD_CLASS_RESIDENTIAL,
    "living_street": ROAD_CLASS_LIVING_STREET,
    "service": ROAD_CLASS_SERVICE,
    "track": ROAD_CLASS_SERVICE,
}
HIGHWAY_SKIPPED = {"proposed", "raceway"}
SIDEWALK_HIGHWAY_TAGS = {"footway", "path", "cycleway"}
SIDEWALK_WIDTH_STUDS = 6.0

ROAD_MIN_WIDTH_STUDS = 12.0
ROAD_WALL_CLEARANCE_STUDS = 2.0
ROAD_JUNCTION_MERGE_ANGLE_DEG = 3.0
ROAD_SEARCH_MARGIN_STUDS = 40.0
ENTRY_TARGET_Y_M = -530.0  # design §2: «OSM primary пересекает рамку при y = −530»

# KC-438b: проезжаемость графа. Осевая OSM местами идёт вплотную к стене или сквозь неё
# (дворовые проезды у гаражей), и GroundPlan, обрезая асфальт по зазору, давал коридоры
# шириной 0..6 studs — машина трафика упиралась в дыру или стену. Решение — в данных:
# сдвинуть осевую от стены (не дальше DRIVABLE_SHIFT_MAX_STUDS), а если сдвиг не помогает —
# удалить ребро, только если это не рвёт связность от въезда. Арки (tunnel=building_passage)
# помечаются флагом passage и не сдвигаются: асфальт идёт сквозь здание, вырез — BuildingPlan.
PASSAGE_TUNNEL_TAG = "building_passage"
DRIVABLE_HALF_STUDS = 3.5  # кузов 6 studs (VehicleService BASE_BODY_SIZE) + по 0.5 с боков
DRIVABLE_WALL_GAP_STUDS = 0.5  # от края проезжего коридора до стены
DRIVABLE_CLEARANCE_STUDS = DRIVABLE_HALF_STUDS + DRIVABLE_WALL_GAP_STUDS  # от осевой до стены
DRIVABLE_SHIFT_MAX_STUDS = 4.0
DRIVABLE_SHIFT_STEP_STUDS = 0.1  # шаг = точность координат в данных
DRIVABLE_FIX_PASSES = 20
DRIVABLE_PIN_EPS_STUDS = 0.05
PASSAGE_TOUCH_EPS_STUDS = 0.05
FRAME_EDGE_EPS_M = 0.01

AREA_KIND_PARKING = "parking"
AREA_KIND_PLAYGROUND = "playground"
AREA_KIND_PITCH = "pitch"
AREA_KIND_TRACK = "track"
AREA_KIND_GRASS = "grass"
AREA_KIND_FOREST = "forest"
AREA_KIND_WATER = "water"
AREA_KIND_SCHOOL_GROUNDS = "schoolGrounds"
AREA_POLY_KINDS = [
    AREA_KIND_PARKING,
    AREA_KIND_PLAYGROUND,
    AREA_KIND_PITCH,
    AREA_KIND_TRACK,
    AREA_KIND_GRASS,
    AREA_KIND_FOREST,
    AREA_KIND_WATER,
    AREA_KIND_SCHOOL_GROUNDS,
]
AREA_KIND_FENCE = "fence"
AREA_KIND_TREE_ROW = "treeRow"
AREA_LINE_KINDS = [AREA_KIND_FENCE, AREA_KIND_TREE_ROW]
AREA_KIND_TREE = "tree"
AREA_KIND_GATE = "gate"
AREA_KIND_EQUIPMENT = "equipment"
AREA_POINT_KINDS = [AREA_KIND_TREE, AREA_KIND_GATE, AREA_KIND_EQUIPMENT]
AREA_ALL_KINDS = AREA_POLY_KINDS + AREA_LINE_KINDS + AREA_POINT_KINDS

# KC-472: ворота на линиях заборов (design §2 п.8). barrier=gate/lift_gate — узлы, лежащие
# прямо на way забора (design §3: «на линии забора»); значение попадает в поле points[].detail.
GATE_KIND_GATE = "gate"
GATE_KIND_LIFT = "liftGate"
GATE_KINDS = [GATE_KIND_GATE, GATE_KIND_LIFT]
GATE_TAG_TO_KIND = {"gate": GATE_KIND_GATE, "lift_gate": GATE_KIND_LIFT}

# KC-472: тип парковки (design §2 п.7 расширен брифом директора 2026-09-25: только
# surface/multiStorey/underground — lane и street_side из OSM в этой тройке нет, они
# обозначают придорожные места, а не площадку, и сводятся к surface как более частому случаю).
PARKING_KIND_SURFACE = "surface"
PARKING_KIND_MULTI_STOREY = "multiStorey"
PARKING_KIND_UNDERGROUND = "underground"
PARKING_KINDS = [PARKING_KIND_SURFACE, PARKING_KIND_MULTI_STOREY, PARKING_KIND_UNDERGROUND]
PARKING_TAG_TO_KIND = {
    "surface": PARKING_KIND_SURFACE,
    "multi-storey": PARKING_KIND_MULTI_STOREY,
    "underground": PARKING_KIND_UNDERGROUND,
}

# KC-472: снаряды детских площадок, playground=* на узлах (design §2 п.9). Неизвестное
# значение тега (найдётся в будущей выгрузке OSM) -> "other", а не отбрасывается молча.
EQUIPMENT_SLIDE = "slide"
EQUIPMENT_SWING = "swing"
EQUIPMENT_SEESAW = "seesaw"
EQUIPMENT_SANDPIT = "sandpit"
EQUIPMENT_CLIMBING_FRAME = "climbingframe"
EQUIPMENT_SPRING = "springy"
EQUIPMENT_ZIPWIRE = "zipwire"
EQUIPMENT_STRUCTURE = "structure"
EQUIPMENT_OTHER = "other"
EQUIPMENT_KINDS = [
    EQUIPMENT_SLIDE,
    EQUIPMENT_SWING,
    EQUIPMENT_SEESAW,
    EQUIPMENT_SANDPIT,
    EQUIPMENT_CLIMBING_FRAME,
    EQUIPMENT_SPRING,
    EQUIPMENT_ZIPWIRE,
    EQUIPMENT_STRUCTURE,
    EQUIPMENT_OTHER,
]
EQUIPMENT_KNOWN_TAGS = set(EQUIPMENT_KINDS) - {EQUIPMENT_OTHER}

# KC-472: спортивные площадки, sport=* на полигонах leisure=pitch (design §2 п.9, расширено
# брифом директора). Неизвестное значение -> "other".
SPORT_TAG_TO_KIND = {
    "soccer": "soccer",
    "basketball": "basketball",
    "tennis": "tennis",
    "athletics": "athletics",
    "fitness": "fitness",
    "ice_hockey": "iceHockey",
    "karting": "karting",
    "running": "running",
    "volleyball": "volleyball",
}
SPORT_KIND_OTHER = "other"
SPORT_KINDS = sorted(set(SPORT_TAG_TO_KIND.values())) + [SPORT_KIND_OTHER]

GATE_MAX_DIST_STUDS = 1.0  # узел ворот — вершина той же way; допуск только на округление 0.1


def parse_parking_kind(value):
    return PARKING_TAG_TO_KIND.get(value, PARKING_KIND_SURFACE)


def parse_area_capacity(value):
    if value is None:
        return None
    try:
        n = int(value)
    except ValueError:
        return None
    return n if n > 0 else None


def parse_sport_kind(value):
    return SPORT_TAG_TO_KIND.get(value, SPORT_KIND_OTHER)


def parse_equipment_kind(value):
    return value if value in EQUIPMENT_KNOWN_TAGS else EQUIPMENT_OTHER


def collect_equipment_nodes(nodes, node_tags):
    """playground=* узлы (design §2 п.9): всего число, без привязки к конкретной площадке —
    в исходных данных их 6, отдельный точечный слой, как деревья."""
    out = []
    for nid, tags in node_tags.items():
        value = tags.get("playground")
        if value is None:
            continue
        latlon = nodes.get(nid)
        if latlon is None:
            continue
        out.append((latlon, parse_equipment_kind(value)))
    return out


def liang_barsky_clip(p0, p1, xmin, xmax, ymin, ymax):
    """Отрезок p0->p1, отсечённый прямоугольником. None, если отрезок целиком снаружи."""
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    checks = ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0))
    for p, q in checks:
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return None
            if t < t1:
                t1 = t
    if t0 > t1:
        return None
    return (x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy)


def clip_polyline(points, xmin, xmax, ymin, ymax):
    """Режет полилинию по рамке (design §3.2 п.1: «дороги режутся по ней»). Возвращает
    список цепочек (кусков внутри рамки); излом на границе рамки — новая, не-узловая точка."""

    def inside(p):
        return xmin <= p[0] <= xmax and ymin <= p[1] <= ymax

    chains = []
    current = []
    prev = None
    prev_in = None
    for p in points:
        cur_in = inside(p)
        if prev is not None and prev_in != cur_in:
            clipped = liang_barsky_clip(prev, p, xmin, xmax, ymin, ymax)
            if clipped is not None:
                c0, c1 = clipped
                if prev_in:
                    current.append(c1)
                    chains.append(current)
                    current = []
                else:
                    current = [c0]
        if cur_in:
            current.append(p)
        prev, prev_in = p, cur_in
    if len(current) >= 2:
        chains.append(current)
    return [c for c in chains if len(c) >= 2]


def rdp_protected(points, epsilon, protected_coords):
    """RDP, который никогда не убирает точки из protected_coords (узлы, общие с другим way —
    design §3.2 п.7: граф должен остаться связным после упрощения одного way)."""
    protected = {0, len(points) - 1}
    for i, p in enumerate(points):
        if p in protected_coords:
            protected.add(i)
    protected = sorted(protected)
    result = []
    for i in range(len(protected) - 1):
        a, b = protected[i], protected[i + 1]
        chunk = points[a : b + 1]
        simplified = rdp(chunk, epsilon)
        if result:
            result.extend(simplified[1:])
        else:
            result.extend(simplified)
    return result


def orientation(p, q, r):
    val = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(val) < 1e-9:
        return 0
    return 1 if val > 0 else 2


def on_segment(p, q, r):
    return (
        min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
        and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
    )


def segments_intersect(p1, p2, p3, p4):
    o1, o2 = orientation(p1, p2, p3), orientation(p1, p2, p4)
    o3, o4 = orientation(p3, p4, p1), orientation(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_segment(p1, p3, p2):
        return True
    if o2 == 0 and on_segment(p1, p4, p2):
        return True
    if o3 == 0 and on_segment(p3, p1, p4):
        return True
    if o4 == 0 and on_segment(p3, p2, p4):
        return True
    return False


def seg_seg_distance(a1, a2, b1, b2):
    if segments_intersect(a1, a2, b1, b2):
        return 0.0
    return min(
        point_seg_dist(a1, b1, b2),
        point_seg_dist(a2, b1, b2),
        point_seg_dist(b1, a1, a2),
        point_seg_dist(b2, a1, a2),
    )


def bbox_of(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), max(xs), min(ys), max(ys)


def bbox_overlap(a, b):
    return a[0] <= b[1] and b[0] <= a[1] and a[2] <= b[3] and b[2] <= a[3]


def clip_polygon_to_bbox(points, xmin, xmax, ymin, ymax):
    """Sutherland-Hodgman: режет полигон (без замыкающей точки) по прямоугольнику. Нужен для
    крупных площадей (парковки, парки), которые крупнее здания и центроидом внутри рамки не
    удержать целиком (design §3.2 п.1 явно режет только дороги, здесь распространяем тот же
    приём на площади, которые обнаружились за рамкой на разведке)."""

    def clip_edge(pts, inside, intersect):
        if not pts:
            return []
        out = []
        prev = pts[-1]
        prev_in = inside(prev)
        for cur in pts:
            cur_in = inside(cur)
            if cur_in:
                if not prev_in:
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(intersect(prev, cur))
            prev, prev_in = cur, cur_in
        return out

    def inter_x(p1, p2, x):
        t = (x - p1[0]) / (p2[0] - p1[0])
        return (x, p1[1] + t * (p2[1] - p1[1]))

    def inter_y(p1, p2, y):
        t = (y - p1[1]) / (p2[1] - p1[1])
        return (p1[0] + t * (p2[0] - p1[0]), y)

    out = points
    out = clip_edge(out, lambda p: p[0] >= xmin, lambda a, b: inter_x(a, b, xmin))
    out = clip_edge(out, lambda p: p[0] <= xmax, lambda a, b: inter_x(a, b, xmax))
    out = clip_edge(out, lambda p: p[1] >= ymin, lambda a, b: inter_y(a, b, ymin))
    out = clip_edge(out, lambda p: p[1] <= ymax, lambda a, b: inter_y(a, b, ymax))
    return out


def collect_highway_ways(ways):
    """Все way с highway из известных классов (машинных + пешеходных), кроме HIGHWAY_SKIPPED."""
    out = []
    for wid, way in ways.items():
        tags = way["tags"]
        hw = tags.get("highway")
        if hw is None or hw in HIGHWAY_SKIPPED:
            continue
        if hw not in HIGHWAY_TO_ROAD_CLASS and hw not in SIDEWALK_HIGHWAY_TAGS:
            continue
        out.append((wid, hw, way["refs"]))
    return out


def point_in_polygon(p, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if (a[1] > p[1]) != (b[1] > p[1]):
            x_cross = (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]
            if p[0] < x_cross:
                inside = not inside
    return inside


def fix_drivable_corridors(edges, final_buildings, entry_point):
    """KC-438b: у каждого сегмента не-арки расстояние от осевой до ближайшей стены (0, если
    сегмент внутри здания) должно быть >= DRIVABLE_CLEARANCE_STUDS. Жадно: сдвиг сегмента
    (обоих концов или одного — поворот) перпендикулярно ему шагом 0.1 не дальше
    DRIVABLE_SHIFT_MAX_STUDS от исходного положения; узел графа двигается вместе со всеми
    рёбрами, которые в нём сходятся. Сдвиг принимается, только если ни один затронутый
    сегмент не стал хуже min(порог, было). Точки на рамке и въезд не двигаются.
    Не помогло — ребро удаляется, если граф остаётся связным от въезда; иначе остаётся
    и попадает в stats["unresolved_edges"] (DistrictDrivable.spec покраснеет).
    Сегменты, примыкающие к устью арки, не считают зазор до здания, которое арка
    пронзает: в нём будет вырез (KC-437b)."""
    index = [(bbox_of(b["footprint"]), b["footprint"]) for b in final_buildings]
    pad = DRIVABLE_CLEARANCE_STUDS + 1.0
    frame = (FRAME_MIN_X * SCALE, FRAME_MAX_X * SCALE, FRAME_MIN_Y * SCALE, FRAME_MAX_Y * SCALE)

    def clearance(a, b, exempt):
        seg_bb = (min(a[0], b[0]) - pad, max(a[0], b[0]) + pad, min(a[1], b[1]) - pad, max(a[1], b[1]) + pad)
        best = float("inf")
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        for k, (bb, fp) in enumerate(index):
            if k in exempt or not bbox_overlap(seg_bb, bb):
                continue
            if point_in_polygon(a, fp) or point_in_polygon(b, fp) or point_in_polygon(mid, fp):
                return 0.0
            ring = fp + [fp[0]]
            for j in range(len(ring) - 1):
                d = seg_seg_distance(a, b, ring[j], ring[j + 1])
                if d < best:
                    best = d
        return best

    # Арки: какие здания они пронзают и где их устья.
    pierced = set()
    passage_nodes = set()
    for e in edges:
        if not e["passage"]:
            continue
        passage_nodes.add(e["points"][0])
        passage_nodes.add(e["points"][-1])
        for i in range(len(e["points"]) - 1):
            a, b = e["points"][i], e["points"][i + 1]
            for k in range(len(index)):
                others = set(range(len(index))) - {k}
                if clearance(a, b, others) < PASSAGE_TOUCH_EPS_STUDS:
                    pierced.add(k)

    def seg_clearance(ei, si):
        pts = edges[ei]["points"]
        a, b = pts[si], pts[si + 1]
        exempt = pierced if (a in passage_nodes or b in passage_nodes) else ()
        return clearance(a, b, exempt)

    def pinned(p):
        if p == entry_point:
            return True
        return (
            abs(p[0] - frame[0]) < DRIVABLE_PIN_EPS_STUDS
            or abs(p[0] - frame[1]) < DRIVABLE_PIN_EPS_STUDS
            or abs(p[1] - frame[2]) < DRIVABLE_PIN_EPS_STUDS
            or abs(p[1] - frame[3]) < DRIVABLE_PIN_EPS_STUDS
        )

    def locations(ei, pi):
        pts = edges[ei]["points"]
        if 0 < pi < len(pts) - 1:
            return [(ei, pi)]
        p = pts[pi]
        out = []
        for ej, e in enumerate(edges):
            if e["points"][0] == p:
                out.append((ej, 0))
            if e["points"][-1] == p:
                out.append((ej, len(e["points"]) - 1))
        return out

    def affected_segments(locs):
        segs = set()
        for ej, pj in locs:
            if pj > 0:
                segs.add((ej, pj - 1))
            if pj < len(edges[ej]["points"]) - 1:
                segs.add((ej, pj))
        return segs

    endpoints = set()
    for e in edges:
        endpoints.add(e["points"][0])
        endpoints.add(e["points"][-1])
    original = {}
    for ei, e in enumerate(edges):
        for pi, p in enumerate(e["points"]):
            original[(ei, pi)] = p
    touched_edges = set()

    def try_shift(ei, si):
        pts = edges[ei]["points"]
        a, b = pts[si], pts[si + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return False
        nx, ny = -dy / length, dx / length
        locs_a, locs_b = locations(ei, si), locations(ei, si + 1)
        modes = [locs_a + locs_b, locs_a, locs_b]
        steps = int(round(DRIVABLE_SHIFT_MAX_STUDS / DRIVABLE_SHIFT_STEP_STUDS))
        for step in range(1, steps + 1):
            mag = step * DRIVABLE_SHIFT_STEP_STUDS
            for sign in (1.0, -1.0):
                for locs in modes:
                    if any(pinned(edges[ej]["points"][pj]) for ej, pj in locs):
                        continue
                    segs = affected_segments(locs)
                    before = {sg: seg_clearance(*sg) for sg in segs}
                    saved = [(ej, pj, edges[ej]["points"][pj]) for ej, pj in locs]
                    ok = True
                    for ej, pj, p in saved:
                        q = (round(p[0] + sign * mag * nx, 1), round(p[1] + sign * mag * ny, 1))
                        o = original[(ej, pj)]
                        if math.hypot(q[0] - o[0], q[1] - o[1]) > DRIVABLE_SHIFT_MAX_STUDS + 1e-9:
                            ok = False
                        if p in endpoints and q != p and q in endpoints:
                            ok = False  # не склеивать узел с чужим узлом
                        edges[ej]["points"][pj] = q
                    if ok and seg_clearance(ei, si) >= DRIVABLE_CLEARANCE_STUDS:
                        ok = all(seg_clearance(*sg) >= min(DRIVABLE_CLEARANCE_STUDS, before[sg]) for sg in segs)
                    else:
                        ok = False
                    if ok:
                        for ej, pj, p in saved:
                            if p in endpoints:
                                endpoints.discard(p)
                                endpoints.add(edges[ej]["points"][pj])
                            touched_edges.add(ej)
                        return True
                    for ej, pj, p in saved:
                        edges[ej]["points"][pj] = p
        return False

    for _ in range(DRIVABLE_FIX_PASSES):
        changed = False
        for ei, e in enumerate(edges):
            if e["passage"]:
                continue
            for si in range(len(e["points"]) - 1):
                if seg_clearance(ei, si) < DRIVABLE_CLEARANCE_STUDS and try_shift(ei, si):
                    changed = True
        if not changed:
            break

    def bad_edges():
        out = []
        for ei, e in enumerate(edges):
            if e["passage"]:
                continue
            if any(seg_clearance(ei, si) < DRIVABLE_CLEARANCE_STUDS for si in range(len(e["points"]) - 1)):
                out.append(ei)
        return out

    def all_reachable(alive):
        adj = {}
        for ei in alive:
            a, b = edges[ei]["points"][0], edges[ei]["points"][-1]
            adj.setdefault(a, []).append((b, ei))
            adj.setdefault(b, []).append((a, ei))
        if entry_point not in adj:
            return False
        seen, stack, seen_edges = {entry_point}, [entry_point], set()
        while stack:
            n = stack.pop()
            for other, ei in adj.get(n, []):
                seen_edges.add(ei)
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        return len(seen_edges) == len(alive)

    alive = set(range(len(edges)))
    deleted = 0
    unresolved = []
    for ei in bad_edges():
        trial = alive - {ei}
        if all_reachable(trial):
            alive = trial
            deleted += 1
        else:
            unresolved.append(ei)

    max_shift = 0.0
    for (ei, pi), o in original.items():
        p = edges[ei]["points"][pi]
        max_shift = max(max_shift, math.hypot(p[0] - o[0], p[1] - o[1]))
    stats = {
        "shifted_edges": len(touched_edges & alive),
        "deleted_edges": deleted,
        "passages": sum(1 for e in edges if e["passage"]),
        "unresolved_edges": len(unresolved),
        "max_shift_studs": round(max_shift, 2),
    }
    kept = [edges[ei] for ei in sorted(alive)]
    return kept, stats


def build_roads_dataset(nodes, ways, final_buildings):
    highway_ways = collect_highway_ways(ways)

    # Общие узлы (design §3.2 п.7): ref, встреченный больше чем в одном way, — потенциальный
    # перекрёсток, RDP не имеет права его убрать.
    ref_uses = {}
    for wid, hw, refs in highway_ways:
        for ref in set(refs):
            ref_uses[ref] = ref_uses.get(ref, 0) + 1
    junction_refs = {ref for ref, n in ref_uses.items() if n > 1}

    vehicle_edges = []  # [{"cls":..., "points": [(x,y)meters...]}]
    sidewalks_raw = []  # [(points_meters, tag)]
    skipped_no_geom = 0

    for wid, hw, refs in highway_ways:
        pts = []
        junction_coords = set()
        ok = True
        for ref in refs:
            latlon = nodes.get(ref)
            if latlon is None:
                ok = False
                break
            xy = project(*latlon)
            pts.append(xy)
            if ref in junction_refs:
                junction_coords.add(xy)
        if not ok or len(pts) < 2:
            skipped_no_geom += 1
            continue
        simplified = rdp_protected(pts, RDP_EPSILON_LINE_M, junction_coords) if len(pts) > 2 else pts
        junction_set = set(simplified) & junction_coords
        chains = clip_polyline(simplified, FRAME_MIN_X, FRAME_MAX_X, FRAME_MIN_Y, FRAME_MAX_Y)
        if hw in SIDEWALK_HIGHWAY_TAGS:
            for chain in chains:
                sidewalks_raw.append((chain, hw))
            continue
        cls = HIGHWAY_TO_ROAD_CLASS[hw]
        passage = ways[wid]["tags"].get("tunnel") == PASSAGE_TUNNEL_TAG
        for chain in chains:
            # Разбиваем цепочку на «сырые» рёбра по узлам-перекрёсткам, чтобы граф видел
            # каждый перекрёсток отдельным узлом (design §3.2 п.7).
            current = [chain[0]]
            for p in chain[1:]:
                current.append(p)
                if p in junction_set and len(current) >= 2 and p != current[0]:
                    vehicle_edges.append({"cls": cls, "passage": passage, "points": current})
                    current = [p]
            if len(current) >= 2:
                vehicle_edges.append({"cls": cls, "passage": passage, "points": current})

    # --- Граф: узлы = координаты концов рёбер, degree = число рёбер на узле. ---
    def node_key(p):
        return (round(p[0], 3), round(p[1], 3))

    class RawEdge:
        __slots__ = ("cls", "passage", "points", "alive")

        def __init__(self, cls, passage, points):
            self.cls = cls
            self.passage = passage
            self.points = points
            self.alive = True

    edges = [RawEdge(e["cls"], e["passage"], e["points"]) for e in vehicle_edges]
    adjacency = {}
    for i, e in enumerate(edges):
        for end in (node_key(e.points[0]), node_key(e.points[-1])):
            adjacency.setdefault(end, []).append(i)

    def far_endpoint(edge, node):
        a, b = node_key(edge.points[0]), node_key(edge.points[-1])
        return b if a == node else a

    def oriented_points_ending_at(edge, node):
        if node_key(edge.points[-1]) == node:
            return edge.points
        return list(reversed(edge.points))

    def direction_near(points, at_end, sample=3):
        # at_end True -> направление входа в последнюю точку; False -> выхода из первой.
        if at_end:
            a = points[max(0, len(points) - 1 - sample)]
            b = points[-1]
        else:
            a = points[0]
            b = points[min(len(points) - 1, sample)]
        return (b[0] - a[0], b[1] - a[1])

    def angle_deg(v1, v2):
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-9 or l2 < 1e-9:
            return 0.0
        cosv = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
        return math.degrees(math.acos(cosv))

    merges = 0
    changed = True
    while changed:
        changed = False
        for node in list(adjacency.keys()):
            incident = [i for i in adjacency.get(node, []) if edges[i].alive]
            if len(incident) != 2:
                continue
            i1, i2 = incident
            e1, e2 = edges[i1], edges[i2]
            if e1.cls != e2.cls or e1.passage != e2.passage or i1 == i2:
                continue  # арку не сливаем с соседями: флаг passage — на ребре целиком (KC-438b)
            p1 = oriented_points_ending_at(e1, node)  # ...-> node
            p2 = oriented_points_ending_at(e2, node)  # node -> ... (need reversed of ending-at)
            p2_out = list(reversed(p2))  # node -> other2
            dir_in = direction_near(p1, True)
            dir_out = direction_near(p2_out, False)
            turn = angle_deg(dir_in, dir_out)
            if turn >= ROAD_JUNCTION_MERGE_ANGLE_DEG:
                continue
            far1 = far_endpoint(e1, node)
            far2 = far_endpoint(e2, node)
            if far1 == far2:
                continue  # петля через один узел — не сливаем, редкий вырожденный случай
            merged_points = p1 + p2_out[1:]
            new_edge = RawEdge(e1.cls, e1.passage, merged_points)
            edges.append(new_edge)
            new_i = len(edges) - 1
            e1.alive = False
            e2.alive = False
            adjacency[node] = [k for k in adjacency.get(node, []) if k not in (i1, i2)]
            for far in (far1, far2):
                adjacency[far] = [k for k in adjacency.get(far, []) if k not in (i1, i2)]
                adjacency[far].append(new_i)
            merges += 1
            changed = True
            break  # adjacency поменялась — начинаем проход заново

    alive_edges = [e for e in edges if e.alive]

    # --- Связность: компоненты, въезд, острова (design §2, §3.2 п.7). ---
    node_adj = {}
    for e in alive_edges:
        a, b = node_key(e.points[0]), node_key(e.points[-1])
        node_adj.setdefault(a, []).append((b, e))
        node_adj.setdefault(b, []).append((a, e))

    entry_node = None
    entry_dist = None
    for e in alive_edges:
        if e.cls != ROAD_CLASS_PRIMARY:
            continue
        for end in (e.points[0], e.points[-1]):
            on_boundary = (
                abs(end[0] - FRAME_MIN_X) < FRAME_EDGE_EPS_M
                or abs(end[0] - FRAME_MAX_X) < FRAME_EDGE_EPS_M
                or abs(end[1] - FRAME_MIN_Y) < FRAME_EDGE_EPS_M
                or abs(end[1] - FRAME_MAX_Y) < FRAME_EDGE_EPS_M
            )
            if not on_boundary:
                continue
            dist = abs(end[1] - ENTRY_TARGET_Y_M)
            if entry_dist is None or dist < entry_dist:
                entry_dist = dist
                entry_node = node_key(end)

    def component_of(start):
        seen = {start}
        stack = [start]
        comp_edges = set()
        while stack:
            n = stack.pop()
            for other, e in node_adj.get(n, []):
                comp_edges.add(id(e))
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        return seen, comp_edges

    if entry_node is not None and entry_node in node_adj:
        keep_nodes, keep_edge_ids = component_of(entry_node)
    else:
        keep_nodes, keep_edge_ids = set(), set()

    kept_edges = [e for e in alive_edges if id(e) in keep_edge_ids]
    dropped_islands = len(alive_edges) - len(kept_edges)

    # --- Проезжаемость (KC-438b): сдвиг осевой от стены или удаление ребра, арки. ---
    studs_edges = [
        {"cls": e.cls, "passage": e.passage, "points": [(round(x * SCALE, 1), round(y * SCALE, 1)) for x, y in e.points]}
        for e in kept_edges
    ]
    entry_studs = (round(entry_node[0] * SCALE, 1), round(entry_node[1] * SCALE, 1)) if entry_node is not None else None
    studs_edges, drivable_stats = fix_drivable_corridors(studs_edges, final_buildings, entry_studs)

    # --- Финальные узлы/рёбра/сужение (координаты уже в studs). ---
    node_ids = {}
    node_list = []
    for e in studs_edges:
        for end in (e["points"][0], e["points"][-1]):
            if end not in node_ids:
                node_ids[end] = len(node_list) + 1
                node_list.append({"x": end[0], "y": end[1]})

    building_bboxes = [(bbox_of(b["footprint"]), b["footprint"]) for b in final_buildings]

    road_edges_out = []
    narrowed_count = 0
    for idx, e in enumerate(studs_edges, start=1):
        pts_studs = e["points"]
        nominal = ROAD_CLASS_WIDTH_STUDS[e["cls"]]
        half = nominal / 2.0
        eb = bbox_of(pts_studs)
        search_bbox = (
            eb[0] - half - ROAD_SEARCH_MARGIN_STUDS,
            eb[1] + half + ROAD_SEARCH_MARGIN_STUDS,
            eb[2] - half - ROAD_SEARCH_MARGIN_STUDS,
            eb[3] + half + ROAD_SEARCH_MARGIN_STUDS,
        )
        min_clear = None
        for bb, footprint in building_bboxes:
            if not bbox_overlap(search_bbox, bb):
                continue
            ring = footprint + [footprint[0]]
            for i in range(len(pts_studs) - 1):
                a, b = pts_studs[i], pts_studs[i + 1]
                for j in range(len(ring) - 1):
                    d = seg_seg_distance(a, b, ring[j], ring[j + 1])
                    if min_clear is None or d < min_clear:
                        min_clear = d
        if min_clear is not None and min_clear < half + ROAD_WALL_CLEARANCE_STUDS:
            new_half = max(ROAD_MIN_WIDTH_STUDS / 2.0, min_clear - ROAD_WALL_CLEARANCE_STUDS)
            width = max(ROAD_MIN_WIDTH_STUDS, min(nominal, 2.0 * new_half))
        else:
            width = nominal
        if width < nominal - 1e-6:
            narrowed_count += 1
        road_edges_out.append(
            {
                "id": idx,
                "cls": e["cls"],
                "passage": e["passage"],
                "width": round(width, 1),
                "nodeA": node_ids[pts_studs[0]],
                "nodeB": node_ids[pts_studs[-1]],
                "points": pts_studs,
            }
        )

    entry_id = node_ids.get(entry_studs) if entry_studs is not None else None

    # --- Тротуары/дорожки (design §4.3): без графа, по своим цепочкам. ---
    sidewalks_out = []
    for i, (chain, hw) in enumerate(sidewalks_raw, start=1):
        pts_studs = [(round(x * SCALE, 1), round(y * SCALE, 1)) for x, y in chain]
        sidewalks_out.append({"id": i, "cls": hw, "width": SIDEWALK_WIDTH_STUDS, "points": pts_studs})

    stats = {
        "vehicle_ways_raw_edges": len(vehicle_edges),
        "merges": merges,
        "nodes": len(node_list),
        "edges": len(road_edges_out),
        "narrowed": narrowed_count,
        "dropped_island_edges": dropped_islands,
        "sidewalks": len(sidewalks_out),
        "entry_id": entry_id,
        "skipped_no_geom": skipped_no_geom,
        "drivable": drivable_stats,
        "by_class": {cls: sum(1 for r in road_edges_out if r["cls"] == cls) for cls in ROAD_CLASSES},
    }
    return {
        "nodes": node_list,
        "edges": road_edges_out,
        "sidewalks": sidewalks_out,
        "entryNodeId": entry_id,
    }, stats


def classify_area_kind(tags):
    if tags.get("amenity") in ("school", "kindergarten"):
        return AREA_KIND_SCHOOL_GROUNDS, "polygon"
    if tags.get("amenity") == "parking":
        return AREA_KIND_PARKING, "polygon"
    if tags.get("leisure") == "playground":
        return AREA_KIND_PLAYGROUND, "polygon"
    if tags.get("leisure") == "pitch":
        return AREA_KIND_PITCH, "polygon"
    if tags.get("leisure") == "track":
        return AREA_KIND_TRACK, "polygon"
    if tags.get("landuse") == "grass":
        return AREA_KIND_GRASS, "polygon"
    if tags.get("landuse") == "forest" or tags.get("natural") in ("wood", "scrub"):
        return AREA_KIND_FOREST, "polygon"
    if tags.get("natural") == "water":
        return AREA_KIND_WATER, "polygon"
    if tags.get("natural") == "tree_row":
        return AREA_KIND_TREE_ROW, "line"
    if tags.get("barrier") == "fence":
        return AREA_KIND_FENCE, "line"
    return None, None


def build_areas_dataset(nodes, ways):
    polygons = []
    lines = []
    points = []
    skipped_not_closed = 0
    skipped_out_of_frame = 0
    skipped_degenerate = 0
    node_tags = collect_node_tags()

    for wid, way in ways.items():
        tags = way["tags"]
        kind, shape = classify_area_kind(tags)
        if kind is None:
            continue
        refs = way["refs"]
        pts = polygon_from_refs(refs, nodes)
        if pts is None or len(pts) < 2:
            skipped_degenerate += 1
            continue
        if shape == "polygon":
            if len(pts) < 4 or refs[0] != refs[-1]:
                skipped_not_closed += 1
                continue
            area_m2, cx, cy = shoelace_area_centroid(pts)
            if area_m2 <= 0.0:
                skipped_degenerate += 1
                continue
            if not (FRAME_MIN_X <= cx <= FRAME_MAX_X and FRAME_MIN_Y <= cy <= FRAME_MAX_Y):
                skipped_out_of_frame += 1
                continue
            simplified = rdp_ring(pts, RDP_EPSILON_LINE_M)[:-1]
            clipped = clip_polygon_to_bbox(simplified, FRAME_MIN_X, FRAME_MAX_X, FRAME_MIN_Y, FRAME_MAX_Y)
            if len(clipped) < 3:
                skipped_out_of_frame += 1
                continue
            footprint = [(round(x * SCALE, 1), round(y * SCALE, 1)) for x, y in clipped]
            entry = {"kind": kind, "points": footprint}
            if kind == AREA_KIND_PARKING:
                entry["parkingKind"] = parse_parking_kind(tags.get("parking"))
                capacity = parse_area_capacity(tags.get("capacity"))
                if capacity is not None:
                    entry["capacity"] = capacity
            elif kind == AREA_KIND_PITCH:
                entry["sport"] = parse_sport_kind(tags.get("sport"))
            polygons.append(entry)
        else:
            # Заборы и ряды деревьев тянутся вдоль улиц и могут быть длиннее рамки —
            # в отличие от компактных зданий/площадей, отбор по центроиду их не удержит
            # внутри района (найдено на реальных данных: забор школы длиной больше 100 м
            # заходил за рамку на добрую сотню studs). Режем по рамке, как дороги (design §3.2 п.1).
            # KC-472: узлы barrier=gate/lift_gate, лежащие на этой way, защищены от RDP —
            # иначе упрощение линии могло стереть вершину ворот, и «ворота на линии забора»
            # переставало быть верным.
            gate_by_coord = {}
            if kind == AREA_KIND_FENCE:
                for ref in refs:
                    t = node_tags.get(ref)
                    if t is None:
                        continue
                    gate_kind = GATE_TAG_TO_KIND.get(t.get("barrier"))
                    if gate_kind is None:
                        continue
                    latlon = nodes.get(ref)
                    if latlon is None:
                        continue
                    gate_by_coord[project(*latlon)] = gate_kind
            if len(pts) > 2:
                simplified = rdp_protected(pts, RDP_EPSILON_LINE_M, set(gate_by_coord.keys()))
            else:
                simplified = pts
            chains = clip_polyline(simplified, FRAME_MIN_X, FRAME_MAX_X, FRAME_MIN_Y, FRAME_MAX_Y)
            if not chains:
                skipped_out_of_frame += 1
                continue
            for chain in chains:
                line_pts = [(round(x * SCALE, 1), round(y * SCALE, 1)) for x, y in chain]
                lines.append({"kind": kind, "points": line_pts})
                for mx, my in chain:
                    # chain уже отрезан clip_polyline по рамке (в метрах), поэтому точка внутри
                    # рамки заведомо; отдельная проверка здесь не нужна.
                    gate_kind = gate_by_coord.get((mx, my))
                    if gate_kind is None:
                        continue
                    points.append(
                        {
                            "kind": AREA_KIND_GATE,
                            "x": round(mx * SCALE, 1),
                            "y": round(my * SCALE, 1),
                            "detail": gate_kind,
                        }
                    )

    tree_count = 0
    tree_out_of_frame = 0
    tree_nodes = collect_tree_nodes(nodes, ways)
    for lat, lon in tree_nodes:
        x, y = project(lat, lon)
        if not (FRAME_MIN_X <= x <= FRAME_MAX_X and FRAME_MIN_Y <= y <= FRAME_MAX_Y):
            tree_out_of_frame += 1
            continue
        points.append(
            {"kind": AREA_KIND_TREE, "x": round(x * SCALE, 1), "y": round(y * SCALE, 1)}
        )
        tree_count += 1

    equipment_out_of_frame = 0
    for latlon, equip_kind in collect_equipment_nodes(nodes, node_tags):
        x, y = project(*latlon)
        if not (FRAME_MIN_X <= x <= FRAME_MAX_X and FRAME_MIN_Y <= y <= FRAME_MAX_Y):
            equipment_out_of_frame += 1
            continue
        points.append(
            {
                "kind": AREA_KIND_EQUIPMENT,
                "x": round(x * SCALE, 1),
                "y": round(y * SCALE, 1),
                "detail": equip_kind,
            }
        )

    for i, p in enumerate(polygons, start=1):
        p["id"] = i
    for i, l in enumerate(lines, start=1):
        l["id"] = i
    for i, p in enumerate(points, start=1):
        p["id"] = i

    stats = {
        "polygons": len(polygons),
        "lines": len(lines),
        "points": len(points),
        "skipped_not_closed": skipped_not_closed,
        "skipped_out_of_frame": skipped_out_of_frame,
        "skipped_degenerate": skipped_degenerate,
        "tree_out_of_frame": tree_out_of_frame,
        "by_kind": {},
    }
    for item in polygons + lines + points:
        stats["by_kind"][item["kind"]] = stats["by_kind"].get(item["kind"], 0) + 1
    return {"polygons": polygons, "lines": lines, "points": points}, stats


_TREE_NODE_CACHE = None


def collect_tree_nodes(nodes, ways):
    """natural=tree — теги живут на node, а не на way; parse_osm их не сохраняет отдельно,
    поэтому читаем raw.osm second pass именно за деревьями (дёшево: один XML-проход)."""
    global _TREE_NODE_CACHE
    if _TREE_NODE_CACHE is not None:
        return _TREE_NODE_CACHE
    tree = ET.parse(RAW_OSM)
    root = tree.getroot()
    out = []
    for child in root:
        if child.tag != "node":
            continue
        tags = {t.get("k"): t.get("v") for t in child.findall("tag")}
        if tags.get("natural") == "tree":
            out.append((float(child.get("lat")), float(child.get("lon"))))
    _TREE_NODE_CACHE = out
    return out


def format_number(n):
    if float(n).is_integer():
        return "%d" % int(n)
    return ("%.2f" % n).rstrip("0").rstrip(".")


def render_luau(buildings):
    lines = []
    lines.append("--!strict")
    lines.append("-- СГЕНЕРИРОВАНО tools/osm_import.py, не править вручную (пересчитается при следующем импорте).")
    lines.append("-- Contains information from OpenStreetMap, © OpenStreetMap contributors (https://www.openstreetmap.org/copyright),")
    lines.append("-- made available under the Open Database License 1.0: https://opendatacommons.org/licenses/odbl/1-0/")
    lines.append("-- Источник: input/raw.osm. Имена, адреса и бренды удалены при импорте")
    lines.append("-- OSM id в модулях нет, только собственный порядковый id.")
    lines.append("")
    lines.append("export type OsmPoint = { x: number, y: number }")
    lines.append("export type OsmRgb = { r: number, g: number, b: number }")
    lines.append("export type OsmEntrance = { x: number, y: number, edge: number, kind: string }")
    lines.append("export type OsmBuilding = {")
    lines.append("\tid: number,")
    lines.append("\tkind: string,")
    lines.append("\tlevels: number,")
    lines.append("\theight: number,")
    lines.append("\tcentroid: OsmPoint,")
    lines.append("\tfootprint: { OsmPoint },")
    lines.append("\twall: OsmRgb,")
    lines.append("\taccent: OsmRgb?,")
    lines.append("\tfacade: string,")
    lines.append("\troof: string,")
    lines.append("\troofColor: OsmRgb?,")
    lines.append("\t-- true: цвет стены снят по фото (overrides.csv), а не выбран из Palette по серии")
    lines.append("\tphotoColor: boolean,")
    lines.append("\t-- KC-471: подъезды из OSM entrance=*, привязанные к ребру контура (design §3)")
    lines.append("\tentrances: { OsmEntrance },")
    lines.append("\tentranceCount: number,")
    lines.append("\tbalconies: string,")
    lines.append("\tground: string,")
    lines.append("\tpattern: { string },")
    lines.append("\tendColor: OsmRgb?,")
    lines.append("\tbalconyColor: OsmRgb?,")
    lines.append("\tshopCount: number,")
    lines.append('\tappearanceSource: string, -- "photo" | "series"')
    lines.append("\t-- KC-472: число боксов ряда гаражей (building=garages), nil у остальных kind")
    lines.append("\tboxCount: number?,")
    lines.append("\t-- С1: роль корпуса составного здания (декомпозиция контура, SCHOOL_DECOMPOSE_OSM_ID")
    lines.append('\t-- в tools/osm_import.py), "main" | "wing" | "gym"; nil у цельных зданий')
    lines.append("\tpart: string?,")
    lines.append("}")
    lines.append("")
    lines.append("export type OsmBuildingsData = {")
    lines.append("\tKinds: { string },")
    lines.append("\tFacadeMaterials: { string },")
    lines.append("\tRoofKinds: { string },")
    lines.append("\tEntranceKinds: { string },")
    lines.append("\tBalconyKinds: { string },")
    lines.append("\tGroundKinds: { string },")
    lines.append("\tFacadePatterns: { string },")
    lines.append("\tParts: { string },")
    lines.append("\tPalette: { [string]: OsmRgb },")
    lines.append("\tBuildings: { OsmBuilding },")
    lines.append("}")
    lines.append("")

    def rgb_literal(rgb):
        r, g, b = rgb
        return "{ r = %d, g = %d, b = %d }" % (r, g, b)

    lines.append("local OsmBuildings: OsmBuildingsData = {")
    lines.append("\tKinds = {")
    for kind in ALLOWED_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tFacadeMaterials = {")
    for facade in FACADE_MATERIALS:
        lines.append('\t\t"%s",' % facade)
    lines.append("\t},")
    lines.append("\tRoofKinds = {")
    for roof in ROOF_KINDS:
        lines.append('\t\t"%s",' % roof)
    lines.append("\t},")
    lines.append("\tEntranceKinds = {")
    for kind in ENTRANCE_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tBalconyKinds = {")
    for kind in BALCONY_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tGroundKinds = {")
    for kind in GROUND_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tFacadePatterns = {")
    for pattern in FACADE_PATTERNS:
        lines.append('\t\t"%s",' % pattern)
    lines.append("\t},")
    lines.append("\tParts = {")
    for role in BUILDING_PART_ROLES:
        lines.append('\t\t"%s",' % role)
    lines.append("\t},")
    lines.append("\tPalette = {")
    for name in sorted(PALETTE.keys()):
        lines.append("\t\t%s = %s," % (name, rgb_literal(PALETTE[name])))
    lines.append("\t},")
    lines.append("\tBuildings = {")
    for i, b in enumerate(buildings, start=1):
        fp = ", ".join(
            "{ x = %s, y = %s }" % (format_number(x), format_number(y)) for x, y in b["footprint"]
        )
        lines.append("\t\t{")
        lines.append("\t\t\tid = %d," % i)
        lines.append('\t\t\tkind = "%s",' % b["kind"])
        lines.append("\t\t\tlevels = %s," % format_number(b["levels"]))
        lines.append("\t\t\theight = %s," % format_number(b["height"]))
        lines.append(
            "\t\t\tcentroid = { x = %s, y = %s },"
            % (format_number(b["centroid"][0]), format_number(b["centroid"][1]))
        )
        lines.append("\t\t\tfootprint = { %s }," % fp)
        lines.append("\t\t\twall = %s," % rgb_literal(b["wall"]))
        lines.append("\t\t\taccent = %s," % (rgb_literal(b["accent"]) if b["accent"] is not None else "nil"))
        lines.append('\t\t\tfacade = "%s",' % b["facade"])
        lines.append('\t\t\troof = "%s",' % b["roof"])
        lines.append(
            "\t\t\troofColor = %s," % (rgb_literal(b["roofColor"]) if b["roofColor"] is not None else "nil")
        )
        lines.append("\t\t\tphotoColor = %s," % ("true" if b["photoColor"] else "false"))
        ent_lits = ", ".join(
            '{ x = %s, y = %s, edge = %d, kind = "%s" }'
            % (format_number(e["x"]), format_number(e["y"]), e["edge"], e["kind"])
            for e in b["entrances"]
        )
        lines.append("\t\t\tentrances = { %s }," % ent_lits)
        lines.append("\t\t\tentranceCount = %d," % b["entranceCount"])
        lines.append('\t\t\tbalconies = "%s",' % b["balconies"])
        lines.append('\t\t\tground = "%s",' % b["ground"])
        pattern_lits = ", ".join('"%s"' % p for p in b["pattern"])
        lines.append("\t\t\tpattern = { %s }," % pattern_lits)
        lines.append(
            "\t\t\tendColor = %s," % (rgb_literal(b["endColor"]) if b["endColor"] is not None else "nil")
        )
        lines.append(
            "\t\t\tbalconyColor = %s,"
            % (rgb_literal(b["balconyColor"]) if b["balconyColor"] is not None else "nil")
        )
        lines.append("\t\t\tshopCount = %d," % b["shopCount"])
        lines.append('\t\t\tappearanceSource = "%s",' % b["appearanceSource"])
        lines.append("\t\t\tboxCount = %s," % (str(b["boxCount"]) if b["boxCount"] is not None else "nil"))
        if b.get("part"):
            lines.append('\t\t\tpart = "%s",' % b["part"])
        lines.append("\t\t},")
    lines.append("\t},")
    lines.append("}")
    lines.append("")
    lines.append("return OsmBuildings")
    lines.append("")
    return "\n".join(lines)


def write_photo_check(photo_check, nodes):
    lines = []
    lines.append("# Здания без надёжной этажности — сверить по фото (KC-434)")
    lines.append("")
    lines.append(
        "Этажность выведена из «той же серии» или по площади (design district-osm.md §3.2 п.5),"
    )
    lines.append("не из прямого тега OSM. Формат: `osm_id | lat, lon | предполагаемая этажность | правило`.")
    lines.append("")
    for b, levels, source in photo_check:
        lat = REF_LAT + b["centroid_m"][1] / 111132.92
        lon = REF_LON + b["centroid_m"][0] / (111412.84 * math.cos(math.radians(REF_LAT)))
        lines.append(
            "- %s | %.5f, %.5f | %s | %s (%s)"
            % (b["osm_id"], lat, lon, format_number(levels), source, b["kind"])
        )
    lines.append("")
    return "\n".join(lines)


def format_luau(text):
    """design §3.2 п.8: форматирование stylua через stdin с явным stylua.toml версии.
    Ни одного временного файла в src/: Rojo 7.7 падает, заметив временный файл,
    который уже удалён (CLAUDE.md, 2026-09-09). Если stylua недоступен, текст
    остаётся неотформатированным — шлюз `stylua --check` это покажет."""
    try:
        result = subprocess.run(
            ["stylua", "--config-path", os.path.join(VERSION_DIR, "stylua.toml"), "-"],
            input=text.encode("utf-8"),
            check=True,
            capture_output=True,
        )
        return result.stdout.decode("utf-8").replace("\r\n", "\n")
    except (OSError, subprocess.CalledProcessError) as exc:
        print("Предупреждение: stylua не применён (%s)" % exc, file=sys.stderr)
        return text


def render_formatted(buildings):
    return format_luau(render_luau(buildings))


def render_roads_luau(data):
    lines = []
    lines.append("--!strict")
    lines.append("-- СГЕНЕРИРОВАНО tools/osm_import.py, не править вручную (пересчитается при следующем импорте).")
    lines.append("-- Contains information from OpenStreetMap, © OpenStreetMap contributors (https://www.openstreetmap.org/copyright),")
    lines.append("-- made available under the Open Database License 1.0: https://opendatacommons.org/licenses/odbl/1-0/")
    lines.append("-- Источник: input/raw.osm. Имена и адреса удалены при импорте")
    lines.append("-- OSM id в модулях нет, только собственный порядковый id.")
    lines.append("")
    lines.append("export type OsmPoint = { x: number, y: number }")
    lines.append("export type OsmRoadNode = { id: number, x: number, y: number }")
    lines.append("export type OsmRoadEdge = {")
    lines.append("\tid: number,")
    lines.append("\tclass: string,")
    lines.append("\twidth: number,")
    lines.append("\tnodeA: number,")
    lines.append("\tnodeB: number,")
    lines.append("\tpassage: boolean?, -- арка сквозь здание (OSM tunnel=building_passage), KC-438b")
    lines.append("\tpoints: { OsmPoint },")
    lines.append("}")
    lines.append("export type OsmSidewalk = {")
    lines.append("\tid: number,")
    lines.append("\tclass: string,")
    lines.append("\twidth: number,")
    lines.append("\tpoints: { OsmPoint },")
    lines.append("}")
    lines.append("")
    lines.append("export type OsmRoadsData = {")
    lines.append("\tClasses: { string },")
    lines.append("\tSidewalkClasses: { string },")
    lines.append("\tEntryNodeId: number?,")
    lines.append("\tNodes: { OsmRoadNode },")
    lines.append("\tEdges: { OsmRoadEdge },")
    lines.append("\tSidewalks: { OsmSidewalk },")
    lines.append("}")
    lines.append("")
    lines.append("local OsmRoads: OsmRoadsData = {")
    lines.append("\tClasses = {")
    for cls in ROAD_CLASSES:
        lines.append('\t\t"%s",' % cls)
    lines.append("\t},")
    lines.append("\tSidewalkClasses = {")
    for tag in sorted(SIDEWALK_HIGHWAY_TAGS):
        lines.append('\t\t"%s",' % tag)
    lines.append("\t},")
    entry = data["entryNodeId"]
    lines.append("\tEntryNodeId = %s," % (str(entry) if entry is not None else "nil"))
    lines.append("\tNodes = {")
    for i, n in enumerate(data["nodes"], start=1):
        lines.append("\t\t{ id = %d, x = %s, y = %s }," % (i, format_number(n["x"]), format_number(n["y"])))
    lines.append("\t},")
    lines.append("\tEdges = {")
    for e in data["edges"]:
        pts = ", ".join("{ x = %s, y = %s }" % (format_number(x), format_number(y)) for x, y in e["points"])
        lines.append("\t\t{")
        lines.append("\t\t\tid = %d," % e["id"])
        lines.append('\t\t\tclass = "%s",' % e["cls"])
        lines.append("\t\t\twidth = %s," % format_number(e["width"]))
        lines.append("\t\t\tnodeA = %d," % e["nodeA"])
        lines.append("\t\t\tnodeB = %d," % e["nodeB"])
        if e["passage"]:
            lines.append("\t\t\tpassage = true,")
        lines.append("\t\t\tpoints = { %s }," % pts)
        lines.append("\t\t},")
    lines.append("\t},")
    lines.append("\tSidewalks = {")
    for s in data["sidewalks"]:
        pts = ", ".join("{ x = %s, y = %s }" % (format_number(x), format_number(y)) for x, y in s["points"])
        lines.append("\t\t{")
        lines.append("\t\t\tid = %d," % s["id"])
        lines.append('\t\t\tclass = "%s",' % s["cls"])
        lines.append("\t\t\twidth = %s," % format_number(s["width"]))
        lines.append("\t\t\tpoints = { %s }," % pts)
        lines.append("\t\t},")
    lines.append("\t},")
    lines.append("}")
    lines.append("")
    lines.append("return OsmRoads")
    lines.append("")
    return "\n".join(lines)


def render_areas_luau(data):
    lines = []
    lines.append("--!strict")
    lines.append("-- СГЕНЕРИРОВАНО tools/osm_import.py, не править вручную (пересчитается при следующем импорте).")
    lines.append("-- Contains information from OpenStreetMap, © OpenStreetMap contributors (https://www.openstreetmap.org/copyright),")
    lines.append("-- made available under the Open Database License 1.0: https://opendatacommons.org/licenses/odbl/1-0/")
    lines.append("-- Источник: input/raw.osm. Имена и адреса удалены при импорте")
    lines.append("-- OSM id в модулях нет, только собственный порядковый id.")
    lines.append("")
    lines.append("export type OsmPoint = { x: number, y: number }")
    lines.append("export type OsmAreaPolygon = {")
    lines.append("\tid: number,")
    lines.append("\tkind: string,")
    lines.append("\tpoints: { OsmPoint },")
    lines.append("\t-- KC-472: только kind = \"parking\"")
    lines.append("\tparkingKind: string?,")
    lines.append("\tcapacity: number?,")
    lines.append("\t-- KC-472: только kind = \"pitch\"")
    lines.append("\tsport: string?,")
    lines.append("}")
    lines.append("export type OsmAreaLine = { id: number, kind: string, points: { OsmPoint } }")
    lines.append("export type OsmAreaPoint = {")
    lines.append("\tid: number,")
    lines.append("\tkind: string,")
    lines.append("\tx: number,")
    lines.append("\ty: number,")
    lines.append("\t-- KC-472: GateKinds для kind = \"gate\", EquipmentKinds для kind = \"equipment\"")
    lines.append("\tdetail: string?,")
    lines.append("}")
    lines.append("")
    lines.append("export type OsmAreasData = {")
    lines.append("\tKinds: { string },")
    lines.append("\tGateKinds: { string },")
    lines.append("\tParkingKinds: { string },")
    lines.append("\tEquipmentKinds: { string },")
    lines.append("\tSportKinds: { string },")
    lines.append("\tPolygons: { OsmAreaPolygon },")
    lines.append("\tLines: { OsmAreaLine },")
    lines.append("\tPoints: { OsmAreaPoint },")
    lines.append("}")
    lines.append("")
    lines.append("local OsmAreas: OsmAreasData = {")
    lines.append("\tKinds = {")
    for kind in AREA_ALL_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tGateKinds = {")
    for kind in GATE_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tParkingKinds = {")
    for kind in PARKING_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tEquipmentKinds = {")
    for kind in EQUIPMENT_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tSportKinds = {")
    for kind in SPORT_KINDS:
        lines.append('\t\t"%s",' % kind)
    lines.append("\t},")
    lines.append("\tPolygons = {")
    for p in data["polygons"]:
        pts = ", ".join("{ x = %s, y = %s }" % (format_number(x), format_number(y)) for x, y in p["points"])
        extra = ""
        if "parkingKind" in p:
            extra += ', parkingKind = "%s"' % p["parkingKind"]
        if "capacity" in p:
            extra += ", capacity = %d" % p["capacity"]
        if "sport" in p:
            extra += ', sport = "%s"' % p["sport"]
        lines.append(
            "\t\t{ id = %d, kind = \"%s\", points = { %s }%s },"
            % (p["id"], p["kind"], pts, extra)
        )
    lines.append("\t},")
    lines.append("\tLines = {")
    for l in data["lines"]:
        pts = ", ".join("{ x = %s, y = %s }" % (format_number(x), format_number(y)) for x, y in l["points"])
        lines.append("\t\t{ id = %d, kind = \"%s\", points = { %s } }," % (l["id"], l["kind"], pts))
    lines.append("\t},")
    lines.append("\tPoints = {")
    for pt in data["points"]:
        extra = ""
        if "detail" in pt:
            extra = ', detail = "%s"' % pt["detail"]
        lines.append(
            "\t\t{ id = %d, kind = \"%s\", x = %s, y = %s%s },"
            % (pt["id"], pt["kind"], format_number(pt["x"]), format_number(pt["y"]), extra)
        )
    lines.append("\t},")
    lines.append("}")
    lines.append("")
    lines.append("return OsmAreas")
    lines.append("")
    return "\n".join(lines)


def render_roads_formatted(data):
    return format_luau(render_roads_luau(data))


def render_areas_formatted(data):
    return format_luau(render_areas_luau(data))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Пересобрать в память и сравнить с файлом")
    args = parser.parse_args()

    ensure_overrides_file(OVERRIDES_CSV)
    nodes_raw, ways_raw, relations_raw = parse_osm(RAW_OSM)
    buildings, photo_check, stats, nodes = build_dataset()
    roads, road_stats = build_roads_dataset(nodes_raw, ways_raw, buildings)
    areas, area_stats = build_areas_dataset(nodes_raw, ways_raw)

    outputs = [
        (OUTPUT_LUAU, render_formatted(buildings)),
        (OUTPUT_ROADS_LUAU, render_roads_formatted(roads)),
        (OUTPUT_AREAS_LUAU, render_areas_formatted(areas)),
    ]

    if args.check:
        buildings2, _, _, _ = build_dataset()
        roads2, _ = build_roads_dataset(nodes_raw, ways_raw, buildings2)
        areas2, _ = build_areas_dataset(nodes_raw, ways_raw)
        outputs2 = [
            (OUTPUT_LUAU, render_formatted(buildings2)),
            (OUTPUT_ROADS_LUAU, render_roads_formatted(roads2)),
            (OUTPUT_AREAS_LUAU, render_areas_formatted(areas2)),
        ]
        for (path, _), (path2, rebuilt) in zip(outputs, outputs2):
            if not os.path.exists(path):
                print("FAIL: %s не найден (сначала запусти без --check)." % path, file=sys.stderr)
                sys.exit(1)
            with open(path, "r", encoding="utf-8", newline="\n") as f:
                on_disk = f.read()
            if rebuilt != on_disk:
                print("FAIL: %s отличается от пересборки. Запусти без --check." % path, file=sys.stderr)
                sys.exit(1)
        print(
            "OK: --check совпал побайтно (зданий %d, рёбер дорог %d, площадей/линий/точек %d)"
            % (len(buildings2), len(roads2["edges"]), len(areas2["polygons"]) + len(areas2["lines"]) + len(areas2["points"]))
        )
        sys.exit(0)

    for path, text in outputs:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    with open(PHOTO_CHECK_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write(write_photo_check(photo_check, nodes))

    print(
        "Зданий: %d (кандидатов %d, приватных исключено %d, вне рамки %d, мелких сараев %d, вырожденных %d)"
        % (
            stats["kept"],
            stats["candidates"],
            stats["skipped_private"],
            stats["skipped_out_of_frame"],
            stats["skipped_small_shed"],
            stats["skipped_degenerate"],
        )
    )
    print("photo-check.md: %d зданий" % len(photo_check))
    print("Вывод: %s (%d байт)" % (OUTPUT_LUAU, os.path.getsize(OUTPUT_LUAU)))
    print(
        "Дороги: рёбер %d (%s), узлов %d, сужений %d, тротуаров %d, острова отброшены (рёбер) %d, въезд nodeId=%s"
        % (
            road_stats["edges"],
            ", ".join("%s=%d" % (c, n) for c, n in road_stats["by_class"].items()),
            road_stats["nodes"],
            road_stats["narrowed"],
            road_stats["sidewalks"],
            road_stats["dropped_island_edges"],
            road_stats["entry_id"],
        )
    )
    print("Проезжаемость (KC-438b): %s" % road_stats["drivable"])
    print("Вывод: %s (%d байт)" % (OUTPUT_ROADS_LUAU, os.path.getsize(OUTPUT_ROADS_LUAU)))
    print("Площади: %s" % ", ".join("%s=%d" % (k, n) for k, n in sorted(area_stats["by_kind"].items())))
    print("Вывод: %s (%d байт)" % (OUTPUT_AREAS_LUAU, os.path.getsize(OUTPUT_AREAS_LUAU)))


if __name__ == "__main__":
    main()
