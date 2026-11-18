import os
from hmac import compare_digest

from aiohttp import web

from .bot import Bot


def application(bot: Bot, secret: str) -> web.Application:
    secret_bytes = secret.encode("ascii")

    async def health(_):
        return web.Response(text="ok")

    async def update(request):
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not compare_digest(header.encode(), secret_bytes):
            raise web.HTTPForbidden()
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        if not isinstance(data, dict) or type(data.get("update_id")) is not int:
            raise web.HTTPBadRequest()
        bot.accept([data], polling=False)
        return web.Response(text="ok")

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/healthz", health)
    app.router.add_post("/telegram", update)
    return app


async def serve_webhook(bot: Bot, secret: str):
    runner = web.AppRunner(application(bot, secret), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "10000"))).start()
        await bot.run(polling=False)
    finally:
        await runner.cleanup()
