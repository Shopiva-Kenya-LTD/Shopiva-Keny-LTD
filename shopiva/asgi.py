"""ASGI entrypoint for Shopiva Django plus the Nia MCP endpoint."""

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


async def application(scope, receive, send):
    """Serve Nia MCP at /mcp and keep all other traffic on Django."""
    scope_type = scope.get("type")

    if scope_type == "lifespan":
        try:
            async with mcp.session_manager.run():
                while True:
                    message = await receive()
                    message_type = message.get("type")

                    if message_type == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message_type == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
        except BaseException:
            try:
                await send({
                    "type": "lifespan.startup.failed",
                    "message": "Nia MCP lifespan failed to start.",
                })
            except Exception:
                pass
            raise
        return

    if scope_type == "http":
        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            await mcp_application(scope, receive, send)
            return

    await django_application(scope, receive, send)
