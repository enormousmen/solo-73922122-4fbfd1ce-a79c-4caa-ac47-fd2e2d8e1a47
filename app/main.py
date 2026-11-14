"""FastAPI application exposing ingest, undo and query endpoints."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .daily import get_daily_result, issue_daily_result, list_daily_results
from .db import DatabaseBusy, initialize
from .models import (
    ConflictResponse,
    DailyIssueRequest,
    DailyIssueResponse,
    DailyResultOut,
    ErrorResponse,
    HealthResponse,
    IngestRequest,
    MeterStateResponse,
    MeterSummary,
    MutationResponse,
    UndoRequest,
)
from .seed import seed_demo_data
from .service import BatchRejected, ingest, list_meters, query_meter, undo

_BUSY_MESSAGE = "database is locked"


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def create_app(settings: Settings | None = None, seed_demo: bool = False) -> FastAPI:
    app_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(app_settings)
        if seed_demo:
            seed_demo_data(app_settings)
        app.state.settings = app_settings
        yield

    app = FastAPI(
        title="园区电表区间用电量 API",
        version="1.0.0",
        description=(
            "把网关补传的累计读数转换成区间用电量。\n\n"
            "* **入仓** `POST /api/v1/readings`：整批原子提交，乱序补传按电表号+时刻排序后求差。\n"
            "* **撤销** `POST /api/v1/readings/undo`：按源记录号撤销，并重算受影响区间。\n"
            "* **查询** `GET /api/v1/meters/{meter_id}`：单次一致性快照，返回读数、区间、断点。\n"
            "* **日用量签发** `POST /api/v1/daily-results/issue`：核对 UTC 当日 25 个整点与断点，"
            "签发或重签日用量质量结果。\n"
            "* **日用量查询** `GET /api/v1/daily-results/{meter_id}/{date}`："
            "只返回有效结果或明确过期。\n\n"
            "同一源记录号重复提交相同内容为幂等重试；同号异内容、同表同刻冲突会整批拒绝（409）。"
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(DatabaseBusy)
    async def _busy_handler(request: Request, exc: DatabaseBusy) -> JSONResponse:
        return _busy_response()

    @app.exception_handler(sqlite3.OperationalError)
    async def _sqlite_handler(
        request: Request, exc: sqlite3.OperationalError
    ) -> JSONResponse:
        if _is_busy(exc):
            return _busy_response()
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                code="storage_error", message="数据库操作失败"
            ).model_dump(),
        )

    def _busy_response() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                code="database_busy",
                message=_BUSY_MESSAGE + "，请稍后重试",
            ).model_dump(),
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(BatchRejected)
    async def _rejected_handler(
        request: Request, exc: BatchRejected
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=ConflictResponse(
                code="batch_rejected",
                message=exc.message,
                conflicts=exc.conflicts,
            ).model_dump(),
        )

    @app.exception_handler(_InvalidDate)
    async def _invalid_date_handler(
        request: Request, exc: "_InvalidDate"
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                code="invalid_date",
                message=f"日期必须是 YYYY-MM-DD: {exc.raw!r}",
            ).model_dump(),
        )

    def settings_of(request: Request) -> Settings:
        return request.app.state.settings

    @app.get("/health", response_model=HealthResponse, tags=["运维"])
    def health(request: Request) -> HealthResponse:
        """进程与数据库连通性检查。"""
        from .db import connection, current_commit_seq
        from .db import SCHEMA_VERSION

        with connection(settings_of(request)) as conn:
            seq = current_commit_seq(conn)
        return HealthResponse(
            status="ok", commit_seq=seq, schema_version=SCHEMA_VERSION
        )

    @app.post(
        "/api/v1/readings",
        response_model=MutationResponse,
        responses={
            409: {"model": ConflictResponse, "description": "整批拒绝"},
            503: {"model": ErrorResponse},
        },
        tags=["入仓"],
    )
    def ingest_readings(
        payload: IngestRequest, request: Request
    ) -> MutationResponse:
        """入仓一批累计读数（整批原子提交）。

        * 可乱序补传：区间按电表号 + UTC 时刻排序后重新计算。
        * 同号同内容重试：逐条返回 `unchanged`，不重复写入。
        * 同号异内容，或同一电表同一时刻出现两条不同源记录：整批 409 拒绝，
          数据库保持原样。
        * 换表标记或计数回退（后一读数小于前一读数）处记断点，该区间 `usage` 为
          `null`，不推算用量。
        """
        return ingest(settings_of(request), payload.readings)

    @app.post(
        "/api/v1/readings/undo",
        response_model=MutationResponse,
        responses={
            409: {"model": ConflictResponse, "description": "整批拒绝"},
            503: {"model": ErrorResponse},
        },
        tags=["撤销"],
    )
    def undo_readings(payload: UndoRequest, request: Request) -> MutationResponse:
        """按源记录号撤销读数，并重算受影响电表的全部区间。

        撤销相邻读数后，前后两条读数会自动连成新的区间。不存在的源记录号
        返回 `not_found`，不算失败。
        """
        return undo(settings_of(request), payload.source_record_ids)

    @app.get(
        "/api/v1/meters",
        response_model=list[MeterSummary],
        tags=["查询"],
    )
    def get_meters(request: Request) -> list[MeterSummary]:
        """列出所有电表的读数条数、区间数、断点数与累计用量。"""
        return list_meters(settings_of(request))

    @app.get(
        "/api/v1/meters/{meter_id}",
        response_model=MeterStateResponse,
        responses={
            404: {"model": ErrorResponse, "description": "电表无任何读数"},
            422: {"model": ErrorResponse, "description": "时间窗口参数非法"},
        },
        tags=["查询"],
    )
    def get_meter(
        meter_id: str,
        request: Request,
        from_: str | None = Query(
            default=None,
            alias="from",
            description="窗口起点（含），ISO-8601，如 2026-01-01T00:00:00Z",
        ),
        to: str | None = Query(
            default=None, description="窗口终点（含），ISO-8601"
        ),
        include_intervals: bool = Query(
            default=True, description="是否返回区间明细（只计入完全落在窗口内的区间）"
        ),
    ) -> MeterStateResponse:
        """查询单只电表的读数、区间用电量和断点。

        响应来自一个数据库快照：`commit_seq` 是快照对应的提交序号，读数、
        区间与断点同属该次提交，不会看到并发写入的中间状态。
        """
        try:
            state = query_meter(
                settings_of(request),
                meter_id,
                from_raw=from_,
                to_raw=to,
                include_intervals=include_intervals,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content=ErrorResponse(
                    code="invalid_window", message=str(exc)
                ).model_dump(),
            )
        if state is None:
            return JSONResponse(
                status_code=404,
                content=ErrorResponse(
                    code="meter_not_found",
                    message=f"电表 {meter_id} 没有任何读数",
                ).model_dump(),
            )
        return state

    # ------------------------------------------------------------------
    # Daily quality results
    # ------------------------------------------------------------------

    @app.post(
        "/api/v1/daily-results/issue",
        response_model=DailyIssueResponse,
        responses={503: {"model": ErrorResponse}},
        tags=["日用量签发"],
    )
    def issue_daily(payload: DailyIssueRequest, request: Request) -> DailyIssueResponse:
        """按电表号与 UTC 日期签发（或重签）当日用量质量结果。

        * 窗口为当日 00:00 到次日 00:00，**两端都算**，共 25 个逐小时整点。
        * 整点缺读数，或窗口内出现换表 / 计数回退断点：返回
          `status=not_issued` 并列出缺失时刻与断点，**不写入任何结果**。
        * 整点齐全且无断点：汇总窗口内全部相邻区间的用量，保存为当日
          `valid` 结果。
        * 相同内容重试为幂等操作（`idempotent_retry=true`，状态不变）；
          读数变化后重签从最新已提交状态计算，旧结果已在读数提交时过期。
        """
        return issue_daily_result(
            settings_of(request), payload.meter_id, payload.date
        )

    @app.get(
        "/api/v1/daily-results",
        response_model=list[DailyResultOut],
        tags=["日用量查询"],
    )
    def list_daily(
        request: Request,
        meter_id: str | None = Query(default=None, description="按电表号过滤"),
        date: str | None = Query(
            default=None, description="按 UTC 日期过滤（YYYY-MM-DD）"
        ),
    ) -> list[DailyResultOut]:
        """列出每个电表每日最新一条已保存结果（有效或明确过期）。"""
        parsed_date = _parse_date(date)
        return list_daily_results(
            settings_of(request), meter_id=meter_id, day=parsed_date
        )

    @app.get(
        "/api/v1/daily-results/{meter_id}/{date}",
        response_model=DailyResultOut,
        responses={
            404: {"model": ErrorResponse, "description": "该电表该日从未签发"},
            422: {"model": ErrorResponse, "description": "日期格式非法"},
        },
        tags=["日用量查询"],
    )
    def get_daily(
        meter_id: str, date: str, request: Request
    ) -> DailyResultOut | JSONResponse:
        """查询单表单日最新签发结果：只能得到有效日用量或明确过期。"""
        parsed_date = _parse_date(date)
        result = get_daily_result(
            settings_of(request), meter_id.strip(), parsed_date
        )
        if result is None:
            return JSONResponse(
                status_code=404,
                content=ErrorResponse(
                    code="daily_result_not_found",
                    message=(
                        f"电表 {meter_id} 在 {parsed_date.isoformat()} "
                        "没有已签发的日用量结果"
                    ),
                ).model_dump(),
            )
        return result

    return app


def _parse_date(raw: str | None):
    from datetime import date as date_cls

    if raw is None:
        return None
    try:
        return date_cls.fromisoformat(raw)
    except ValueError as exc:
        # FastAPI converts ValueError raised inside a route into 500 unless
        # handled explicitly; the route callers wrap this into 422 below.
        raise _InvalidDate(raw) from exc


class _InvalidDate(ValueError):
    """Raised for malformed ``YYYY-MM-DD`` query/path parameters."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"invalid date: {raw!r}")
        self.raw = raw
