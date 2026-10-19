"""FastAPI server: JSON API, WebSocket live updates and the dashboard."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .bot import TradingBot

WEB_DIR = Path(__file__).parent / "web"


def create_app(bot: TradingBot, run_loop: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(bot.run()) if run_loop else None
        yield
        bot.stop()
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="NSE Options Bot", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    def _call(fn, *args):
        try:
            return fn(*args)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/state")
    def state() -> dict:
        return bot.state

    @app.post("/api/bot/pause")
    def pause() -> dict:
        bot.set_paused(True)
        return {"paused": True}

    @app.post("/api/bot/resume")
    def resume() -> dict:
        bot.set_paused(False)
        return {"paused": False}

    @app.post("/api/bot/auto-paper/{enabled}")
    def auto_paper(enabled: bool) -> dict:
        bot.set_auto_paper(enabled)
        return {"auto_paper": enabled}

    @app.post("/api/ideas/{idea_id}/paper")
    def paper_trade(idea_id: str) -> dict:
        return _call(bot.paper_trade, idea_id)

    @app.post("/api/positions/{pos_id}/close")
    def close_position(pos_id: str) -> dict:
        return _call(bot.close_position, pos_id)

    @app.post("/api/flatten")
    def flatten() -> dict:
        bot.flatten()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        queue = bot.subscribe()
        try:
            if bot.state:
                await socket.send_json(bot.state)
            while True:
                await socket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            bot.unsubscribe(queue)

    return app
