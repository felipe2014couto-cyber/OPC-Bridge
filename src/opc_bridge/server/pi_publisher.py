"""Background publisher service for periodic OPC-to-PI mappings.

Industrial Guarantees:
- Zero impact on OPC collection: strictly consumes already-cached live values.
- Kill-switch enforced: does nothing when OPC_BRIDGE_PI_OUTPUT_ENABLED is false.
- Isolated failure handling: individual mapping failures are caught, logged, and backed off without affecting others.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, List, Optional

from opc_bridge.server.pi_output import (
    PiOutputChannel,
    PiOutputConfig,
    PiOutputDisabledError,
    PiOutputResult,
    PiPublisher,
    PiPublishResult,
    PiValuePayload,
    PiWebApiOutputChannel,
    SimulatedPiOutputChannel,
    create_pi_output_channel,
    default_pi_point_name,
    evaluate_mapping_publication,
    format_pi_timestamp,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PiPublisherService",
    "PiPublisher",
    "PiValuePayload",
    "PiPublishResult",
    "default_pi_point_name",
    "format_pi_timestamp",
]


class PiPublisherService:
    """Service evaluating and publishing active OPC-to-PI mappings when due."""

    def __init__(
        self,
        bridge_server: Any,
        database: Any,
        config: Optional[PiOutputConfig] = None,
        channel: Optional[PiOutputChannel] = None,
    ) -> None:
        self.bridge_server = bridge_server
        self.database = database
        self.config = config if config is not None else PiOutputConfig.load_from_env()
        self.channel = channel if channel is not None else create_pi_output_channel(self.config)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def publish_due_cycle(self) -> List[dict[str, Any]]:
        """Run a single evaluation cycle of all active mappings.

        Returns list of published mapping results.
        """
        # Kill-switch: do nothing if output is not enabled
        if not self.config.enabled:
            return []

        try:
            with self.database.session() as repo:
                mappings = repo.list_pi_mappings()
        except Exception as exc:
            logger.error("Failed to list mappings for PI publisher: %s", sanitize_error_message(exc))
            return []

        if not mappings:
            return []

        # Gather cached live values for relevant agents (pure read-only from memory cache)
        live_cache: dict[tuple[str, str], dict[str, Any]] = {}
        if self.bridge_server is not None:
            seen_agents = {m["agent_id"] for m in mappings if m.get("agent_id")}
            for ag_id in seen_agents:
                try:
                    data = self.bridge_server.get_live_values(ag_id)
                    for item in data.get("items", []):
                        live_cache[(ag_id, item.get("opc_item_path", ""))] = item
                except Exception as exc:
                    logger.debug("Failed to read live cache for agent %s: %s", ag_id, sanitize_error_message(exc))

        results: List[dict[str, Any]] = []

        for m in mappings:
            # Only process enabled mappings
            if not m.get("enabled"):
                continue

            ag_id = m.get("agent_id") or ""
            opc_tag = m.get("opc_item_path") or ""
            live_item = live_cache.get((ag_id, opc_tag))

            try:
                eval_res = evaluate_mapping_publication(
                    mapping=m,
                    live_item=live_item,
                    channel=self.channel,
                    config=self.config,
                )

                if eval_res.get("action") == "published":
                    with self.database.session() as repo:
                        repo.update_pi_mapping_publication(
                            mapping_id=m["mapping_id"],
                            status=eval_res["new_status"],
                            published_value=str(eval_res["value"]),
                            next_publish_due_at=eval_res.get("next_publish_due_at"),
                            error=eval_res.get("error"),
                            failure_count=eval_res.get("failure_count"),
                        )
                        if ag_id:
                            repo.add_audit_event(
                                ag_id,
                                str(uuid.uuid4()),
                                "pi_mapping.published",
                                json.dumps({
                                    "mapping_id": m["mapping_id"],
                                    "pi_point_name": m["pi_point_name"],
                                    "opc_item_path": opc_tag,
                                    "value": eval_res["value"],
                                    "quality": eval_res["quality"],
                                    "status": eval_res["new_status"],
                                    "mode": self.config.mode,
                                    "error": eval_res.get("error"),
                                }),
                            )
                    results.append(eval_res)

            except Exception as exc:
                sanitized = sanitize_error_message(exc)
                logger.error("Error publishing mapping %s (%s): %s", m.get("mapping_id"), m.get("pi_point_name"), sanitized)
                # Ensure isolated failure is recorded without crashing
                try:
                    with self.database.session() as repo:
                        repo.update_pi_mapping_publication(
                            mapping_id=m["mapping_id"],
                            status="Erro",
                            error=sanitized,
                            failure_count=int(m.get("failure_count") or 0) + 1,
                        )
                except Exception:
                    pass

        return results

    def start(self, interval_seconds: float = 1.0) -> None:
        """Start background polling thread."""
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                try:
                    self.publish_due_cycle()
                except Exception as exc:
                    logger.debug("PI publisher cycle exception: %s", sanitize_error_message(exc))
                time.sleep(interval_seconds)

        self._thread = threading.Thread(target=_loop, daemon=True, name="PiPublisherService")
        self._thread.start()

    def stop(self) -> None:
        """Stop background publisher thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
