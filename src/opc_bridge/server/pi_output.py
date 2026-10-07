"""Real and simulated output channels for PI System (OSIsoft / AVEVA PI) integration.

Guarantees and Industrial Safety Principles:
- Strictly output destination: never writes to OPC source tags (zero COM calls, zero OPC DA writes).
- Pure open-source / standard library: uses urllib, ssl, and json. Zero proprietary PI SDKs.
- Mandatory Kill-Switch: if OPC_BRIDGE_PI_OUTPUT_ENABLED is not strictly "true", blocks all HTTP calls.
- Absolute Secret Isolation: zero credentials, URLs, tokens or certificates logged or exposed to the UI.
- Fail-Safe & Backoff: PI failures never disrupt OPC collection, the UI, or other mappings.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def sanitize_error_message(msg: Any) -> str:
    """Sanitize error messages to guarantee no credentials, tokens or secrets leak into logs or DB."""
    if msg is None:
        return ""
    text = str(msg)
    # 1. Strip user:pass from URLs
    text = re.sub(r"://([^:]+):([^@]+)@", "://***:***@", text)
    # 2. Strip Bearer tokens
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/-]+", r"\1***", text, flags=re.IGNORECASE)
    # 3. Strip Basic auth base64 strings
    text = re.sub(r"(Basic\s+)[A-Za-z0-9+/=]+", r"\1***", text, flags=re.IGNORECASE)
    # 4. Strip passwords in query parameters
    text = re.sub(r"([?&](?:password|pwd|secret|token)=)[^&]+", r"\1***", text, flags=re.IGNORECASE)
    # Limit length
    if len(text) > 255:
        return text[:252] + "..."
    return text


def format_pi_timestamp(ts: Any) -> str:
    """Format any timestamp representation into ISO 8601 UTC string for PI Web API."""
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat()
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()
    if isinstance(ts, str):
        cleaned = ts.strip()
        if cleaned:
            try:
                dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                return cleaned
    return datetime.now(timezone.utc).isoformat()


# Backwards compatibility alias
format_iso_timestamp = format_pi_timestamp


def default_pi_point_name(opc_item_path: str, configured_pi_point: str | None = None) -> str:
    """Derive default target PI Point name from OPC tag path or configured name."""
    if configured_pi_point and configured_pi_point.strip():
        return configured_pi_point.strip()
    clean = (opc_item_path or "").strip()
    if not clean:
        return ""
    # In industrial setups, the last segment after '.' is typically the tag name
    # e.g., Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN -> DIAMETRO_CALC_BOBIN
    parts = clean.split(".")
    candidate = parts[-1].strip()
    return candidate if candidate else clean


@dataclass
class PiValuePayload:
    opc_item_path: str
    pi_point: str
    value: Any
    timestamp: Any = None
    quality: int | None = None


@dataclass
class PiPublishResult:
    opc_item_path: str
    pi_point: str
    value: Any
    status: str  # "published", "error", "skipped"
    error: str | None = None
    timestamp: str | None = None
    http_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "opc_item_path": self.opc_item_path,
            "pi_point": self.pi_point,
            "value": self.value,
            "status": self.status,
            "error": self.error,
            "timestamp": self.timestamp,
            "http_status": self.http_status,
        }


@dataclass
class PiOutputConfig:
    """Configuration for PI output channel loaded exclusively from external environment."""
    enabled: bool = False
    mode: str = "simulated"  # "simulated" or "web_api"
    base_url: str = ""       # e.g. "https://piserver.corp.local/piwebapi"
    data_server: str = "PIMS" # e.g. "PIMS"
    auth_type: str = "basic" # "basic", "bearer", "anonymous"
    username: str = ""
    password: str = ""
    bearer_token: str = ""
    timeout_seconds: float = 60.0
    ca_bundle: Optional[str] = None
    verify_ssl: bool = True

    @classmethod
    def load_from_env(cls) -> PiOutputConfig:
        enabled_raw = os.getenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "false").strip().lower()
        enabled = (enabled_raw == "true")

        mode_raw = os.getenv("OPC_BRIDGE_PI_OUTPUT_MODE", "simulated").strip().lower()
        mode = "web_api" if mode_raw == "web_api" else "simulated"

        base_url = (
            os.getenv("OPC_BRIDGE_PIWEBAPI_BASE_URL")
            or os.getenv("OPC_BRIDGE_PI_WEB_API_URL")
            or os.getenv("PI_WEB_API_BASE_URL", "")
        ).strip().rstrip("/")

        data_server = (
            os.getenv("OPC_BRIDGE_PI_SERVER")
            or os.getenv("OPC_BRIDGE_PI_DATA_SERVER")
            or os.getenv("PI_DATA_SERVER_NAME", "PIMS")
        ).strip()
        if not data_server:
            data_server = "PIMS"

        auth_type = (
            os.getenv("OPC_BRIDGE_PI_WEB_API_AUTH_TYPE")
            or os.getenv("PI_WEB_API_AUTH_MODE", "basic")
        ).strip().lower()
        if auth_type in ("none", "anonymous"):
            auth_type = "anonymous"
        elif auth_type not in ("basic", "bearer"):
            auth_type = "basic"

        username = (
            os.getenv("OPC_BRIDGE_PIWEBAPI_USERNAME")
            or os.getenv("OPC_BRIDGE_PI_WEB_API_USERNAME")
            or os.getenv("PI_WEB_API_USERNAME", "")
        )
        password = (
            os.getenv("OPC_BRIDGE_PIWEBAPI_PASSWORD")
            or os.getenv("OPC_BRIDGE_PI_WEB_API_PASSWORD")
            or os.getenv("PI_WEB_API_PASSWORD", "")
        )
        bearer_token = os.getenv("OPC_BRIDGE_PI_WEB_API_BEARER_TOKEN", "")

        timeout_raw = (
            os.getenv("OPC_BRIDGE_PIWEBAPI_TIMEOUT_SECONDS")
            or os.getenv("OPC_BRIDGE_PI_WEB_API_TIMEOUT_SECONDS")
            or os.getenv("PI_WEB_API_TIMEOUT_SECONDS", "60.0")
        )
        try:
            timeout_seconds = max(1.0, float(timeout_raw))
        except (ValueError, TypeError):
            timeout_seconds = 60.0

        ca_bundle = (
            os.getenv("OPC_BRIDGE_PIWEBAPI_CA_FILE")
            or os.getenv("OPC_BRIDGE_PI_WEB_API_CA_BUNDLE")
        )
        if ca_bundle and not ca_bundle.strip():
            ca_bundle = None

        verify_ssl_raw = (
            os.getenv("OPC_BRIDGE_PIWEBAPI_VERIFY_SSL")
            or os.getenv("OPC_BRIDGE_PI_WEB_API_VERIFY_SSL", "true")
        ).strip().lower()
        verify_ssl = (verify_ssl_raw != "false")

        return cls(
            enabled=enabled,
            mode=mode,
            base_url=base_url,
            data_server=data_server,
            auth_type=auth_type,
            username=username,
            password=password,
            bearer_token=bearer_token,
            timeout_seconds=timeout_seconds,
            ca_bundle=ca_bundle,
            verify_ssl=verify_ssl,
        )


class PiOutputDisabledError(RuntimeError):
    """Raised when an attempt to make a real PI call occurs while output is disabled."""
    pass


@dataclass
class PiOutputResult:
    pi_point_name: str
    value: Any
    status: str  # "Simulado", "Publicado", "Erro", "Desabilitado"
    timestamp: str
    point_source: str = ""
    location1: int = 0
    error: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pi_point_name": self.pi_point_name,
            "value": self.value,
            "status": self.status,
            "timestamp": self.timestamp,
            "point_source": self.point_source,
            "location1": self.location1,
            "error": self.error,
            "details": self.details,
        }


class PiOutputChannel(ABC):
    """Abstract interface for PI Point publication."""

    @abstractmethod
    def publish(
        self,
        pi_point_name: str,
        value: Any,
        timestamp: Optional[Any] = None,
        quality: Optional[int] = None,
        point_source: str = "",
        location1: int = 0,
    ) -> PiOutputResult:
        """Publish a collected process value to the destination PI Point."""
        pass

    @abstractmethod
    def test_connection(self) -> dict[str, Any]:
        """Test connectivity and authentication against PI Web API without writing any data."""
        pass


class SimulatedPiOutputChannel(PiOutputChannel):
    """Controlled simulation channel that records test/audit output without real PI writes."""

    def __init__(self, mode: str = "simulated") -> None:
        self.mode = mode

    def publish(
        self,
        pi_point_name: str,
        value: Any,
        timestamp: Optional[Any] = None,
        quality: Optional[int] = None,
        point_source: str = "",
        location1: int = 0,
    ) -> PiOutputResult:
        ts_str = format_iso_timestamp(timestamp)
        logger.info(
            "Simulated PI publish: point=%s, val=%s, ts=%s, ps=%s, loc1=%s, qual=%s",
            pi_point_name,
            value,
            ts_str,
            point_source,
            location1,
            quality,
        )
        return PiOutputResult(
            pi_point_name=pi_point_name,
            value=value,
            status="Simulado",
            timestamp=ts_str,
            point_source=point_source,
            location1=location1,
            error=None,
            details={
                "simulated": True,
                "quality": quality,
                "quality_text": "Good" if (quality is None or quality >= 192) else f"Bad ({quality})",
            },
        )

    def test_connection(self) -> dict[str, Any]:
        return {
            "connected": True,
            "mode": "simulated",
            "message": "Canal simulado ativo (nenhuma chamada de rede ou credencial necessária).",
        }


class PiWebApiOutputChannel(PiOutputChannel):
    """Production output channel publishing to OSIsoft/AVEVA PI Web API via HTTPS.

    Compatible workflow:
    1. Resolve WebId: GET {base_url}/points?path=\\{data_server}\\{pi_point_name} (cached in memory)
    2. Publish Value: POST {base_url}/streams/{WebId}/value?updateOption=Replace
       Content-Type: application/json
       Body: {"Timestamp": "<ISO>", "Value": <val>}
    """

    def __init__(
        self,
        config: Optional[PiOutputConfig] = None,
        *,
        base_url: Optional[str] = None,
        data_server: Optional[str] = None,
        auth_mode: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
        simulated: Optional[bool] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._web_id_cache: dict[str, str] = {}
        if config is not None:
            self.config = config
        else:
            env_sim = os.environ.get("PI_PUBLISH_SIMULATED", "false").lower() in ("true", "1", "yes")
            is_sim = simulated if simulated is not None else env_sim

            if enabled is not None:
                eff_enabled = enabled
            elif simulated is not None:
                eff_enabled = not is_sim
            else:
                eff_enabled = os.environ.get("OPC_BRIDGE_PI_OUTPUT_ENABLED", "false").lower() == "true"

            eff_base_url = (
                base_url
                or os.environ.get("OPC_BRIDGE_PIWEBAPI_BASE_URL")
                or os.environ.get("OPC_BRIDGE_PI_WEB_API_URL")
                or os.environ.get("PI_WEB_API_BASE_URL", "")
            ).rstrip("/")
            eff_data_server = (
                data_server
                or os.environ.get("OPC_BRIDGE_PI_SERVER")
                or os.environ.get("OPC_BRIDGE_PI_DATA_SERVER")
                or os.environ.get("PI_DATA_SERVER_NAME", "PIMS")
            )
            eff_auth_type = (
                auth_mode
                or os.environ.get("OPC_BRIDGE_PI_WEB_API_AUTH_TYPE")
                or os.environ.get("PI_WEB_API_AUTH_MODE", "basic")
            ).lower()
            eff_username = (
                username
                or os.environ.get("OPC_BRIDGE_PIWEBAPI_USERNAME")
                or os.environ.get("OPC_BRIDGE_PI_WEB_API_USERNAME")
                or os.environ.get("PI_WEB_API_USERNAME", "")
            )
            eff_password = (
                password
                or os.environ.get("OPC_BRIDGE_PIWEBAPI_PASSWORD")
                or os.environ.get("OPC_BRIDGE_PI_WEB_API_PASSWORD")
                or os.environ.get("PI_WEB_API_PASSWORD", "")
            )
            eff_verify_ssl = (
                verify_ssl
                if verify_ssl is not None
                else (os.environ.get("OPC_BRIDGE_PIWEBAPI_VERIFY_SSL", "true").lower() != "false")
            )
            eff_timeout = (
                timeout_seconds
                if timeout_seconds is not None
                else float(
                    os.environ.get("OPC_BRIDGE_PIWEBAPI_TIMEOUT_SECONDS")
                    or os.environ.get("OPC_BRIDGE_PI_WEB_API_TIMEOUT_SECONDS", "60.0")
                )
            )
            eff_ca_bundle = (
                os.environ.get("OPC_BRIDGE_PIWEBAPI_CA_FILE")
                or os.environ.get("OPC_BRIDGE_PI_WEB_API_CA_BUNDLE")
            )
            if eff_ca_bundle and not eff_ca_bundle.strip():
                eff_ca_bundle = None

            self.config = PiOutputConfig(
                enabled=eff_enabled,
                mode="simulated" if is_sim else "web_api",
                base_url=eff_base_url,
                data_server=eff_data_server,
                auth_type=eff_auth_type,
                username=eff_username,
                password=eff_password,
                verify_ssl=eff_verify_ssl,
                timeout_seconds=eff_timeout,
                ca_bundle=eff_ca_bundle,
            )

    @property
    def base_url(self) -> str:
        return self.config.base_url

    @base_url.setter
    def base_url(self, val: str) -> None:
        self.config.base_url = (val or "").rstrip("/")

    @property
    def data_server(self) -> str:
        return self.config.data_server

    @data_server.setter
    def data_server(self, val: str) -> None:
        self.config.data_server = val or "PIMS"

    @property
    def auth_mode(self) -> str:
        return self.config.auth_type

    @auth_mode.setter
    def auth_mode(self, val: str) -> None:
        self.config.auth_type = val

    @property
    def username(self) -> str:
        return self.config.username

    @username.setter
    def username(self, val: str) -> None:
        self.config.username = val

    @property
    def password(self) -> str:
        return self.config.password

    @password.setter
    def password(self, val: str) -> None:
        self.config.password = val

    @property
    def verify_ssl(self) -> bool:
        return self.config.verify_ssl

    @verify_ssl.setter
    def verify_ssl(self, val: bool) -> None:
        self.config.verify_ssl = val

    @property
    def timeout_seconds(self) -> float:
        return self.config.timeout_seconds

    @timeout_seconds.setter
    def timeout_seconds(self, val: float) -> None:
        self.config.timeout_seconds = val

    @property
    def simulated(self) -> bool:
        return not self.config.enabled or self.config.mode == "simulated"

    @simulated.setter
    def simulated(self, val: bool) -> None:
        if val:
            self.config.mode = "simulated"
        else:
            self.config.mode = "web_api"
            self.config.enabled = True

    def _build_request(self, url: str, method: str, body: Optional[dict[str, Any]] = None) -> urllib.request.Request:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Requested-With": "OPC-Bridge",
            "User-Agent": "OPC-Bridge-PI-Integration/1.0",
        }
        data_bytes = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        # In-memory Basic Auth assembly only when username and password are provided
        if (self.config.auth_type == "basic" or self.auth_mode == "basic") and self.username and self.password:
            user_pass = f"{self.username}:{self.password}".encode("utf-8")
            encoded = base64.b64encode(user_pass).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        elif self.config.auth_type == "bearer" and self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"

        return urllib.request.Request(url=url, data=data_bytes, headers=headers, method=method)

    def _get_ssl_context(self) -> ssl.SSLContext:
        if not self.config.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.config.ca_bundle:
            return ssl.create_default_context(cafile=self.config.ca_bundle)
        return ssl.create_default_context()

    def _execute_http(self, req: urllib.request.Request) -> tuple[int, Any]:
        # MANDATORY INDUSTRIAL SAFETY KILL-SWITCH
        if not self.config.enabled:
            logger.info("Saída PI desabilitada")
            raise PiOutputDisabledError("Saída PI desabilitada")

        ssl_ctx = self._get_ssl_context()
        with urllib.request.urlopen(req, timeout=self.config.timeout_seconds, context=ssl_ctx) as response:
            code = getattr(response, "status", None) or response.getcode()
            body_bytes = response.read()
            if not body_bytes:
                return code, {}
            try:
                return code, json.loads(body_bytes.decode("utf-8"))
            except Exception:
                return code, {"raw": body_bytes.decode("utf-8", errors="replace")}

    def resolve_point_web_id(self, pi_point_name: str) -> str:
        """Resolve destination PI Point WebId via GET /points?path=\\{data_server}\\{pi_point_name}.

        Caches WebId in memory per PI Point name to avoid redundant network lookups.
        """
        clean_point = (pi_point_name or "").strip()
        if not clean_point:
            raise ValueError("Ponto PI de destino não informado")

        if clean_point in self._web_id_cache:
            return self._web_id_cache[clean_point]

        path_param = f"\\\\{self.data_server}\\{clean_point}"
        url = f"{self.base_url}/points?path={urllib.parse.quote(path_param)}"
        req = self._build_request(url, "GET")
        code, resp_data = self._execute_http(req)

        if not (200 <= code < 300) or not isinstance(resp_data, dict):
            raise RuntimeError(f"Falha ao resolver ponto PI '{clean_point}' (HTTP {code})")

        web_id = resp_data.get("WebId")
        if not web_id:
            raise RuntimeError(f"WebId não encontrado para o ponto PI '{clean_point}'")

        self._web_id_cache[clean_point] = str(web_id)
        return str(web_id)

    def _resolve_stream_url(self, pi_point_name: str) -> str:
        """Build PI Web API stream value URL with updateOption=Replace using resolved WebId."""
        web_id = self.resolve_point_web_id(pi_point_name)
        return f"{self.base_url}/streams/{web_id}/value?updateOption=Replace"

    def publish(
        self,
        pi_point_name: str,
        value: Any,
        timestamp: Optional[Any] = None,
        quality: Optional[int] = None,
        point_source: str = "",
        location1: int = 0,
    ) -> PiOutputResult:
        iso_ts = format_pi_timestamp(timestamp)

        clean_point = (pi_point_name or "").strip()
        if not clean_point:
            return PiOutputResult(
                pi_point_name="",
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error="Ponto PI de destino não informado",
                details={},
            )

        # KILL-SWITCH CHECK: Mandatory Industrial Safety
        if not self.config.enabled:
            logger.info("Saída PI desabilitada")
            return PiOutputResult(
                pi_point_name=clean_point,
                value=value,
                status="Desabilitado",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error="Saída PI desabilitada",
                details={"enabled": False},
            )

        if not self.config.base_url:
            return PiOutputResult(
                pi_point_name=clean_point,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error="URL base do PI Web API não configurada",
                details={"missing_config": "OPC_BRIDGE_PIWEBAPI_BASE_URL"},
            )

        stream_payload = {
            "Timestamp": iso_ts,
            "Value": value,
        }

        try:
            target_url = self._resolve_stream_url(clean_point)
            req = self._build_request(target_url, "POST", body=stream_payload)
            code, resp_data = self._execute_http(req)

            if code in (200, 201, 202, 204):
                logger.info("PI publish success: point=%s, status=%d", clean_point, code)
                return PiOutputResult(
                    pi_point_name=clean_point,
                    value=value,
                    status="Publicado",
                    timestamp=iso_ts,
                    point_source=point_source,
                    location1=location1,
                    error=None,
                    details={"status_code": code, "web_id": self._web_id_cache.get(clean_point, ""), "web_api": True},
                )
            sanitized_body = sanitize_error_message(resp_data)
            err_msg = f"PI Web API retornou HTTP {code}"
            if sanitized_body:
                err_msg += f": {sanitized_body}"
            logger.warning("PI publish rejected for %s: %s", clean_point, err_msg)
            return PiOutputResult(
                pi_point_name=clean_point,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error=err_msg,
                details={"status_code": code},
            )

        except urllib.error.HTTPError as exc:
            err_msg = f"HTTP {exc.code}: {exc.reason}"
            if exc.code == 404:
                err_msg = f"Ponto PI '{clean_point}' não encontrado no servidor {self.data_server} (HTTP 404)"
            elif exc.code in (401, 403):
                err_msg = f"Acesso não autorizado ao PI Web API (HTTP {exc.code})"
            sanitized = sanitize_error_message(err_msg)
            logger.warning("PI publish HTTP error for %s: %s", clean_point, sanitized)
            return PiOutputResult(
                pi_point_name=clean_point,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error=sanitized,
                details={"status_code": exc.code},
            )
        except Exception as exc:
            err_msg = f"Falha de comunicação com PI Web API: {exc}"
            sanitized = sanitize_error_message(err_msg)
            logger.warning("PI publish error for %s: %s", clean_point, sanitized)
            return PiOutputResult(
                pi_point_name=clean_point,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error=sanitized,
                details={},
            )

    def publish_single(self, item: PiValuePayload) -> PiPublishResult:
        """Publish a single already-read OPC value into the target PI Point."""
        ts_str = format_pi_timestamp(item.timestamp)

        clean_point = (item.pi_point or "").strip()
        if not clean_point:
            return PiPublishResult(
                opc_item_path=item.opc_item_path,
                pi_point="",
                value=item.value,
                status="error",
                error="Ponto PI de destino não informado",
                timestamp=ts_str,
            )

        if not self.config.enabled and self.config.mode != "simulated":
            logger.info("Saída PI desabilitada")
            return PiPublishResult(
                opc_item_path=item.opc_item_path,
                pi_point=clean_point,
                value=item.value,
                status="error",
                error="Saída PI desabilitada",
                timestamp=ts_str,
                http_status=None,
            )

        if self.simulated:
            logger.info(
                "Simulating PI publication: opc=%s -> pi=%s, val=%s, ts=%s",
                item.opc_item_path,
                clean_point,
                item.value,
                ts_str,
            )
            return PiPublishResult(
                opc_item_path=item.opc_item_path,
                pi_point=clean_point,
                value=item.value,
                status="published",
                error=None,
                timestamp=ts_str,
                http_status=200,
            )

        out_res = self.publish(
            pi_point_name=clean_point,
            value=item.value,
            timestamp=item.timestamp,
            quality=item.quality,
        )

        status_str = "published" if out_res.status == "Publicado" else "error"
        return PiPublishResult(
            opc_item_path=item.opc_item_path,
            pi_point=clean_point,
            value=item.value,
            status=status_str,
            error=out_res.error,
            timestamp=out_res.timestamp,
            http_status=out_res.details.get("status_code"),
        )

    def publish_batch(self, items: list[PiValuePayload]) -> list[PiPublishResult]:
        """Publish a batch of already-read values to PI Points."""
        return [self.publish_single(it) for it in items]

    def test_connection(self) -> dict[str, Any]:
        """Query PI Web API read-only health/system endpoint without writing any process data."""
        if not self.config.enabled:
            logger.info("Saída PI desabilitada")
            return {
                "connected": False,
                "mode": "web_api",
                "error": "output_disabled",
                "message": "Saída PI desabilitada. Configure OPC_BRIDGE_PI_OUTPUT_ENABLED=true para conectar ao servidor real.",
            }

        if not self.config.base_url:
            return {
                "connected": False,
                "mode": "web_api",
                "error": "missing_base_url",
                "message": "URL base do PI Web API não informada.",
            }

        test_url = f"{self.config.base_url}/system/landing"
        try:
            req = self._build_request(test_url, "GET")
            code, data = self._execute_http(req)
            if 200 <= code < 300:
                prod_title = data.get("ProductTitle") if isinstance(data, dict) else None
                msg = f"Conexão com PI Web API verificada com sucesso (HTTP {code})."
                if prod_title:
                    msg += f" Sistema: {prod_title}."
                return {
                    "connected": True,
                    "mode": "web_api",
                    "status_code": code,
                    "message": msg,
                }
            return {
                "connected": False,
                "mode": "web_api",
                "status_code": code,
                "message": f"Resposta inesperada do servidor PI Web API (HTTP {code}).",
            }
        except urllib.error.HTTPError as exc:
            sanitized = sanitize_error_message(f"HTTP {exc.code} {exc.reason}")
            return {
                "connected": False,
                "mode": "web_api",
                "status_code": exc.code,
                "error": sanitized,
                "message": f"Falha de autenticação ou acesso ao PI Web API: {sanitized}.",
            }
        except Exception as exc:
            sanitized = sanitize_error_message(str(exc))
            return {
                "connected": False,
                "mode": "web_api",
                "error": sanitized,
                "message": f"Erro de comunicação com PI Web API: {sanitized}.",
            }


class PiPublisher(PiWebApiOutputChannel):
    """Client for publishing read process values into OSIsoft / AVEVA PI Web API."""

    def __init__(
        self,
        base_url: str | None = None,
        data_server: str | None = None,
        auth_mode: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_ssl: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
        simulated: bool = False,
        config: Optional[PiOutputConfig] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        super().__init__(
            config=config,
            base_url=base_url,
            data_server=data_server,
            auth_mode=auth_mode,
            username=username,
            password=password,
            verify_ssl=verify_ssl if verify_ssl is not None else True,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else 60.0,
            simulated=simulated,
            enabled=enabled,
        )


def create_pi_output_channel(config: Optional[PiOutputConfig] = None) -> PiOutputChannel:
    """Factory selecting SimulatedPiOutputChannel by default or PiWebApiOutputChannel when fully configured."""
    cfg = config if config is not None else PiOutputConfig.load_from_env()
    if cfg.enabled and cfg.mode == "web_api" and cfg.base_url:
        return PiWebApiOutputChannel(cfg)
    return SimulatedPiOutputChannel(mode=cfg.mode)


def evaluate_mapping_publication(
    mapping: dict[str, Any],
    live_item: Optional[dict[str, Any]],
    channel: PiOutputChannel,
    config: PiOutputConfig,
    now_dt: Optional[datetime] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Evaluate and optionally publish a mapping from current cached OPC live reading.

    Guarantees:
    - Never triggers OPC calls, COM reads or CONFIG_PUSH.
    - Only publishes when: enabled, value is present, quality is Good (>=192), data is not stale.
    - Unless force=True, respects publish_interval_ms.
    - Applies backoff to failures without disrupting other mappings.
    """
    now = now_dt or datetime.now(timezone.utc)
    interval_ms = mapping.get("publish_interval_ms", 5000)
    mapping_id = mapping.get("mapping_id", "")
    point_name = mapping.get("pi_point_name", "")

    # 1. Enabled check
    if not mapping.get("enabled"):
        return {"action": "skipped", "reason": "mapping_disabled", "message": "Mapeamento desativado."}

    # 2. Reading validation
    if live_item is None:
        return {"action": "skipped", "reason": "no_opc_reading", "message": "Nenhuma leitura OPC no cache."}

    val = live_item.get("value")
    if val is None:
        return {"action": "skipped", "reason": "null_value", "message": "Valor OPC ausente (None)."}

    quality = live_item.get("quality")
    if quality is not None and quality < 192:
        return {
            "action": "skipped",
            "reason": "bad_quality",
            "quality": quality,
            "message": f"Qualidade OPC ruim (Bad: {quality}). Publicação bloqueada.",
        }

    is_stale = bool(live_item.get("stale") or live_item.get("status") == "stale")
    if is_stale:
        return {"action": "skipped", "reason": "stale_data", "message": "Dado OPC desatualizado / obsoleto. Publicação bloqueada."}

    # 3. Interval check (unless force=True)
    if not force:
        next_due_raw = mapping.get("next_publish_due_at")
        if next_due_raw:
            try:
                next_due_dt = datetime.fromisoformat(str(next_due_raw).replace("Z", "+00:00"))
                if next_due_dt.tzinfo is None:
                    next_due_dt = next_due_dt.replace(tzinfo=timezone.utc)
                if now < next_due_dt:
                    return {
                        "action": "skipped",
                        "reason": "interval_not_elapsed",
                        "next_due": next_due_dt.isoformat(),
                        "message": "Intervalo de publicação ainda não decorrido.",
                    }
            except Exception:
                pass

    # 4. Publication
    opc_ts = live_item.get("opc_timestamp") or live_item.get("received_at")
    res = channel.publish(
        pi_point_name=point_name,
        value=val,
        timestamp=opc_ts,
        quality=quality,
        point_source=mapping.get("point_source", ""),
        location1=mapping.get("location1", 0),
    )

    # 5. Calculate next due and backoff
    curr_failures = int(mapping.get("failure_count") or 0)
    if res.status in ("Publicado", "Simulado"):
        new_failure_count = 0
        next_due = now + timedelta(milliseconds=interval_ms)
        clean_error = None
    else:
        new_failure_count = curr_failures + 1
        # Exponential backoff capped at 5 minutes (300,000 ms)
        backoff_ms = min(interval_ms * (2 ** min(new_failure_count - 1, 5)), 300000)
        next_due = now + timedelta(milliseconds=backoff_ms)
        clean_error = sanitize_error_message(res.error or "Falha de publicação")

    return {
        "action": "published",
        "result": res,
        "new_status": res.status,
        "value": val,
        "quality": quality,
        "timestamp": format_iso_timestamp(opc_ts),
        "next_publish_due_at": next_due.isoformat(),
        "error": clean_error,
        "failure_count": new_failure_count,
    }
