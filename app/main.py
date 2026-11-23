"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import checking, coverage
from .db import MissingRosterError, StorageError, Store, volume_view


DEFAULT_DB_PATH = os.environ.get("NEWSPAPER_DB", "data/checker.db")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class FrameIn(BaseModel):
    frame_no: int = Field(..., description="原片帧号（正整数）")
    candidates: list[str] | None = Field(
        default=None,
        description="候选版号；认不出时留空或省略",
    )


class VolumeIn(BaseModel):
    volume_code: str = Field(..., min_length=1, description="卷号")
    plates: list[str] | None = Field(
        default=None,
        description="全期版号清单（按版序）；首次提交必填，换清单时一并提交",
    )
    frames: list[FrameIn] = Field(..., description="本卷已扫帧")


class RosterIn(BaseModel):
    plates: list[str] = Field(..., description="全期版号清单（按版序）")


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, **extra}},
    )


def create_app(db_path: str | None = None) -> FastAPI:
    resolved_path = db_path or os.environ.get("NEWSPAPER_DB", DEFAULT_DB_PATH)

    store = Store(resolved_path)
    store.initialize()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        yield

    app = FastAPI(
        title="旧报数字档补扫核查 API",
        version="1.0.0",
        description=(
            "按版序登记每期版号，逐卷提交原帧号与候选版号，检查各版是否已扫、"
            "待核或缺扫。"
        ),
        lifespan=lifespan,
    )
    app.state.store = store

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return _error(422, "invalid_request", "请求体格式不正确", details=exc.errors())

    @app.exception_handler(coverage.ArrangementError)
    async def handle_arrangement_error(request: Request, exc: coverage.ArrangementError):
        return _error(422, exc.code, exc.message, data_unchanged=True)

    @app.exception_handler(MissingRosterError)
    async def handle_missing_roster(request: Request, exc: MissingRosterError):
        return _error(422, "missing_roster", exc.message, data_unchanged=True)

    @app.exception_handler(StorageError)
    async def handle_storage_error(request: Request, exc: StorageError):
        return _error(503, "storage_error", exc.message, data_unchanged=True)

    def current_store(request: Request) -> Store:
        return request.app.state.store

    # -- 系统 ---------------------------------------------------------------

    @app.get("/health", tags=["系统"])
    def health(request: Request) -> dict[str, str]:
        current_store(request).ping()
        return {"status": "ok"}

    # -- 期与卷 -------------------------------------------------------------

    @app.get("/issues", tags=["期"])
    def list_issues(request: Request) -> dict[str, Any]:
        return {"issues": current_store(request).list_issues()}

    @app.get("/issues/{issue_id}", tags=["期"])
    def get_issue(issue_id: str, request: Request) -> dict[str, Any]:
        store = current_store(request)
        issue = store.get_issue(issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="该期尚未登记")
        return {
            "issue_id": issue["issue_id"],
            "plates": issue["plates"],
            "volume_count": len(store.get_volumes(issue_id)),
            "updated_at": issue["updated_at"],
        }

    @app.put("/issues/{issue_id}/plates", tags=["期"])
    def set_roster(issue_id: str, payload: RosterIn, request: Request) -> dict[str, Any]:
        """单独登记或更换全期版号清单，并重判该期所有卷。

        任何一卷排不通则整体回滚，沿用原数据。
        """

        return current_store(request).set_roster(issue_id, payload.plates)

    @app.post("/issues/{issue_id}/volumes", status_code=201, tags=["卷"])
    def submit_volume(issue_id: str, payload: VolumeIn, request: Request):
        """提交一卷，可同时提交（或更换）全期清单。

        清单未变时只重判本卷；清单变化时用新清单重判该期所有卷。
        排不通或写入失败整体回滚，沿用原数据。
        """

        summary = current_store(request).submit_volume(
            issue_id=issue_id,
            volume_code=payload.volume_code,
            plates=payload.plates,
            raw_frames=[frame.model_dump() for frame in payload.frames],
        )
        return {
            "issue_id": summary["issue_id"],
            "volume_code": summary["volume_code"],
            "issue_created": summary["issue_created"],
            "roster_changed": summary["roster_changed"],
            "recomputed_volume_codes": summary["recomputed_volume_codes"],
            "volume": summary["volume"],
        }

    @app.get("/issues/{issue_id}/volumes", tags=["卷"])
    def list_volumes(issue_id: str, request: Request) -> dict[str, Any]:
        store = current_store(request)
        if store.get_issue(issue_id) is None:
            raise HTTPException(status_code=404, detail="该期尚未登记")
        return {
            "issue_id": issue_id,
            "volumes": [volume_view(v) for v in store.get_volumes(issue_id)],
        }

    @app.get("/issues/{issue_id}/volumes/{volume_code}", tags=["卷"])
    def get_volume(issue_id: str, volume_code: str, request: Request) -> dict[str, Any]:
        store = current_store(request)
        volume = store.get_volume(issue_id, volume_code)
        if volume is None:
            if store.get_issue(issue_id) is None:
                raise HTTPException(status_code=404, detail="该期尚未登记")
            raise HTTPException(status_code=404, detail="该卷尚未提交")
        return {"issue_id": issue_id, **volume_view(volume)}

    # -- 核查 ---------------------------------------------------------------

    @app.get("/issues/{issue_id}/plates", tags=["核查"])
    def issue_report(issue_id: str, request: Request) -> dict[str, Any]:
        """按期查各版：已扫 / 待核 / 缺扫。"""

        store = current_store(request)
        issue = store.get_issue(issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="该期尚未登记")
        return checking.build_issue_report(issue, store.get_volumes(issue_id))

    @app.get("/issues/{issue_id}/plates/{plate}", tags=["核查"])
    def plate_report(issue_id: str, plate: str, request: Request) -> dict[str, Any]:
        store = current_store(request)
        issue = store.get_issue(issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="该期尚未登记")
        report = checking.build_issue_report(issue, store.get_volumes(issue_id))
        for item in report["plates"]:
            if item["plate"] == plate:
                return {"issue_id": issue_id, **item}
        raise HTTPException(status_code=404, detail="该版号不在本期清单中")

    return app


app = create_app()
