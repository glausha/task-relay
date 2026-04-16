from __future__ import annotations

import json

from aiohttp import web

from task_relay.ingress.forgejo_webhook import canonicalize, verify_signature
from task_relay.journal.writer import JournalWriter


class ForgejoWebhookServer:
    def __init__(
        self,
        journal_writer: JournalWriter,
        webhook_secret: bytes,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
    ) -> None:
        self._writer = journal_writer
        self._secret = webhook_secret
        self._host = host
        self._port = port
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        sig = request.headers.get("X-Forgejo-Signature", "")
        if not verify_signature(body, sig, self._secret):
            return web.Response(status=401, text="invalid signature")
        event_name = request.headers.get("X-Forgejo-Event", "")
        delivery_id = request.headers.get("X-Forgejo-Delivery", "")
        body_json = json.loads(body)
        event = canonicalize(event_name, delivery_id, body_json)
        if event is None:
            return web.Response(status=200, text="ignored")
        self._writer.append(event)
        return web.Response(status=202, text="accepted")

    def create_app(self) -> web.Application:
        if self._app is not None:
            return self._app
        app = web.Application()
        app.router.add_post("/webhook/forgejo", self._handle_webhook)
        app.on_cleanup.append(self._close_writer)
        self._app = app
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        self._runner = web.AppRunner(self.create_app())
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._host, port=self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is None:
            self._writer.close()
            return
        runner = self._runner
        self._runner = None
        self._site = None
        await runner.cleanup()

    async def _close_writer(self, _: web.Application) -> None:
        self._writer.close()
