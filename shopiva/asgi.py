"""ASGI entrypoint for Shopiva Django plus the Nia MCP endpoint."""

import contextlib
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shopiva.settings")

from django.core.asgi import get_asgi_application

from shopiva_mcp import mcp

django_application = get_asgi_application()

mcp_application = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    host="0.0.0.0",
)


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():
        yield


async def application(scope, receive, send):
    """Route /mcp directly to Nia MCP and everything else to Django."""
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        await mcp_application(scope, receive, send)
        return

    await django_application(scope, receive, send)
