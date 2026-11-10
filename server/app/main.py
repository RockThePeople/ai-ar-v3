"""앱 서버 v1 진입점 — `python -m server.app.main` (기동은 `server/run-app.sh`).

설정은 `app.env`(경로 `APP_ENV_FILE`) + 환경변수. 바인드는 `APP_BIND_HOST:APP_BIND_PORT`.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .backends import MockBackend, RealBackend
from .config import Settings, load_env_file
from .engine import AppError, Engine
from .routes import build, err_response
from .store import ChunkStore, Db


def create_app(settings: Optional[Settings] = None, *, backend=None, clock=None, start_thread: bool = True,
               llm=None) -> FastAPI:
    s = settings or Settings.from_env()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    db = Db(s.data_dir / "app.sqlite3")
    store = ChunkStore(s.data_dir)
    if backend is None:
        backend = MockBackend(s, store, Path(os.environ.get("APP_MOCK_FIXTURES") or s.data_dir / "mock_fixtures")) if s.mock \
            else RealBackend(s, store)
    kw = {"clock": clock} if clock else {}
    eng = Engine(s, db, store, backend, start_thread=start_thread, llm=llm, **kw)
    st = SimpleNamespace(settings=s, db=db, store=store, engine=eng, backend=backend)

    app = FastAPI(title="vhl app server", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.app = st
    r, v2 = build(st)
    app.include_router(r)
    app.include_router(v2)
    from .debug import build_debug
    app.include_router(build_debug(st))

    @app.exception_handler(AppError)
    async def _app_err(_req: Request, e: AppError):
        return err_response(e)

    @app.exception_handler(RequestValidationError)
    async def _val(_req: Request, e: RequestValidationError):
        return err_response(AppError(400, "INVALID_REQUEST", f"요청 형식 오류: {e.errors()[:1]}"))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, e: StarletteHTTPException):
        code = {404: "INVALID_REQUEST", 405: "INVALID_REQUEST"}.get(e.status_code, "INTERNAL")
        return err_response(AppError(e.status_code, code, str(e.detail)))

    @app.exception_handler(Exception)
    async def _any(_req: Request, e: Exception):
        return err_response(AppError(500, "INTERNAL", f"{type(e).__name__}: {e}"))

    return app


def main() -> None:
    import uvicorn

    p = load_env_file()
    s = Settings.from_env()
    if not s.bind_port:
        raise SystemExit("APP_BIND_PORT 가 없다(app.env)")
    print(f"[app] env={'loaded' if p else 'none'} mock={s.mock} data={s.data_dir} render_mode={s.render_mode}", flush=True)
    uvicorn.run(create_app(s), host=s.bind_host, port=s.bind_port, log_level="info")


if __name__ == "__main__":
    main()
