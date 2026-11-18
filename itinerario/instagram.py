"""Public Instagram evidence from Apify's capped, free-plan actor runs."""

import asyncio
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from urllib.parse import urlsplit

import httpx

from .providers import ProviderError, request

LOG = logging.getLogger(__name__)
API = "https://api.apify.com/v2"
POSTS = "apify~instagram-scraper"
COMMENTS = "apify~instagram-comment-scraper"
STORIES = "deepmine~instagram-stories-scraper"
RUN_CAP = 0.05
MAX_HANDLES = 3
MAX_POST_LINKS = 3
MAX_VISUAL = 4
HANDLE = re.compile(r"(?!\.)(?!.*\.\.)(?!.*\.$)[a-zA-Z0-9._]{1,30}", re.ASCII)
POST_PATH = re.compile(r"/(?:[\w.]+/)?(p|reel|reels|tv)/([A-Za-z0-9_-]{4,40})/?", re.ASCII)
RESERVED = {"about", "accounts", "api", "auth", "challenge", "developer", "developers", "direct",
            "directory", "emails", "explore", "legal", "p", "press", "privacy", "reel", "reels",
            "share", "stories", "terms", "tv", "web"}


def _instagram_path(url):
    if not isinstance(url, str) or len(url) > 2048:
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
            "instagram.com", "www.instagram.com", "m.instagram.com",
        }:
            return parsed.path
    except ValueError:
        pass
    return ""


def profile_handle(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    path = _instagram_path(value)
    value = (path.strip("/") if path else value.removeprefix("@")).lower()
    return value if HANDLE.fullmatch(value) and value not in RESERVED else ""


def post_link(value):
    match = POST_PATH.fullmatch(_instagram_path(value))
    if not match:
        return ""
    kind, code = match.groups()
    return f"https://www.instagram.com/{'reel' if kind == 'reels' else kind}/{code}/"


def _post_code(url):
    return post_link(url).rstrip("/").rsplit("/", 1)[-1]


def _str(row, key, limit=2200):
    value = row.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def _amount(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Importe desconocido")
    return value


async def free_credit_available(client, headers, run_count, *, verified_scoped=False):
    async def get(path):
        try:
            response = await request(client, "apify", "plan", "GET", f"{API}/users/me{path}",
                                     headers=headers, timeout=15)
        except ProviderError as exc:
            if exc.status == 403 and verified_scoped:
                return None
            raise
        data = response.json()["data"]
        if not isinstance(data, dict):
            raise ValueError("Datos de cuenta desconocidos")
        return data

    account, limits = await asyncio.gather(get(""), get("/limits"))
    credit = 5.0
    if account is not None:
        plan = account["plan"]
        if plan.get("tier") != "FREE" or _amount(plan["monthlyBasePriceUsd"]) != 0:
            return False
        credit = min(credit, _amount(plan["monthlyUsageCreditsUsd"]))
    if limits is None:
        # Actor-scoped tokens cannot inspect billing. Only a token-bound Free attestation
        # and a persistent reservation supplied by the deployment permit this branch.
        return verified_scoped and credit >= run_count * RUN_CAP
    limit = _amount(limits["limits"]["maxMonthlyUsageUsd"])
    used = _amount(limits["current"]["monthlyUsageUsd"])
    # The account hard limit also covers concurrent jobs and delayed usage reporting.
    return limit <= credit and limit - used >= run_count * RUN_CAP


async def _rows(client, headers, actor, data, limit, fields):
    # Do not retry the POST: a broken connection can leave a billable run in progress.
    response = await request(
        client, "apify", actor, "POST", f"{API}/actors/{actor}/run-sync-get-dataset-items",
        params={"timeout": 75, "maxTotalChargeUsd": RUN_CAP, "limit": limit, "clean": "true",
                "restartOnError": "false", "fields": fields}, headers=headers, json=data, timeout=90,
    )
    rows = response.json()
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Formato de dataset desconocido")
    return rows[:limit]


def _active_story(row, handles, now):
    name = profile_handle(row.get("username"))
    if not name or name not in handles or row.get("isLive") is not True or row.get("kind") != "story":
        return None
    try:
        stamp = datetime.fromisoformat(_str(row, "takenAt").replace("Z", "+00:00"))
        if stamp.tzinfo is None or not now - timedelta(hours=24) < stamp <= now:
            return None
    except ValueError:
        return None
    story_id = str(row.get("storyId") or "")
    path = _instagram_path(row.get("storyUrl"))
    matched = re.fullmatch(rf"/stories/{re.escape(name)}/([0-9]{{4,30}})/?", path, re.IGNORECASE)
    suffix = matched[1] if matched else story_id if re.fullmatch(r"[0-9]{4,30}", story_id) else ""
    link = f"https://www.instagram.com/stories/{name}/" + (suffix + "/" if suffix else "")
    stamp = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    # Without a media ID the provider only supplies a rotating profile-story link.
    title = f"Historia de @{name} · {stamp}"
    text = title if suffix else title + " (enlace temporal a las historias de la cuenta)"
    return ({"url": link, "title": title, "text": text}, row)


def _visual_media(row):
    return (_str(row, "displayUrl", 8192) or _str(row, "imageUrl", 8192)
            or _str(row, "thumbnailUrl", 8192), _str(row, "videoUrl", 8192))


async def collect(client, token, handles, post_urls, *, visual_reader, objective="", discovery_query="",
                  reserve_free_credit=None):
    """Returns citeable public records and factual coverage notes for this research."""
    names = list(dict.fromkeys(filter(None, map(profile_handle, handles))))[:MAX_HANDLES]
    seeds = list(dict.fromkeys(filter(None, map(post_link, post_urls))))[:MAX_POST_LINKS]
    discovery = " ".join(discovery_query.split())[:100] if not (names or seeds) else ""
    if not (names or seeds or discovery):
        return [], ["Instagram: no se identificaron cuentas o publicaciones públicas para consultar."]
    if not token:
        return [], ["Instagram: la consulta directa no está configurada."]
    headers = {"Authorization": f"Bearer {token}"}
    try:
        allowed = await free_credit_available(client, headers, 4 if discovery else 3,
                                              verified_scoped=reserve_free_credit is not None)
    except (ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
        LOG.warning("Apify preflight no disponible (%s)", getattr(exc, "status", None) or type(exc).__name__)
        allowed = False
    if not allowed:
        return [], ["Instagram: no se pudo confirmar crédito suficiente en el plan gratuito de Apify."]

    coverage = []

    async def fetch(actor, data, label, limit, fields):
        if reserve_free_credit is not None and not reserve_free_credit(round(100 * RUN_CAP)):
            coverage.append(f"Instagram: el cupo gratuito de Rumbo para {label} está agotado.")
            return []
        try:
            rows = await _rows(client, headers, actor, data, limit, fields)
        except (ProviderError, ValueError) as exc:
            LOG.warning("Apify actor=%s fallo=%s", actor, getattr(exc, "status", None) or type(exc).__name__)
            coverage.append(f"Instagram: consulta de {label} no disponible.")
            return []
        bad = sum(bool(r.get("error") or r.get("errorDescription")) for r in rows)
        if bad:
            coverage.append(f"Instagram: el proveedor marcó {bad} resultado(s) de {label} como inaccesible(s).")
        return [r for r in rows if not (r.get("error") or r.get("errorDescription") or r.get("isPrivate") is True)]

    if discovery:
        profiles = await fetch(
            POSTS, {"search": discovery, "searchType": "user", "searchLimit": 3, "resultsType": "details"},
            "perfiles", 3, "username,private,isBusinessAccount,businessCategoryName,error,errorDescription")
        names = [name for row in profiles if row.get("private") is False
                 and (row.get("isBusinessAccount") is True or _str(row, "businessCategoryName"))
                 and (name := profile_handle(row.get("username")))]
        if not names:
            return [], [*coverage, "Instagram: no se identificaron cuentas profesionales públicas para este pedido."]

    posts_job = fetch(
        POSTS, {"directUrls": [*seeds, *(f"https://www.instagram.com/{name}/" for name in names)],
                "resultsType": "posts", "resultsLimit": 4}, "publicaciones", 15,
        "url,inputUrl,type,caption,ownerUsername,timestamp,commentsCount,displayUrl,videoUrl,childPosts,"
        "error,errorDescription,isPrivate")
    def fetch_stories(names):
        return fetch(STORIES, {"usernames": names, "content": "stories", "maxAgeHours": 24},
                     "historias", 16, "username,kind,mediaType,takenAt,isLive,storyId,storyUrl,imageUrl,videoUrl,"
                     "thumbnailUrl,isPrivate,error,errorDescription")

    if names:
        posts, stories = await asyncio.gather(posts_job, fetch_stories(names))
    else:
        posts = await posts_job
        seed_codes = {_post_code(url) for url in seeds}
        owners = (profile_handle(row.get("ownerUsername")) for row in posts if _post_code(row.get("url")) in seed_codes)
        names = list(dict.fromkeys(filter(None, owners)))[:MAX_HANDLES]
        stories = await fetch_stories(names) if names else []

    terms = set(re.findall(r"\w{4,}", objective.casefold()))
    ranked = {}
    for row in posts:
        url = post_link(row.get("url"))
        if not url or url in ranked:
            continue
        caption = _str(row, "caption")
        owner = profile_handle(row.get("ownerUsername"))
        title = f"Publicación de @{owner}" if owner else "Publicación de Instagram"
        date = _str(row, "timestamp", 40)
        record = {"url": url, "title": title, "text": f"{title} · {date}\n{caption}".strip()}
        relevance = len(terms.intersection(re.findall(r"\w{4,}", caption.casefold())))
        ranked[url] = ((url in seeds, relevance, date), record, row)
    posts_found = [(rec, row) for _, rec, row in sorted(ranked.values(), key=lambda item: item[0], reverse=True)]
    now = datetime.now(timezone.utc)
    story_rows = [_active_story(row, names, now) for row in stories]
    seen = set()
    story_found = []
    for found in sorted((item for item in story_rows if item), key=lambda item: item[1]["takenAt"], reverse=True):
        key = found[0]["title"]
        if key not in seen:
            story_found.append(found)
            seen.add(key)

    targets = list(dict.fromkeys([*seeds, *(r["url"] for r, row in posts_found if row.get("commentsCount") != 0)]))[:3]

    async def get_comments():
        if not targets:
            return []
        rows = await fetch(COMMENTS, {"directUrls": targets, "resultsLimit": 5, "includeNestedComments": False},
                           "comentarios", 15, "id,postUrl,timestamp,text,isPrivate,error,errorDescription")
        records = []
        comments_seen = set()
        target_codes = {_post_code(t) for t in targets}
        for row in rows:
            url, message = post_link(row.get("postUrl")), _str(row, "text", 750)
            if _post_code(url) not in target_codes or not message:
                continue
            comment_id = str(row.get("id") or "")
            if re.fullmatch(r"[0-9]{4,30}", comment_id):
                url += f"c/{comment_id}/"
            date = _str(row, "timestamp", 40)
            if (url, message) not in comments_seen:
                records.append({"url": url, "title": "Comentario público de Instagram",
                                "text": f"Comentario de un usuario · {date}\n{message}"})
                comments_seen.add((url, message))
        return records

    async def read_visuals():
        # Reserve coverage for both the feed and stories, including text-only flyers.
        media = []

        def add(record, row):
            image, video = _visual_media(row)
            if image or video:
                video_cover = not video and (row.get("type") == "Video" or row.get("mediaType") == "video")
                media.append((record, image, video, video_cover))

        for pair in zip_longest(posts_found, story_found):
            for entry in filter(None, pair):
                add(*entry)
        for record, row in posts_found:
            children = row.get("childPosts")
            if isinstance(children, list):
                for child in children[1:3]:
                    if isinstance(child, dict):
                        add(record, child)
        read = 0
        for record, image, video, video_cover in media[:MAX_VISUAL]:
            try:
                detail = await visual_reader(image_url=image, video_url=video)
            except (ProviderError, httpx.HTTPError, ValueError, RuntimeError, TimeoutError):
                detail = ""
            if detail:
                prefix = "Portada del video (solo imagen): " if video_cover else ""
                record["text"] += "\n" + prefix + detail[:2400]
                read += 1
        return read

    comments, visual_count = await asyncio.gather(get_comments(), read_visuals())
    records = [r for r, row in posts_found if _str(row, "caption") or "\n" in r["text"]]
    records.extend(r for r, _ in story_found if "\n" in r["text"])
    records.extend(comments)
    coverage.append(f"Instagram vía Apify: {len(posts_found)} publicaciones, {len(comments)} comentarios, "
                    f"{len(story_found)} historias activas y {visual_count} lecturas visuales.")
    LOG.info("Apify posts=%d comentarios=%d historias=%d lecturas=%d", len(posts_found), len(comments),
             len(story_found), visual_count)
    return records, coverage
