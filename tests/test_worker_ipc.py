"""Regression tests for the serialized OPC worker boundary."""
from __future__ import annotations

import asyncio
import pickle

from opc_bridge.adapters.da import (
    DevelopmentComGroup,
    DevelopmentComGroups,
    DevelopmentComServer,
    OpcDaAdapter,
)
from opc_bridge.adapters.da_worker import process_worker_command
from opc_bridge.adapters.simulated import SimulatedOpcAdapter
from opc_bridge.adapters.supervised import SupervisedOpcAdapter
from opc_bridge.agent.client import AgentClient
from opc_bridge.protocol import ConfigAckPayload, ConfigPushPayload, ItemRef, MsgType


class NonPicklableComGroup(DevelopmentComGroup):
    """Stand-in for a PyIDispatch group that cannot cross process boundaries."""

    def __reduce__(self):
        raise TypeError("cannot pickle 'PyIDispatch' object")


class NonPicklableGroups(DevelopmentComGroups):
    def Add(self, name: str) -> NonPicklableComGroup:
        group = NonPicklableComGroup(name)
        self._groups[name] = group
        return group


class NonPicklableComServer(DevelopmentComServer):
    def __init__(self) -> None:
        super().__init__()
        self.OPCGroups = NonPicklableGroups()


class PickleBoundaryAdapter(SupervisedOpcAdapter):
    """Exercise worker commands with pickle on both sides of each IPC call."""

    def __init__(self) -> None:
        super().__init__()
        self.worker_adapter = OpcDaAdapter(com_factory=NonPicklableComServer)

    def _start_worker(self) -> None:
        """The test dispatches commands in-process but retains the pickle boundary."""

    def _execute(self, msg, timeout=None, allow_retry=True):
        request = pickle.loads(pickle.dumps(msg))
        response = process_worker_command(self.worker_adapter, request)
        return pickle.loads(pickle.dumps(response))


def test_config_push_keeps_nonpicklable_com_group_inside_worker() -> None:
    async def run() -> None:
        acknowledgements = []

        async def capture_send(msg_type, payload):
            acknowledgements.append((msg_type, payload))

        client = AgentClient(
            server_host="127.0.0.1",
            server_port=1,
            agent_id="ipc-regression-agent",
            auth_token_hash=b"x" * 32,
            adapter=SimulatedOpcAdapter(),
            adapter_factory=PickleBoundaryAdapter,
        )
        client._send = capture_send

        await client._handle_config_push(
            ConfigPushPayload(
                config_version=3,
                update_rate_ms=5000,
                items=[ItemRef(1, "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN")],
                opc_prog_id="ABB.AfwOpcDaSurrogate.1",
            ).pack()
        )

        assert len(acknowledgements) == 1
        msg_type, payload = acknowledgements[0]
        assert msg_type == MsgType.CONFIG_ACK
        assert ConfigAckPayload.unpack(payload).applied is True
        assert client.config_version == 3
        assert client._group_handle.native_handle is None

        adapter = client._adapter
        assert isinstance(adapter, PickleBoundaryAdapter)
        assert adapter.worker_adapter._groups
        native_group = next(iter(adapter.worker_adapter._groups.values()))["native_group"]
        assert isinstance(native_group, NonPicklableComGroup)

    asyncio.run(run())
