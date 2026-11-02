from __future__ import annotations

import logging

from aiohttp import web

from .db import Database

logger = logging.getLogger(__name__)


def make_app(db: Database) -> web.Application:
    app = web.Application()

    async def health(_request):
        try:
            sources = await db.list_sources()
            state = await db.publication_state()
            errors = sum(1 for s in sources if s["enabled"] and s["last_status"] == "error")
            return web.json_response(
                {
                    "status": "ok" if errors == 0 else "degraded",
                    "publication": state.value,
                    "sources": len(sources),
                    "source_errors": errors,
                    "replacements": await db.replacement_summary(),
                }
            )
        except Exception:
            logger.exception("Health check failed")
            return web.json_response({"status": "error"}, status=503)

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    return app
