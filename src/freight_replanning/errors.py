"""多式联运异常重排服务异常体系。"""

from __future__ import annotations


class ServiceError(Exception):
    """服务层基础异常，携带 HTTP 状态码与稳定错误码。"""

    status = 400
    code = "service_error"

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"error": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class ValidationError(ServiceError):
    status = 400
    code = "validation_error"


class NotFoundError(ServiceError):
    status = 404
    code = "not_found"


class ConflictError(ServiceError):
    status = 409
    code = "conflict"


class CustodyConflict(ConflictError):
    """保管关系冲突，例如同一托盘被同时装上两辆车。"""

    code = "custody_conflict"


class CapacityConflict(ConflictError):
    """班次容量在并发重排下被占满。"""

    code = "capacity_conflict"
