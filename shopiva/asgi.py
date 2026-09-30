"""ASGI entrypoint for Shopiva Django plus the Nia MCP endpoint."""

import contextlib
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shopiva.settings")

from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.routing import Mount

from shopiva_mcp import mcp

django_application = get_asgi_application()

mcp_application = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    host="0.0.0.0",
)


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():
        yield


application = Starlette(
    routes=[
        Mount("/mcp", app=mcp_application),
        Mount("/", app=django_application),
    ],
    lifespan=lifespan,
)
