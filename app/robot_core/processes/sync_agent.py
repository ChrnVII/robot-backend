from __future__ import annotations

import asyncio

from app.robot_core.common.config import load_config
from app.ipc import JsonSocketServer
from app.robot_core.storage import PlatformStore
from app.robot_core.sync import HttpsOutboxSender, OutboxDispatcher

from .common import check_output, configure_logging, parser, run


def main() -> None:
    args = parser("sync-agent").parse_args()
    config = load_config(args.config)
    configure_logging(config, "sync-agent")
    store = PlatformStore(config.database_url, pool_options=config.database_pool_options)
    enabled = bool(config.values["sync"]["enabled"])
    if args.check:
        check_output("sync-agent", config, {"enabled": enabled, "pending": len(list(store.pending_outbox()))})
        store.close()
        return

    dispatcher = None
    if enabled:
        sync_config = config.values["sync"]
        if sync_config.get("transport", "https") != "https":
            raise ValueError("only HTTPS outbox transport is implemented; configure MQTT in the cloud adapter")
        sender = HttpsOutboxSender(
            str(sync_config["endpoint"]),
            config.robot_id,
            timeout_seconds=float(sync_config.get("connect_timeout_seconds", 10)),
            certificate_file=sync_config.get("certificate_file"),
            private_key_file=sync_config.get("private_key_file"),
            ca_file=sync_config.get("ca_file"),
        )
        dispatcher = OutboxDispatcher(
            store,
            sender,
            retry_base_seconds=float(sync_config.get("retry_base_seconds", 2.0)),
            max_attempts=int(sync_config.get("max_attempts", 10)),
        )

    async def handler(request):
        operation = request.get("operation")
        if operation == "drain_once":
            if dispatcher is None:
                return {"sent": 0, "state": "disabled"}
            sent = await asyncio.to_thread(
                dispatcher.drain_once, int(config.values["sync"].get("batch_size", 50))
            )
            return {"sent": sent, "pending": len(list(store.pending_outbox()))}
        if operation != "health":
            raise ValueError("unsupported operation")
        return {"service": "sync-agent", "state": "disabled" if not enabled else "ok", "pending": len(list(store.pending_outbox()))}

    async def serve() -> None:
        server = JsonSocketServer(config.path("ipc", "sync_agent"), handler)
        background = None
        if dispatcher is not None:
            async def drain_loop() -> None:
                while True:
                    await asyncio.to_thread(
                        dispatcher.drain_once, int(config.values["sync"].get("batch_size", 50))
                    )
                    await asyncio.sleep(float(config.values["sync"].get("poll_interval_seconds", 2.0)))

            background = asyncio.create_task(drain_loop())
        try:
            await server.serve_forever()
        finally:
            if background is not None:
                background.cancel()
                await asyncio.gather(background, return_exceptions=True)
            await server.close()
            store.close()

    run(serve)


if __name__ == "__main__":
    main()
