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


def format_iso_timestamp(ts: Any) -> str:
    """Format any timestamp representation into ISO 8601 UTC string."""
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


@dataclass
class PiOutputConfig:
    """Configuration for PI output channel loaded exclusively from external environment."""
    enabled: bool = False
    mode: str = "simulated"  # "simulated" or "web_api"
    base_url: str = ""       # e.g. "https://piserver.corp.local/piwebapi"
    auth_type: str = "basic" # "basic", "bearer", "anonymous"
    username: str = ""
    password: str = ""
    bearer_token: str = ""
    timeout_seconds: float = 10.0
    ca_bundle: Optional[str] = None
    verify_ssl: bool = True

    @classmethod
    def load_from_env(cls) -> PiOutputConfig:
        enabled_raw = os.getenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "false").strip().lower()
        enabled = (enabled_raw == "true")

        mode_raw = os.getenv("OPC_BRIDGE_PI_OUTPUT_MODE", "simulated").strip().lower()
        mode = "web_api" if mode_raw == "web_api" else "simulated"

        base_url = os.getenv("OPC_BRIDGE_PI_WEB_API_URL", "").strip().rstrip("/")
        auth_type = os.getenv("OPC_BRIDGE_PI_WEB_API_AUTH_TYPE", "basic").strip().lower()
        if auth_type not in ("basic", "bearer", "anonymous"):
            auth_type = "basic"

        username = os.getenv("OPC_BRIDGE_PI_WEB_API_USERNAME", "")
        password = os.getenv("OPC_BRIDGE_PI_WEB_API_PASSWORD", "")
        bearer_token = os.getenv("OPC_BRIDGE_PI_WEB_API_BEARER_TOKEN", "")

        timeout_raw = os.getenv("OPC_BRIDGE_PI_WEB_API_TIMEOUT_SECONDS", "10.0")
        try:
            timeout_seconds = max(1.0, float(timeout_raw))
        except (ValueError, TypeError):
            timeout_seconds = 10.0

        ca_bundle = os.getenv("OPC_BRIDGE_PI_WEB_API_CA_BUNDLE")
        if ca_bundle and not ca_bundle.strip():
            ca_bundle = None

        verify_ssl_raw = os.getenv("OPC_BRIDGE_PI_WEB_API_VERIFY_SSL", "true").strip().lower()
        verify_ssl = (verify_ssl_raw != "false")

        return cls(
            enabled=enabled,
            mode=mode,
            base_url=base_url,
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
    """Production output channel publishing to OSIsoft/AVEVA PI Web API via HTTPS."""

    def __init__(self, config: PiOutputConfig) -> None:
        self.config = config
        self._web_id_cache: dict[str, str] = {}

    def _build_request(self, url: str, method: str, body: Optional[dict[str, Any]] = None) -> urllib.request.Request:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Requested-With": "OPC-Bridge",
            "User-Agent": "OPC-Bridge-PI-Integration/1.0",
        }
        data_bytes = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data_bytes = json.dumps(body).encode("utf-8")

        if self.config.auth_type == "basic" and self.config.username:
            user_pass = f"{self.config.username}:{self.config.password}"
            encoded = base64.b64encode(user_pass.encode("utf-8")).decode("ascii")
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
            code = response.getcode()
            body_bytes = response.read()
            if not body_bytes:
                return code, {}
            try:
                return code, json.loads(body_bytes.decode("utf-8"))
            except Exception:
                return code, {"raw": body_bytes.decode("utf-8", errors="replace")}

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
                "message": "URL base do PI Web API não informada (OPC_BRIDGE_PI_WEB_API_URL).",
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

    def _resolve_stream_url(self, pi_point_name: str) -> str:
        """Resolve destination PI Web API stream URL, caching WebId when discovered."""
        if pi_point_name in self._web_id_cache:
            return f"{self.config.base_url}/streams/{self._web_id_cache[pi_point_name]}/value"

        # Query points search
        try:
            encoded_name = urllib.parse.quote(pi_point_name)
            search_url = f"{self.config.base_url}/points/search?query=name:{encoded_name}"
            req = self._build_request(search_url, "GET")
            code, data = self._execute_http(req)
            if 200 <= code < 300 and isinstance(data, dict):
                items = data.get("Items", [])
                if items and isinstance(items[0], dict) and items[0].get("WebId"):
                    web_id = items[0]["WebId"]
                    self._web_id_cache[pi_point_name] = web_id
                    return f"{self.config.base_url}/streams/{web_id}/value"
                if data.get("WebId"):
                    web_id = data["WebId"]
                    self._web_id_cache[pi_point_name] = web_id
                    return f"{self.config.base_url}/streams/{web_id}/value"
        except Exception as exc:
            logger.debug("WebId resolution skipped for %s: %s", pi_point_name, sanitize_error_message(exc))

        # Direct point stream endpoint fallback (standard in mocks and proxies)
        encoded_name = urllib.parse.quote(pi_point_name)
        return f"{self.config.base_url}/streams/{encoded_name}/value"

    def publish(
        self,
        pi_point_name: str,
        value: Any,
        timestamp: Optional[Any] = None,
        quality: Optional[int] = None,
        point_source: str = "",
        location1: int = 0,
    ) -> PiOutputResult:
        iso_ts = format_iso_timestamp(timestamp)

        # KILL-SWITCH CHECK
        if not self.config.enabled:
            logger.info("Saída PI desabilitada")
            return PiOutputResult(
                pi_point_name=pi_point_name,
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
                pi_point_name=pi_point_name,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error="URL base do PI Web API não configurada",
                details={"missing_config": "OPC_BRIDGE_PI_WEB_API_URL"},
            )

        is_good = quality is None or quality >= 192
        stream_payload = {
            "Timestamp": iso_ts,
            "Value": value,
            "Good": is_good,
        }

        try:
            target_url = self._resolve_stream_url(pi_point_name)
            req = self._build_request(target_url, "POST", body=stream_payload)
            code, resp_data = self._execute_http(req)

            if 200 <= code < 300:
                logger.info("PI Web API publish success: %s = %s (%s)", pi_point_name, value, iso_ts)
                return PiOutputResult(
                    pi_point_name=pi_point_name,
                    value=value,
                    status="Publicado",
                    timestamp=iso_ts,
                    point_source=point_source,
                    location1=location1,
                    error=None,
                    details={"status_code": code, "web_api": True},
                )
            sanitized_body = sanitize_error_message(resp_data)
            err_msg = f"HTTP {code}: {sanitized_body}"
            logger.warning("PI Web API publish rejected (%s): %s", pi_point_name, err_msg)
            return PiOutputResult(
                pi_point_name=pi_point_name,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error=err_msg,
                details={"status_code": code},
            )

        except urllib.error.HTTPError as exc:
            err_msg = sanitize_error_message(f"HTTP {exc.code} {exc.reason}")
            logger.warning("PI Web API publish error for %s: %s", pi_point_name, err_msg)
            return PiOutputResult(
                pi_point_name=pi_point_name,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error=err_msg,
                details={"status_code": exc.code},
            )
        except Exception as exc:
            err_msg = sanitize_error_message(str(exc))
            logger.warning("PI Web API publish connection failure for %s: %s", pi_point_name, err_msg)
            return PiOutputResult(
                pi_point_name=pi_point_name,
                value=value,
                status="Erro",
                timestamp=iso_ts,
                point_source=point_source,
                location1=location1,
                error=err_msg,
                details={},
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
