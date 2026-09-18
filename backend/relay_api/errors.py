"""RFC 9457 ("Problem Details for HTTP APIs") error responses.

docs/system-design.md section 15: "Errors use RFC 9457 problem details." Rather
than have every route hand-build a problem+json body, routes raise the ordinary
`fastapi.HTTPException` (optionally via `ProblemDetail` for a custom `type`/
`title`) and a single exception handler formats every error response the same
way.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse


class ProblemDetail(HTTPException):
    def __init__(
        self, status_code: int, title: str, *, detail: str | None = None, type_: str = "about:blank"
    ) -> None:
        super().__init__(status_code=status_code, detail=detail or title)
        self.title = title
        self.type_ = type_


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _problem_details_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if not isinstance(exc, HTTPException):  # pragma: no cover - defensive
            return await http_exception_handler(request, exc)
        title = getattr(exc, "title", None) or (
            exc.detail if isinstance(exc.detail, str) else "Error"
        )
        type_ = getattr(exc, "type_", "about:blank")
        body = {
            "type": type_,
            "title": title,
            "status": exc.status_code,
            "detail": exc.detail if isinstance(exc.detail, str) else None,
            "instance": request.url.path,
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            media_type="application/problem+json",
            headers=exc.headers,
        )
