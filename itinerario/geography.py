import math
import re
import unicodedata
from urllib.parse import urlencode

import httpx

from .models import Coordinate, Geography, Intent, Leg, MapView, Point, Report
from .providers import ProviderError, request

API = "https://api.geoapify.com/v1"
MAP_API = "https://maps.geoapify.com/v1/staticmap"
MAP_SIZE = (1000, 650)
MODES = {"drive": "drive", "walk": "walk", "bicycle": "bicycle"}
ROUTE_COLORS = ("#185DB2", "#A64717", "#6C409F", "#08725F", "#A52459", "#7B5A0D", "#226C83")
ENDPOINT_COLOR = "#273649"


def normalize(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()
    return set(re.findall(r"[a-z0-9]+", plain)) - {"av", "avenida", "calle", "de", "del", "la", "san"}


def parse_coordinate(location: str) -> Coordinate | None:
    if not re.fullmatch(r"\s*-?\d{1,3}(?:\.\d+)?,\s*-?\d{1,3}(?:\.\d+)?\s*", location):
        return None
    lat, lon = [float(value) for value in location.split(",")]
    return (lon, lat) if abs(lon) <= 180 and abs(lat) <= 85 else None


def geocode_candidate(result: dict, address: str) -> bool:
    rank = result.get("rank", {})
    if rank.get("confidence", 0) < 0.68:
        return False
    point_type = result.get("result_type")
    if point_type not in {"building", "amenity"}:
        return False
    address_tokens = normalize(address)
    first_line = result.get("address_line1", "") + " " + result.get("street", "")
    matched = normalize(first_line)
    number = str(result.get("housenumber", ""))
    numbers = {word for word in address_tokens if word.isdigit()}
    if numbers and number not in numbers:
        return False
    return bool((address_tokens & matched) - numbers)


def locality_hint(area: str, country: str) -> str:
    area = re.split(r"\s+[·|–—]\s+", area, maxsplit=1)[0].strip()
    if country == "ar":
        area = re.sub(r"\s+capital$", "", area, flags=re.IGNORECASE)
    return area


def named_point(result: dict, name: str, area: str) -> bool:
    name_tokens = normalize(name)
    area_tokens = normalize(area)
    if not name_tokens or not area_tokens or result.get("result_type") not in {"amenity", "building"}:
        return False
    candidate = normalize(result.get("name") or "")
    subject = name_tokens - area_tokens
    if not subject or candidate - area_tokens != subject:
        return False
    location_names = (normalize(result.get(field) or "") for field in ("city", "town", "village", "municipality"))
    return any(location and (location <= area_tokens or area_tokens <= location) for location in location_names)


def optimal_order(matrix: list[list[float | None]], fixed_end: bool) -> list[int] | None:
    """Solve the small directed path exactly against the provider's cost matrix."""
    end = len(matrix) - 1
    flexible = list(range(1, end if fixed_end else end + 1))
    states = {(0, 0): (0.0, (0,))}
    for mask in range(1 << len(flexible)):
        for last in [0, *flexible]:
            state = states.get((mask, last))
            if state is None:
                continue
            cost, path = state
            for index, target in enumerate(flexible):
                bit = 1 << index
                leg = matrix[last][target]
                if mask & bit or leg is None:
                    continue
                key = (mask | bit, target)
                proposal = (cost + leg, (*path, target))
                if proposal < states.get(key, (math.inf, ())):
                    states[key] = proposal
    finals = []
    complete = (1 << len(flexible)) - 1
    for last in flexible or [0]:
        state = states.get((complete, last))
        if state is None:
            continue
        cost, path = state
        if fixed_end:
            final_leg = matrix[last][end]
            if final_leg is not None:
                finals.append((cost + final_leg, (*path, end)))
        else:
            finals.append(state)
    return list(min(finals)[1]) if finals else None


def map_views(geo: Geography) -> list[MapView]:
    if not geo.points:
        return []
    all_points = list(range(len(geo.points)))
    all_legs = list(range(len(geo.legs)))
    overview = MapView(title="Mapa general", point_indices=all_points, leg_indices=all_legs)
    size = geo.sector_size
    if geo.sector_count == 1 or (not geo.sector_count and len(geo.points) <= size):
        return [overview]
    overlap = int(bool(geo.legs))
    stop = len(geo.points) - overlap
    if geo.sector_count:
        count = min(stop, geo.sector_count)
        if count < 2:
            return [overview]
        width, remainder = divmod(stop, count)
        segment_sizes = [width + int(i < remainder) for i in range(count)]
    else:
        jump = size - overlap
        segment_sizes = [min(jump, stop - i) for i in range(0, stop, jump)]
    views = [overview]
    start_index = 0
    for number, segment_size in enumerate(segment_sizes, 1):
        points = all_points[start_index:start_index + segment_size + overlap]
        legs = all_legs[start_index:points[-1]] if geo.legs else []
        districts = list(dict.fromkeys(geo.points[index].district for index in points if geo.points[index].district))
        suffix = f" · {' / '.join(districts[:2])}" if districts else ""
        views.append(MapView(title=f"Sector {number}{suffix}", sector=number, point_indices=points, leg_indices=legs))
        start_index += segment_size
    return views


def marker_for(point: Point, index: int, geo: Geography) -> str:
    if point.place_index is not None:
        return str(1 + sum(previous.place_index is not None for previous in geo.points[:index]))
    if index == 0:
        return "S"
    return "F"


def sector_color(sector: int) -> str:
    return ROUTE_COLORS[(max(1, sector) - 1) % len(ROUTE_COLORS)]


def route_layers(geo: Geography, view: MapView) -> list[tuple[str, list[Coordinate]]]:
    sectors = map_views(geo)[1:] if not view.sector else []
    return [
        (sector_color(part.sector), [point for i in part.leg_indices for point in geo.legs[i].geometry])
        for part in sectors or [view] if part.leg_indices
    ]


def map_markers(geo: Geography, view: MapView) -> list[tuple[Coordinate, str, str]]:
    sectors = map_views(geo)[1:]
    markers: dict[Coordinate, tuple[str, str]] = {}
    for index in view.point_indices:
        point = geo.points[index]
        label = marker_for(point, index, geo)
        sector = next((part.sector for part in sectors if index in part.point_indices), 0)
        color = ENDPOINT_COLOR if point.place_index is None else sector_color(sector)
        if point.coordinates in markers:
            previous, _ = markers[point.coordinates]
            label = f"{previous}/{label}"
            color = ENDPOINT_COLOR
        markers[point.coordinates] = (label, color)
    return [(coordinate, label, color) for coordinate, (label, color) in markers.items()]


def directions(geo: Geography, view: MapView) -> str | None:
    points = [geo.points[index] for index in view.point_indices]
    if not view.leg_indices or not 2 <= len(points) <= 5:
        return None
    travelmode = {"drive": "driving", "walk": "walking", "bicycle": "bicycling"}[geo.mode]
    coordinates = [f"{point.coordinates[1]},{point.coordinates[0]}" for point in points]
    query = {"api": 1, "origin": coordinates[0], "destination": coordinates[-1], "travelmode": travelmode}
    if coordinates[1:-1]:
        query["waypoints"] = "|".join(coordinates[1:-1])
    return "https://www.google.com/maps/dir/?" + urlencode(query)


def encode_polyline(points: list[Coordinate], precision: int = 6) -> str:
    result = []
    previous = (0, 0)
    for lon, lat in points:
        current = tuple(round(value * (10 ** precision)) for value in (lat, lon))
        for value, old in zip(current, previous):
            diff = value - old
            encoded = ~(diff << 1) if diff < 0 else diff << 1
            while encoded >= 32:
                result.append(chr((encoded & 31) + 95))
                encoded >>= 5
            result.append(chr(encoded + 63))
        previous = current
    return "".join(result)


def map_bounds(points: list[Coordinate]) -> tuple[float, float, float, float]:
    west, south = [min(point[i] for point in points) for i in (0, 1)]
    east, north = [max(point[i] for point in points) for i in (0, 1)]
    dx = max(0.0025, (east - west) * 0.10)
    dy = max(0.0025, (north - south) * 0.10)
    return max(-180, west - dx), max(-85, south - dy), min(180, east + dx), min(85, north + dy)


class Geoapify:
    def __init__(self, client: httpx.AsyncClient, key: str):
        self.client = client
        self.key = key

    async def geocode(self, label: str, address: str, area: str, country: str,
                      *, endpoint: bool = False, place_index: int | None = None) -> Point | None:
        direct = parse_coordinate(address) if endpoint else None
        if direct:
            return Point(label=label, address=address, coordinates=direct)
        area = locality_hint(area, country)
        params = {"limit": 4, "format": "json", "bias": "countrycode:none", "apiKey": self.key}
        if re.fullmatch("[a-z]{2}", country):
            params["filter"] = f"countrycode:{country}"

        async def search(text: str):
            query = {**params, "text": f"{text}, {area}" if area else text}
            response = await request(
                self.client, "geoapify", "geocoding", "GET", API + "/geocode/search", params=query)
            return response.json().get("results", [])

        results = await search(address)
        precise = [item for item in results if geocode_candidate(item, address)]
        if endpoint and not precise:
            precise = [item for item in results if named_point(item, address, area)]
        if not precise and place_index is not None and area and label != address:
            precise = [item for item in await search(label) if named_point(item, label, area)]
        if not precise:
            return None
        best = precise[0]
        for item in precise[1:]:
            distance = math.hypot(item["lat"] - best["lat"], item["lon"] - best["lon"])
            if distance > 0.001 and best["rank"]["confidence"] - item["rank"]["confidence"] < 0.10:
                return None
        return Point(label=label, address=address, coordinates=(best["lon"], best["lat"]),
                     district=best.get("suburb") or best.get("city") or "",
                     place_index=place_index)

    async def matrix(self, points: list[Point], intent: Intent) -> list[list[float | None]]:
        locations = [{"location": point.coordinates} for point in points]
        payload = {"mode": MODES[intent.transport], "sources": locations, "targets": locations}
        if intent.sort_by == "distance":
            payload["type"] = "short"
        response = await request(self.client, "geoapify", "matriz", "POST", API + "/routematrix",
                                 params={"apiKey": self.key}, json=payload)
        property_name = "distance" if intent.sort_by == "distance" else "time"
        rows = response.json().get("sources_to_targets", [])
        size = len(points)
        if len(rows) != size or any(len(row) != size for row in rows):
            raise ProviderError("geoapify", "matriz inválida")
        return [[cell.get(property_name) for cell in row] for row in rows]

    async def route(self, points: list[Point], intent: Intent) -> list[Leg]:
        params = {
            "waypoints": "|".join(f"{point.coordinates[1]},{point.coordinates[0]}" for point in points),
            "mode": MODES[intent.transport], "format": "geojson", "apiKey": self.key,
        }
        if intent.sort_by == "distance":
            params["type"] = "short"
        response = await request(self.client, "geoapify", "ruteo", "GET", API + "/routing", params=params)
        features = response.json().get("features", [])
        if not features:
            raise ProviderError("geoapify", "ruteo sin resultados")
        feature = features[0]
        metrics = feature.get("properties", {}).get("legs", [])
        geometry = feature.get("geometry", {})
        lines = geometry.get("coordinates", [])
        if geometry.get("type") == "LineString":
            lines = [lines]
        if len(metrics) != len(points) - 1 or len(lines) != len(metrics):
            raise ProviderError("geoapify", "ruteo incompleto")
        try:
            return [Leg(meters=metric["distance"], seconds=metric["time"], geometry=line)
                    for metric, line in zip(metrics, lines, strict=True)]
        except (ValueError, KeyError):
            raise ProviderError("geoapify", "ruteo inválido") from None

    async def static_map(self, geography: Geography, view: MapView, language: str = "es") -> bytes:
        pins = map_markers(geography, view)
        layers = route_layers(geography, view)
        coordinates = [point for point, _, _ in pins] + [point for _, line in layers for point in line]
        west, south, east, north = map_bounds(coordinates)
        geometries = [
            {"type": "polyline6", "linecolor": paint or hue, "linewidth": width, "value": encode_polyline(line)}
            for paint, width in (("#FFFFFF", 9), (None, 5))
            for hue, line in layers
        ]
        payload = {
            "style": "osm-bright-grey", "width": MAP_SIZE[0], "height": MAP_SIZE[1],
            "format": "png", "lang": language, "attribution": "default",
            "area": {"type": "rect", "value": {"lon1": west, "lat1": south, "lon2": east, "lat2": north}},
            "markers": [{"lat": lat, "lon": lon, "type": "circle", "size": "large", "contentsize": 17,
                         "color": color, "text": label} for (lon, lat), label, color in pins],
            "geometries": geometries,
        }
        response = await request(self.client, "geoapify", "mapa", "POST", MAP_API,
                                 json=payload, params={"apiKey": self.key})
        if not response.content.startswith(b"\x89PNG\r\n\x1a\n") or len(response.content) > 6_000_000:
            raise ProviderError("geoapify", "imagen inválida")
        return response.content

    async def plan(self, intent: Intent, report: Report) -> Geography:
        geography = Geography(mode=intent.transport, objective=intent.sort_by,
                              sector_size=max(3, min(12, intent.sector_size or 5)),
                              sector_count=max(0, min(6, intent.sector_count)))
        if intent.map_kind == "none":
            return geography
        points = []
        for index, place in enumerate(report.places):
            if not place.address or not place.address_sources:
                geography.excluded.append(f"{place.name} · domicilio pendiente de confirmación.")
                continue
            try:
                place_area = locality_hint(place.locality, intent.country_code)
                if place_area.casefold() in {"", "alrededores", "cercanías"}:
                    place_area = intent.area
                point = await self.geocode(place.name, place.address, place_area,
                                           intent.country_code, place_index=index)
            except ProviderError:
                geography.notes.append("El geocodificador dejó de responder. Se conservan las ubicaciones resueltas.")
                break
            if point:
                points.append(point)
            else:
                geography.excluded.append(f"{place.name} · ubicación ambigua para {place.address}.")
        geography.points = sorted(points, key=lambda point: (point.district, point.coordinates))
        if intent.map_kind != "route":
            return geography
        try:
            origin = await self.geocode("Salida", intent.origin, intent.area, intent.country_code, endpoint=True)
            finish = None
            if origin and intent.return_to_origin:
                finish = origin.model_copy(update={"label": "Regreso"})
            elif intent.finish:
                finish = await self.geocode("Llegada", intent.finish, intent.area, intent.country_code, endpoint=True)
        except ProviderError:
            geography.notes.append("No se pudo consultar la ubicación del punto de salida o llegada.")
            return geography
        if origin is None or (intent.finish and finish is None):
            geography.notes.append("La salida o la llegada no se pudo ubicar con precisión. Revisá el domicilio y pedí recalcular.")
            return geography
        visits = [origin, *points]
        if finish:
            visits.append(finish)
        if len({point.coordinates for point in visits}) < 2:
            geography.notes.append("Faltan paradas con domicilio utilizable para calcular el recorrido.")
            return geography
        try:
            matrix = await self.matrix(visits, intent) if len(visits) > 2 else None
            order = optimal_order(matrix, bool(finish)) if matrix else [0, 1]
            if order is None:
                geography.notes.append("No se encontró una ruta que conecte todas las paradas en el modo pedido.")
                return geography
            sequence = [visits[i] for i in order]
            legs = await self.route(sequence, intent)
        except ProviderError:
            geography.notes.append("El motor de rutas no pudo calcular el trayecto. El mapa conserva las paradas ubicadas.")
            return geography
        geography.points, geography.legs = sequence, legs
        if order != list(range(len(visits))):
            try:
                original = await self.route(visits, intent)
                geography.baseline_meters = sum(leg.meters for leg in original)
                geography.baseline_seconds = sum(leg.seconds for leg in original)
                metric = "meters" if intent.sort_by == "distance" else "seconds"
                if sum(getattr(leg, metric) for leg in legs) > sum(getattr(leg, metric) for leg in original):
                    geography.points, geography.legs = visits, original
                    geography.notes.append("El orden inicial dio un mejor resultado en el cálculo vial final.")
            except ProviderError:
                geography.notes.append("La comparación con el orden inicial no estuvo disponible.")
        return geography
