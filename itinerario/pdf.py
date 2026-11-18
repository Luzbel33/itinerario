import io
import math
import os
from pathlib import Path
from urllib.parse import urlsplit
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

INK = colors.HexColor("#18353C")
MUTED = colors.HexColor("#47595D")
ACCENT = colors.HexColor("#09666B")
PAPER = colors.HexColor("#FBFAF6")
PALE = colors.HexColor("#EEF5F3")
BORDER = colors.HexColor("#CADCD7")
WARM = colors.HexColor("#FBEEE3")
TERRACOTTA = colors.HexColor("#AD4C24")
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
        "title": (31, 35, 13, bold, INK),
        "section": (16, 22, 8, bold, ACCENT),
        "minor": (12, 16, 5, bold, INK),
        "body": (11, 16, 8, normal, INK),
        "detail": (9.5, 14, 5, normal, MUTED),
        "caption": (8.5, 12, 4, normal, MUTED),
        "metric": (10, 14, 8, bold, ACCENT),
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
    return f'<link href={quoteattr(url)} color="#09666B"><u>{escape(label)}</u></link>'


def citations(numbers: list[int], sources: dict[int, Source]) -> str:
    references = [link(f"[{number}]", sources[number].url) for number in dict.fromkeys(numbers) if number in sources]
    return " " + " ".join(references) if references else ""


def metrics(meters: float, seconds: float) -> str:
    km = f"{meters / 1000:.2f}".replace(".", ",")
    minutes, remainder = divmod(math.floor(seconds + 0.5), 60)
    time = f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min {remainder:02d} s"
    return f"{km} km · {time}"


def panel(rows, widths, *, fill=PALE, padding=9, edge=None):
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), padding), ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
    ]
    if edge:
        style.append(("LINEBEFORE", (0, 0), (0, -1), 2.4, edge))
    return Table(rows, colWidths=widths, hAlign="LEFT", style=style)


PLATFORMS = (
    (("instagram.com",), "Instagram", "#AD285F", "camera"),
    (("facebook.com",), "Facebook", "#1D56B7", "f"),
    (("x.com", "twitter.com"), "X", "#243137", "cross"),
    (("linkedin.com",), "LinkedIn", "#0A6088", "in"),
    (("reddit.com",), "Reddit", "#B84419", "thread"),
    (("youtube.com",), "YouTube", "#B52227", "play"),
    (("tiktok.com",), "TikTok", "#242D36", "music"),
    (("threads.com", "threads.net"), "Threads", "#30383C", "@"),
    (("bsky.app",), "Bluesky", "#2562AB", "thread"),
    (("t.me",), "Telegram", "#117794", "send"),
)


def source_platform(url: str):
    host = urlsplit(url).hostname or ""
    for domains, label, color, mark in PLATFORMS:
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return label, color, mark
    return "Web", "#476565", "web"


class PlatformMark(Flowable):
    def __init__(self, color: str, mark: str):
        super().__init__()
        self.width = self.height = 19
        self.color, self.mark = color, mark

    def draw(self):
        c = self.canv
        c.setFillColor(colors.HexColor(self.color))
        c.roundRect(0, 0, 19, 19, 4, fill=1, stroke=0)
        c.setStrokeColor(colors.white)
        c.setFillColor(colors.white)
        c.setLineWidth(1.2)
        c.setLineCap(1)
        if self.mark == "camera":
            c.roundRect(4.5, 4.5, 10, 10, 2.3, fill=0, stroke=1)
            c.circle(9.5, 9.5, 2.3, fill=0, stroke=1)
            c.circle(12.9, 12.9, .65, fill=1, stroke=0)
        elif self.mark == "cross":
            c.line(5.6, 14, 13.4, 5)
            c.line(5.6, 5, 13.4, 14)
        elif self.mark == "thread":
            c.ellipse(4, 5.6, 15, 12.5, fill=0, stroke=1)
            c.line(9.5, 12.5, 11, 15)
            c.circle(13, 15, 1, fill=1, stroke=0)
            c.circle(7.2, 9.4, .65, fill=1, stroke=0)
            c.circle(11.8, 9.4, .65, fill=1, stroke=0)
        elif self.mark == "play":
            path = c.beginPath()
            path.moveTo(7, 5.3)
            path.lineTo(14, 9.5)
            path.lineTo(7, 13.7)
            path.close()
            c.drawPath(path, fill=1, stroke=0)
        elif self.mark == "music":
            c.circle(8, 6.5, 2, fill=1, stroke=0)
            c.line(10, 6.5, 10, 14.2)
            c.line(10, 14.2, 13.6, 12.5)
        elif self.mark == "send":
            c.lines([(4, 10, 15, 14), (15, 14, 12, 5), (12, 5, 9, 8), (9, 8, 13, 12), (9, 8, 4, 10)])
        elif self.mark == "web":
            c.circle(9.5, 9.5, 5.3, fill=0, stroke=1)
            c.ellipse(7, 4.2, 12, 14.8, fill=0, stroke=1)
            c.line(4.5, 9.5, 14.5, 9.5)
        else:
            c.setFont("Helvetica-Bold", 10.5)
            c.drawCentredString(9.5, 5.6, self.mark)


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
        lon, lat = point.coordinates
        location_url = f"https://www.google.com/maps/search/?api=1&query={lat:.6f}%2C{lon:.6f}"
        rows.append([
            paragraph(f'<font color="{pin_colors[point.coordinates]}"><b>{marker}</b></font>', "metric"),
            paragraph(f"<b>{escape(point.label)}</b><br/>{escape(point.address)}{reference}", "detail"),
            paragraph(link("Abrir punto", location_url) + transit, "caption"),
        ])
    story.append(panel(rows, [41, CONTENT - 160, 119], padding=5))


def build_pdf(snapshot: Snapshot, map_images: list[bytes | None],
              *, map_credits: tuple[tuple[str, str], ...] | None = None) -> bytes:
    stream = io.BytesIO()
    report = snapshot.report
    geo = snapshot.geography
    source_by_id = {source.id: source for source in snapshot.sources}
    story = [
        paragraph(f"Modo {escape(snapshot.depth.value)}", "metric"),
        paragraph(escape(report.title), "title"),
        paragraph(escape(report.executive_summary)),
    ]
    if report.next_steps:
        actions = [paragraph("Qué hacer ahora", "minor")]
        actions.extend(paragraph(f"• {escape(item)}", "detail") for item in report.next_steps[:3])
        story.append(panel([[actions]], [CONTENT], fill=WARM, padding=9, edge=TERRACOTTA))
        story.append(Spacer(1, 11))
    if geo.legs:
        mode = {"drive": "Auto", "walk": "A pie", "bicycle": "Bicicleta"}[geo.mode]
        objective = "distancia" if geo.objective == "distance" else "tiempo"
        summary = metrics(sum(leg.meters for leg in geo.legs), sum(leg.seconds for leg in geo.legs))
        distance, duration = summary.split(" · ", 1)
        kpis = [(distance, "Distancia estimada"), (duration, "Tiempo de traslado"), (mode, f"Orden por {objective}")]
        story.append(panel([[[paragraph(value, "number"), paragraph(label, "caption")]
                             for value, label in kpis]], [CONTENT / 3] * 3))
        story.append(Spacer(1, 6))
        if geo.baseline_meters is not None and geo.baseline_seconds is not None:
            baseline = metrics(geo.baseline_meters, geo.baseline_seconds)
            if baseline != summary:
                story.append(paragraph(f"Orden inicial: {baseline}.", "caption"))
    views = map_views(geo)
    for index, (view, image) in enumerate(zip(views, map_images, strict=True)):
        story.append(PageBreak() if index else Spacer(1, 9))
        group = []
        draw_map(group, snapshot, view, image, map_credits)
        if not view.sector and len(views) > 1:
            story.append(KeepTogether(group))
        else:
            story.extend(group)

    if report.places:
        story.extend([Spacer(1, 12), paragraph("Opciones para revisar", "section")])
        priority = list(dict.fromkeys(point.place_index for point in geo.points if point.place_index is not None))
        numbered = {point.place_index: marker_for(point, index, geo)
                    for index, point in enumerate(geo.points) if point.place_index is not None}
        order = priority + [index for index in range(len(report.places)) if index not in priority]
        for index in order:
            place = report.places[index]
            number = f"{numbered[index]} · " if index in numbered else ""
            subtext = " · ".join(escape(text) for text in (place.category, place.locality, place.date) if text)
            address = escape(place.address) + citations(place.address_sources, source_by_id) if place.address_sources else ""
            evidence = [paragraph(number + escape(place.name), "minor")]
            if subtext:
                evidence.append(paragraph(subtext, "caption"))
            evidence.append(paragraph(escape(place.fit) + citations(place.sources, source_by_id), "detail"))
            action = []
            if place.action:
                action.append(paragraph(escape(place.action), "detail"))
            if place.public_contact:
                contact = link("Abrir contacto", place.public_contact) if public_url(place.public_contact) else escape(place.public_contact)
                action.append(paragraph(contact, "detail"))
            if address:
                action.append(paragraph(address, "caption"))
            widths = [CONTENT * .57, CONTENT * .43] if action else [CONTENT]
            story.append(panel([[evidence, action] if action else [evidence]], widths,
                               fill=colors.white, padding=8, edge=ACCENT))
            story.append(Spacer(1, 8))
    if report.findings:
        story.append(paragraph("Señales clave", "section"))
        for finding in report.findings:
            date = f" · {escape(finding.date)}" if finding.date else ""
            tag = f'<font color="{FINDING_COLORS[finding.kind]}"><b>{finding.kind.capitalize()}</b></font>'
            story.append(KeepTogether([
                paragraph(f"<b>{escape(finding.title)}</b> · {tag}{date}", "detail"),
                paragraph(escape(finding.detail) + citations(finding.sources, source_by_id), "detail"),
            ]))
    if report.comparisons:
        story.append(paragraph("Comparativas", "section"))
        rows = [
            [paragraph(f"<b>{escape(item.option)}</b><br/>{escape(item.criterion)}", "detail"),
             paragraph(escape(item.observation) + citations(item.sources, source_by_id))]
            for item in report.comparisons
        ]
        story.append(panel(rows, [134, CONTENT - 134], padding=5))
    limits = list(dict.fromkeys([*report.limitations, *geo.notes, *geo.excluded]))
    if limits:
        story.append(paragraph("Pendientes y límites", "section"))
        for item in limits:
            story.append(paragraph(f"• {escape(item)}", "detail"))
    used_sources = {number for item in [*report.findings, *report.places, *report.comparisons] for number in item.sources}
    used_sources.update(number for place in report.places for number in place.address_sources)
    if used_sources:
        story.append(paragraph("Fuentes citadas", "section"))
        rows = []
        for source in snapshot.sources:
            if source.id not in used_sources:
                continue
            platform, color, mark = source_platform(source.url)
            rows.append([
                PlatformMark(color, mark),
                paragraph(f"<b>[{source.id}] · {platform}</b><br/>{link(source.title, source.url)}", "caption"),
            ])
        story.append(panel(rows, [45, CONTENT - 45], padding=5, fill=colors.white))
    if report.methodology:
        story.append(paragraph(escape(report.methodology), "caption"))

    def page(canvas, document):
        body, bold = fonts()
        width, height = A4
        canvas.setFillColor(PAPER)
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setFillColor(ACCENT)
        canvas.roundRect(MARGIN, height - 35, 20, 20, 5, fill=1, stroke=0)
        canvas.setStrokeColor(colors.white)
        canvas.setLineWidth(1)
        canvas.circle(MARGIN + 10, height - 25, 6, fill=0, stroke=1)
        canvas.line(MARGIN + 7, height - 28, MARGIN + 13, height - 22)
        canvas.setFillColor(INK)
        canvas.setFont(bold, 11)
        canvas.drawString(MARGIN + 27, height - 29, "Rumbo")
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(.65)
        canvas.line(MARGIN, height - 40, width - MARGIN, height - 40)
        canvas.line(MARGIN, 40, width - MARGIN, 40)
        canvas.setFont(body, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 26, f"Fecha de consulta {snapshot.created[:10]} (UTC)")
        canvas.drawRightString(width - MARGIN, 26, f"Pág. {document.page}")

    doc = SimpleDocTemplate(
        stream, pagesize=A4, title=report.title, author="Rumbo", leftMargin=MARGIN,
        rightMargin=MARGIN, topMargin=53, bottomMargin=52,
    )
    doc.build(story, onFirstPage=page, onLaterPages=page)
    return stream.getvalue()
