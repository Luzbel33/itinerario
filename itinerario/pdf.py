import io
import math
import os
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table

from .geography import directions, map_markers, map_views, marker_for, route_layers, sector_color
from .models import Geography, MapView, Snapshot, Source
from .providers import public_url

INK = colors.HexColor("#192B41")
MUTED = colors.HexColor("#435367")
ACCENT = colors.HexColor("#185DB2")
PALE = colors.HexColor("#F2F6FA")
BORDER = colors.HexColor("#D7E2EF")
FINDING_COLORS = {"documentado": "#08725F", "inferencia": "#6C409F", "pendiente": "#8D500B"}
MARGIN = 42
CONTENT = A4[0] - 2 * MARGIN - 12
MAP_HEIGHT = CONTENT * 0.65
FONT_DIRS = [
    (Path("/usr/share/fonts/truetype/dejavu"), "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", "segoeui.ttf", "segoeuib.ttf"),
]


def fonts() -> tuple[str, str]:
    if "Itinerario" not in pdfmetrics.getRegisteredFontNames():
        for path, normal, bold in FONT_DIRS:
            if (path / normal).is_file() and (path / bold).is_file():
                pdfmetrics.registerFont(TTFont("Itinerario", path / normal))
                pdfmetrics.registerFont(TTFont("Itinerario Bold", path / bold))
                break
    if "Itinerario" in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFontFamily("Itinerario", normal="Itinerario", bold="Itinerario Bold")
        return "Itinerario", "Itinerario Bold"
    return "Helvetica", "Helvetica-Bold"


def paragraph(content: str, role: str = "body") -> Paragraph:
    normal, bold = fonts()
    settings = {
        "title": (30, 34, 12, bold, INK),
        "section": (17, 22, 10, bold, INK),
        "minor": (12, 16, 5, bold, INK),
        "body": (10, 15, 7, normal, INK),
        "detail": (9, 13, 6, normal, MUTED),
        "caption": (8, 12, 5, normal, MUTED),
        "metric": (11, 16, 10, bold, ACCENT),
        "number": (17, 22, 2, bold, INK),
    }
    size, leading, after, font, color = settings[role]
    return Paragraph(
        content, ParagraphStyle(role, fontName=font, fontSize=size, leading=leading, spaceAfter=after,
                                textColor=color, splitLongWords=True, keepWithNext=role in {"section", "minor"})
    )


def link(label: str, url: str) -> str:
    if not public_url(url):
        return escape(label)
    return f'<link href={quoteattr(url)} color="#185DB2"><u>{escape(label)}</u></link>'


def citations(numbers: list[int], sources: dict[int, Source]) -> str:
    references = [link(f"[{number}]", sources[number].url) for number in dict.fromkeys(numbers) if number in sources]
    return " " + " ".join(references) if references else ""


def metrics(meters: float, seconds: float) -> str:
    km = f"{meters / 1000:.2f}".replace(".", ",")
    minutes, remainder = divmod(math.floor(seconds + 0.5), 60)
    time = f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min {remainder:02d} s"
    return f"{km} km · {time}"


def panel(rows, widths, *, fill=PALE, padding=9):
    return Table(rows, colWidths=widths, hAlign="LEFT", style=[
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), padding), ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
    ])


class Swatch(Flowable):
    def __init__(self, hue: str):
        super().__init__()
        self.width, self.height = 12, 12
        self.hue = hue

    def draw(self):
        self.canv.setFillColor(colors.HexColor(self.hue))
        self.canv.roundRect(0, 3, 12, 5, 2, fill=1, stroke=0)


class RouteDiagram(Flowable):
    def __init__(self, geography: Geography, view: MapView):
        super().__init__()
        self.geography, self.view = geography, view
        self.width, self.height = CONTENT, MAP_HEIGHT

    def draw(self):
        canvas = self.canv
        pins = map_markers(self.geography, self.view)
        lines = route_layers(self.geography, self.view)
        all_points = [point for point, _, _ in pins] + [point for _, line in lines for point in line]
        west, south = [min(point[i] for point in all_points) for i in (0, 1)]
        east, north = [max(point[i] for point in all_points) for i in (0, 1)]
        cos_lat = math.cos(math.radians((south + north) / 2))
        scale = min((self.width - 60) / max(0.001, (east - west) * cos_lat),
                    (self.height - 55) / max(0.001, north - south))

        def project(point):
            lon, lat = point
            return (self.width / 2 + (lon - (west + east) / 2) * scale * cos_lat,
                    self.height / 2 + (lat - (south + north) / 2) * scale)

        canvas.setFillColor(PALE)
        canvas.roundRect(0, 0, self.width, self.height, 8, fill=1, stroke=0)
        canvas.setLineJoin(1)
        canvas.setLineCap(1)
        for color, line in lines:
            path = canvas.beginPath()
            path.moveTo(*project(line[0]))
            for point in line[1:]:
                path.lineTo(*project(point))
            canvas.setStrokeColor(colors.white)
            canvas.setLineWidth(6)
            canvas.drawPath(path)
            canvas.setStrokeColor(colors.HexColor(color))
            canvas.setLineWidth(3)
            canvas.drawPath(path)
        for coordinates, marker, color in pins:
            x, y = project(coordinates)
            canvas.setFillColor(colors.HexColor(color))
            canvas.setStrokeColor(colors.white)
            canvas.circle(x, y, 11, fill=1, stroke=1)
            canvas.setFont(fonts()[1], 8)
            canvas.setFillColor(colors.white)
            canvas.drawCentredString(x, y - 3, marker)


def draw_map(story, snapshot: Snapshot, view: MapView, image: bytes | None,
             map_credits: tuple[tuple[str, str], ...] | None):
    geo = snapshot.geography
    heading = escape(view.title)
    if view.sector:
        heading = f'<a name="sector-{view.sector}"/><font color="{sector_color(view.sector)}">{heading}</font>'
    story.append(paragraph(heading, "section"))
    if view.leg_indices and view.sector:
        legs = [geo.legs[i] for i in view.leg_indices]
        story.append(paragraph(metrics(sum(leg.meters for leg in legs), sum(leg.seconds for leg in legs)), "metric"))
    if image:
        story.append(Image(io.BytesIO(image), width=CONTENT, height=MAP_HEIGHT))
    else:
        story.append(RouteDiagram(geo, view))
    story.append(Spacer(1, 6))
    label = "Mapa esquemático. Cartografía de fondo no disponible. " if image is None else ""
    credits = map_credits if image and map_credits else (
        ("Powered by Geoapify", "https://www.geoapify.com/"),
        ("© OpenStreetMap contributors", "https://www.openstreetmap.org/copyright"),
    )
    if image and not map_credits:
        credits += (("© OpenMapTiles", "https://openmaptiles.org/"),)
    attribution = " · ".join(link(name, url) for name, url in credits)
    story.append(paragraph(label + attribution, "caption"))
    is_overview = view.title == "Mapa general" and len(map_views(geo)) > 1
    if view.leg_indices and not is_overview:
        for start in range(0, len(view.point_indices) - 1, 4):
            segment = view.model_copy(update={"point_indices": view.point_indices[start:start+5]})
            label = ("Abrir este tramo en Google Maps" if len(view.point_indices) <= 5
                     else f"Abrir tramo {start // 4 + 1} en Google Maps")
            story.append(paragraph(link(label, directions(geo, segment)), "detail"))
    if is_overview:
        rows = []
        for part in map_views(geo)[1:]:
            route_labels = " → ".join(marker_for(geo.points[i], i, geo) for i in part.point_indices)
            rows.append([
                Swatch(sector_color(part.sector)),
                paragraph(f'<link href="#sector-{part.sector}"><b><u>{escape(part.title)}</u></b></link>', "detail"),
                paragraph(escape(route_labels), "detail"),
            ])
        story.append(panel(rows, [30, CONTENT * .65 - 30, CONTENT * .35], padding=3))
        if any(geo.points[i].place_index is None for i in view.point_indices):
            story.append(paragraph("S · Salida   F · Final", "caption"))
        return
    pin_colors = {coordinate: color for coordinate, _, color in map_markers(geo, view)}
    rows = []
    for index in view.point_indices:
        point = geo.points[index]
        marker = marker_for(point, index, geo)
        leg = geo.legs[index - 1] if index > 0 and index - 1 in view.leg_indices else None
        transit = f"<br/>{metrics(leg.meters, leg.seconds)} desde la anterior" if leg else ""
        reference = ""
        if point.place_index is not None:
            place = snapshot.report.places[point.place_index]
            reference = f"<br/>{escape(place.reference)}" if place.reference else ""
        rows.append([
            paragraph(f'<font color="{pin_colors[point.coordinates]}"><b>{marker}</b></font>', "metric"),
            paragraph(f"<b>{escape(point.label)}</b><br/>{escape(point.address)}{reference}", "detail"),
            paragraph(transit.removeprefix("<br/>"), "caption"),
        ])
    story.append(panel(rows, [41, CONTENT - 160, 119], padding=5))


def build_pdf(snapshot: Snapshot, map_images: list[bytes | None],
              *, map_credits: tuple[tuple[str, str], ...] | None = None) -> bytes:
    stream = io.BytesIO()
    report = snapshot.report
    geo = snapshot.geography
    source_by_id = {source.id: source for source in snapshot.sources}
    story = [
        paragraph(escape(snapshot.depth.value.capitalize()), "metric"),
        paragraph(escape(report.title), "title"),
        paragraph(escape(report.executive_summary)),
    ]
    if geo.legs:
        mode = {"drive": "Auto", "walk": "A pie", "bicycle": "Bicicleta"}[geo.mode]
        objective = "distancia" if geo.objective == "distance" else "tiempo"
        summary = metrics(sum(leg.meters for leg in geo.legs), sum(leg.seconds for leg in geo.legs))
        distance, duration = summary.split(" · ", 1)
        kpis = [(distance, "Distancia estimada"), (duration, "Tiempo de traslado"), (mode, f"Orden por {objective}")]
        story.append(panel([[[paragraph(value, "number"), paragraph(label, "caption")]
                             for value, label in kpis]], [CONTENT / 3] * 3))
        story.append(Spacer(1, 6))
        story.append(paragraph("Distancia y tiempo estimados. Consultá tráfico, cierres, acceso y horarios.",
                               "detail"))
        if geo.baseline_meters is not None and geo.baseline_seconds is not None:
            baseline = metrics(geo.baseline_meters, geo.baseline_seconds)
            story.append(paragraph(f"<b>Comparación vial.</b> Orden inicial {baseline}. Orden propuesto {summary}."))
    if report.places:
        counts = Counter(place.category or "Sin categoría" for place in report.places)
        categories = " · ".join(f"{escape(category)} ({count})" for category, count in counts.items())
        story.append(paragraph(f"<b>{len(report.places)} resultados relevados.</b> {categories}.", "detail"))
    for index, (view, image) in enumerate(zip(map_views(geo), map_images, strict=True)):
        story.append(PageBreak() if index else Spacer(1, 9))
        group = []
        draw_map(group, snapshot, view, image, map_credits)
        if not view.sector and len(map_views(geo)) > 1:
            story.append(KeepTogether(group))
        else:
            story.extend(group)

    if geo.points:
        story.append(PageBreak())
    if report.next_steps:
        story.append(paragraph("Próximos pasos", "section"))
        first = paragraph(escape(report.next_steps[0]), "body")
        story.append(panel([[first]], [CONTENT], padding=5))
        story.append(Spacer(1, 7))
        for item in report.next_steps[1:]:
            story.append(paragraph(f"• {escape(item)}"))
    if report.findings:
        story.append(paragraph("Hallazgos", "section"))
        for finding in report.findings:
            date = f" · {escape(finding.date)}" if finding.date else ""
            story.append(KeepTogether([
                paragraph(escape(finding.title), "minor"),
                paragraph(f'<font color="{FINDING_COLORS[finding.kind]}"><b>{finding.kind.capitalize()}</b></font>'
                          f"{date}", "caption"),
                paragraph(escape(finding.detail) + citations(finding.sources, source_by_id)),
            ]))
    if report.places:
        story.append(paragraph("Fichas de lugares", "section"))
        priority = [point.place_index for point in geo.points if point.place_index is not None]
        numbered = {point.place_index: marker_for(point, index, geo)
                    for index, point in enumerate(geo.points) if point.place_index is not None}
        order = priority + [index for index in range(len(report.places)) if index not in priority]
        for index in order:
            place = report.places[index]
            number = f"{numbered[index]} · " if index in numbered else ""
            subtext = " · ".join(escape(text) for text in (place.category, place.locality, place.date) if text)
            address = escape(place.address) + citations(place.address_sources, source_by_id)
            if place.address and not place.address_sources:
                address += " (domicilio sin fuente específica)"
            contact = f"<br/><b>Contacto público:</b> {escape(place.public_contact)}" if place.public_contact else ""
            story.append(KeepTogether([
                paragraph(number + escape(place.name), "minor"),
                paragraph(subtext + (f"<br/>{address}" if address else ""), "detail"),
                paragraph(escape(place.fit) + citations(place.sources, source_by_id)),
                paragraph(f"<b>Próxima acción:</b> {escape(place.action)}{contact}", "detail"),
            ]))
    if report.comparisons:
        story.append(paragraph("Comparativas", "section"))
        rows = [
            [paragraph(f"<b>{escape(item.option)}</b><br/>{escape(item.criterion)}", "detail"),
             paragraph(escape(item.observation) + citations(item.sources, source_by_id))]
            for item in report.comparisons
        ]
        story.append(panel(rows, [134, CONTENT - 134], padding=5))
    if report.methodology:
        story.extend([paragraph("Metodología", "section"), paragraph(escape(report.methodology))])
    limits = list(dict.fromkeys([*report.limitations, *geo.notes, *geo.excluded]))
    if limits:
        story.append(paragraph("Pendientes y límites", "section"))
        for item in limits:
            story.append(paragraph(f"• {escape(item)}"))
    if snapshot.sources:
        story.append(paragraph("Fuentes consultadas", "section"))
        for source in snapshot.sources:
            channel = "Red pública" if source.channel == "social" else "Web"
            story.append(paragraph(
                f"<b>[{source.id}]</b> {link(source.title, source.url)} · {channel} · {escape(source.accessed[:10])}",
                "detail",
            ))

    def page(canvas, document):
        body, bold = fonts()
        width, height = A4
        canvas.setFont(bold, 9)
        canvas.setFillColor(ACCENT)
        canvas.drawString(MARGIN, height - 31, "ITINERARIO")
        canvas.setStrokeColor(BORDER)
        canvas.line(MARGIN, height - 40, width - MARGIN, height - 40)
        canvas.line(MARGIN, 40, width - MARGIN, 40)
        canvas.setFont(body, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 26, f"Fecha de consulta {snapshot.created[:10]} (UTC)")
        canvas.drawRightString(width - MARGIN, 26, str(document.page))

    doc = SimpleDocTemplate(
        stream, pagesize=A4, title=report.title, author="Itinerario", leftMargin=MARGIN,
        rightMargin=MARGIN, topMargin=53, bottomMargin=52,
    )
    doc.build(story, onFirstPage=page, onLaterPages=page)
    return stream.getvalue()
