"""Server app."""

import asyncio
import datetime
import logging
import os
import sys
import traceback
import typing
import urllib.parse

import aiohttp
import aiohttp.web
import arkprts
import dotenv
import jinja2

from . import export

__all__ = ("app",)

dotenv.load_dotenv()

app = aiohttp.web.Application()
routes = aiohttp.web.RouteTableDef()
env = jinja2.Environment(loader=jinja2.PackageLoader("arkprtserver"), autoescape=True, extensions=["jinja2.ext.do"])

network = arkprts.NetworkSession()
client = arkprts.Client(
    server="en",
    assets=arkprts.BundleAssets(os.environ.get("GAMEDATA"), network=network),
    network=network,
)

LOGGER: logging.Logger = logging.getLogger("arkprtserver")


try:
    from jinja_speedups import *  # type: ignore # noqa: F403

    speedup_unix(env)  # type: ignore # noqa: F405
except ImportError:
    pass


def get_image(type: str, id: str, other_repo: bool = False) -> str:
    id = str(id)
    id = id.replace("@", "_") if "@" in id else id.replace("#", "_")
    id = urllib.parse.quote(id)

    if other_repo:
        return f"https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main/{type}/{id}.png"
    return f"https://raw.githubusercontent.com/Aceship/Arknight-Images/main/{type}/{id}.png"


def get_avatar(char_id: str, skin_id: str) -> str:
    if "@" not in skin_id and skin_id.endswith("#1"):
        skin_id = char_id

    return get_image("avatar", skin_id, True)


env_globals = dict(
    get_image=get_image,
    get_avatar=get_avatar,
    export=export,
    client=client,
    gamedata=client.assets,
    datetime=datetime,
)
env.globals.update(env_globals)  # type: ignore
app["log_request"] = globals().get("log_request", lambda **_: None)  # type: ignore


async def startup(app: aiohttp.web.Application) -> None:
    """Startup function."""
    task = asyncio.create_task(startup_gamedata(app))
    task.add_done_callback(lambda _: None)  # little hack


app.on_startup.append(startup)


async def startup_gamedata(app: aiohttp.web.Application) -> None:
    """Load gamedata."""
    if isinstance(client.assets, arkprts.BundleAssets):
        await asyncio.gather(*[client.assets.update_assets(server=server) for server in ("en", "jp", "kr", "cn")])
    else:
        await client.assets.update_assets()

    env.globals["announcements"] = await client.network.request("an")  # type: ignore
    env.globals["preannouncement"] = await client.network.request("prean")  # type: ignore

    app.update(env.globals)  # type: ignore

    LOGGER.info("Startup finished.")


async def reload_client() -> None:
    """Re-login and download new assets."""
    assert isinstance(client.auth, arkprts.GuestAuth)
    client.auth.sessions.clear()


@aiohttp.web.middleware
async def startup_middleware(
    request: aiohttp.web.Request,
    handler: typing.Callable[[aiohttp.web.Request], typing.Awaitable[aiohttp.web.StreamResponse]],
) -> aiohttp.web.StreamResponse:
    """Startup middleware."""
    if "/api" in request.path:
        while "client" not in request.app._state:
            await asyncio.sleep(0)

    if "client" not in request.app._state:
        template = env.get_template("startup.html.j2")
        return aiohttp.web.Response(text=template.render(request=request), content_type="text/html")

    return await handler(request)


@aiohttp.web.middleware
async def error_middleware(
    request: aiohttp.web.Request,
    handler: typing.Callable[[aiohttp.web.Request], typing.Awaitable[aiohttp.web.StreamResponse]],
) -> aiohttp.web.StreamResponse:
    """Error middleware."""
    try:
        return await handler(request)
    except aiohttp.web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        template = env.get_template("error.html.j2")
        return aiohttp.web.Response(
            text=template.render(request=request, exception=e),
            content_type="text/html",
            status=getattr(e, "status_code", 500),
        )


async def on_shutdown(app: aiohttp.web.Application) -> None:
    """Shutdown client."""
    await client.network.close()


app.middlewares.append(startup_middleware)
app.middlewares.append(error_middleware)
app.on_shutdown.append(on_shutdown)


@routes.get("/")
async def index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Index page."""
    template = env.get_template("index.html.j2")
    return aiohttp.web.Response(text=template.render(request=request), content_type="text/html")


@routes.get("/about")
async def about(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """About page."""
    template = env.get_template("about.html.j2")
    return aiohttp.web.Response(text=template.render(request=request), content_type="text/html")


@routes.get("/search")
async def search(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Search for users."""
    users: typing.Sequence[arkprts.models.Player] = []
    if request.query.get("nickname"):
        users = await client.search_players(request.query["nickname"], server=request.query.get("server"))  # type: ignore

    if request.query.get("all") not in ("1", "true"):
        users = [user for user in users if user.level >= 10]

    template = env.get_template("search.html.j2")
    return aiohttp.web.Response(text=template.render(users=users, request=request), content_type="text/html")


@routes.get("/login")
async def login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Login."""
    template = env.get_template("login.html.j2")
    response = aiohttp.web.Response(text=template.render(request=request), content_type="text/html")

    if not request.query.get("email"):
        return response

    server = request.query.get("server", "en")
    if server not in ("en", "jp", "kr"):
        server = "en"

    auth = arkprts.YostarAuth(server=server, network=client.network)

    if not request.query.get("code"):
        try:
            await auth.get_token_from_email_code(request.query["email"].strip())
        except arkprts.errors.BaseArkprtsError as e:
            if hasattr(e, "data") and e.data.get("result") == 50003:  # type: ignore
                e.data["message"] = "Code has been sent in the last 60s, please wait before sending again"  # type: ignore
                e = arkprts.errors.ArkPrtsError(e.data)  # type: ignore

            return aiohttp.web.Response(text=template.render(request=request, error=str(e)), content_type="text/html")

        return response

    try:
        channel_uid, token = await auth.get_token_from_email_code(request.query["email"].strip(), request.query["code"])
    except arkprts.errors.BaseArkprtsError as e:
        return aiohttp.web.Response(text=template.render(request=request, error=str(e)), content_type="text/html")

    response = aiohttp.web.HTTPTemporaryRedirect("/user")
    response.set_cookie("server", auth.server)
    response.set_cookie("channeluid", channel_uid)
    response.set_cookie("token", token)

    return response


@routes.get("/logout")
async def logout(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Logout."""
    response = aiohttp.web.HTTPTemporaryRedirect("/login")
    response.del_cookie("server")
    response.del_cookie("channeluid")
    response.del_cookie("token")

    return response


async def authorize(request: aiohttp.web.Request) -> typing.Union[arkprts.Client, aiohttp.web.Response]:
    """Attempt to authorize or redirect to login."""
    if not request.cookies.get("channeluid") or not request.cookies.get("token") or not request.cookies.get("server"):
        return aiohttp.web.HTTPTemporaryRedirect("/login")

    auth = arkprts.YostarAuth(server=request.cookies["server"], network=client.network)  # type: ignore

    try:
        await auth.login_with_token(request.cookies["channeluid"], request.cookies["token"])
    except arkprts.errors.BaseArkprtsError:
        response = aiohttp.web.HTTPTemporaryRedirect("/login")
        response.del_cookie("channeluid")
        response.del_cookie("token")
        response.del_cookie("server")
        return response

    return arkprts.Client(auth=auth, assets=client.assets)


@routes.get("/user")
async def user(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """User."""
    user_client = await authorize(request)
    if isinstance(user_client, aiohttp.web.Response):
        return user_client

    user = await user_client.get_data()

    template = env.get_template("user.html.j2")
    return aiohttp.web.Response(text=template.render(user=user, request=request), content_type="text/html")


app.router.add_static("/static", "arkprtserver/static", name="static")
app.add_routes(routes)

from .api import api_routes  # noqa: E402

app.add_routes(api_routes)


def entrypoint(argv: list[str] = sys.argv) -> aiohttp.web.Application:
    """Return app as dummy aiohttp entrypoint."""
    return app
