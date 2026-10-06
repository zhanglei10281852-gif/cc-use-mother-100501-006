"""基于标准库 http.server 的 JSON API。

端点一览：
- GET  /health
- GET  /legs                         POST /legs
- GET  /shipments                    POST /shipments
- GET  /shipments/{code}
- GET  /shipments/{code}/liability
- GET  /units/{code}
- POST /events
- POST /disruptions
- GET  /replans?shipment=            GET /replans/{id}
- POST /replans/{id}/choose          POST /replans/{id}/refresh
- POST /recover
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .serde import to_jsonable
from .service import CapacityConflict, FreightService


def _make_handler(service: FreightService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FreightReplanning/0.1"

        # 保持测试输出干净
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            query = parse_qs(parsed.query)
            try:
                if method == "GET":
                    result = self._route_get(parts, query)
                else:
                    result = self._route_post(parts)
                status, payload = result
                self._send(status, payload)
            except KeyError as exc:
                self._send(404, {"error": str(exc).strip("'")})
            except CapacityConflict as exc:
                self._send(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})

        def _route_get(self, parts: list[str], query: dict) -> tuple[int, object]:
            if parts == ["health"]:
                return 200, {"status": "ok"}
            if parts == ["legs"]:
                return 200, service.list_legs()
            if parts == ["shipments"]:
                return 200, service.list_shipments()
            if len(parts) == 2 and parts[0] == "shipments":
                return 200, service.shipment_status(parts[1])
            if len(parts) == 3 and parts[0] == "shipments" and parts[2] == "liability":
                return 200, service.shipment_status(parts[1])["liability"]
            if len(parts) == 2 and parts[0] == "units":
                return 200, service.unit_custody(parts[1])
            if parts == ["replans"]:
                shipment = query.get("shipment", [None])[0]
                return 200, service.list_replans(shipment)
            if len(parts) == 2 and parts[0] == "replans":
                return 200, service.get_replan(parts[1])
            raise KeyError(f"未知路径 /{'/'.join(parts)}")

        def _route_post(self, parts: list[str]) -> tuple[int, object]:
            body = self._body()
            if parts == ["legs"]:
                return 201, service.register_leg(**body)
            if parts == ["shipments"]:
                return 201, service.register_shipment(**body)
            if parts == ["events"]:
                return 200, service.ingest_event(**body)
            if parts == ["disruptions"]:
                return 200, service.report_disruption(**body)
            if len(parts) == 3 and parts[0] == "replans" and parts[2] == "choose":
                return 200, service.choose_candidate(replan_id=parts[1], **body)
            if len(parts) == 3 and parts[0] == "replans" and parts[2] == "refresh":
                return 200, service.refresh_replan(replan_id=parts[1])
            if parts == ["recover"]:
                return 200, service.recover()
            raise KeyError(f"未知路径 /{'/'.join(parts)}")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def create_server(service: FreightService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """创建 HTTP 服务实例（port=0 时由系统分配端口）。"""
    return ThreadingHTTPServer((host, port), _make_handler(service))


def serve(service: FreightService, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = create_server(service, host, port)
    actual_host, actual_port = server.server_address[:2]
    print(f"多式联运异常重排服务已启动：http://{actual_host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
