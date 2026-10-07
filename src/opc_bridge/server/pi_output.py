"""Abstract and simulated output channel for PI System (OSIsoft / AVEVA PI) integration.

Guarantees:
- Pure output destination: never writes to OPC source tags.
- Abstract interface: prepared for future PI Data Archive / Web API integration.
- Controlled simulation: records test/audit output without contacting real PI Data Archive.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
class PiOutputResult:
    pi_point_name: str
    value: Any
    status: str  # "Simulado", "Publicado", "Erro"
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
