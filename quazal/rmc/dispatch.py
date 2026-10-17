"""RMC protocol registry and dispatch.

Protocols subclass :class:`Protocol` and mark handlers with :func:`method`::

    class SecureProtocol(Protocol):
        protocol_id = 0x0B

        @method(4, "RegisterEx")
        async def register_ex(self, call: Call) -> bytes:
            urls = call.args.string_list()
            ...
            return Writer().u32(0x10001).u32(cid).string(url).bytes()

A handler reads its parameters from ``call.args`` and returns the response
body (``bytes``, a :class:`~quazal.ddl.Writer`, a
:class:`~quazal.ddl.Structure` or ``None`` for an empty body). Raising
:class:`RMCError` answers with a Quazal error code instead. Work that must
happen *after* the response is on the wire (notifying other players, for
example) is queued with :meth:`Call.after`.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ClassVar

from ..ddl import Reader, Structure, Writer
from ..errors import QError
from .message import RMCError, RMCMessage

if TYPE_CHECKING:
    from ..prudp.transport import Client, Connection

log = logging.getLogger("quazal.rmc")

Handler = Callable[[Any, "Call"], Awaitable[Any]]


@dataclass
class MethodInfo:
    id: int
    name: str
    func: Handler
    requires_login: bool


def method(method_id: int, name: str | None = None, *, requires_login: bool = True):
    """Mark a coroutine method of a :class:`Protocol` as an RMC handler."""

    def decorate(func: Handler) -> Handler:
        func.__rmc_method__ = MethodInfo(method_id, name or func.__name__, func, requires_login)  # type: ignore[attr-defined]
        return func

    return decorate


class Protocol:
    protocol_id: ClassVar[int]
    name: ClassVar[str] = ""
    _methods: ClassVar[dict[int, MethodInfo]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        methods: dict[int, MethodInfo] = {}
        for klass in reversed(cls.__mro__):
            for attr in vars(klass).values():
                info = getattr(attr, "__rmc_method__", None)
                if info is not None:
                    methods[info.id] = info
        cls._methods = methods
        if not cls.name:
            cls.name = cls.__name__.removesuffix("Protocol")

    def methods(self) -> dict[int, MethodInfo]:
        return self._methods


@dataclass
class Call:
    connection: "Connection"
    message: RMCMessage
    protocol: Protocol
    method: MethodInfo
    args: Reader
    _after: list[Callable[[], Any]] = field(default_factory=list)

    @property
    def client(self) -> "Client":
        return self.connection.client

    @property
    def server(self):
        return self.connection.socket.server

    def after(self, action: Callable[[], Any]) -> None:
        """Run ``action`` (sync or async) once the response has been sent."""
        self._after.append(action)


class Dispatcher:
    def __init__(self, encoding: str = "latin-1"):
        self.encoding = encoding
        self.protocols: dict[int, Protocol] = {}

    def register(self, *protocols: Protocol) -> None:
        for proto in protocols:
            if proto.protocol_id in self.protocols:
                raise ValueError(f"protocol 0x{proto.protocol_id:X} registered twice")
            self.protocols[proto.protocol_id] = proto

    async def dispatch(self, conn: "Connection", message: RMCMessage) -> tuple[bytes, list] | None:
        """Run the handler for ``message``; returns the encoded response and post actions."""
        proto = self.protocols.get(message.protocol)
        info = proto.methods().get(message.method) if proto else None
        label = f"{proto.name if proto else f'0x{message.protocol:X}'}.{info.name if info else message.method}"
        client = conn.client
        if info is None or proto is None:
            log.warning("%s called unimplemented %s (call %d)", client, label, message.call_id)
            return self._error(message, QError.Core_NotImplemented), []
        if info.requires_login and client.user is None:
            log.warning("%s called %s before logging in", client, label)
            return self._error(message, QError.RendezVous_NotAuthenticated), []
        call = Call(conn, message, proto, info, Reader(message.body, encoding=self.encoding))
        log.info("%s -> %s (call %d)", client, label, message.call_id)
        try:
            result = await info.func(proto, call)
        except RMCError as err:
            log.info("%s <- %s error 0x%08X %s", client, label, err.code, err)
            return self._error(message, err.code), call._after
        except Exception:  # noqa: BLE001
            log.exception("%s failed handling %s", client, label)
            return self._error(message, QError.Core_Exception), []
        body = self._encode(result)
        return RMCMessage.response(message.protocol, message.method, message.call_id, body).encode(), call._after

    def _encode(self, result: Any) -> bytes:
        if result is None:
            return b""
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        if isinstance(result, Writer):
            return result.bytes()
        if isinstance(result, Structure):
            return result.encode(self.encoding)
        raise TypeError(f"cannot encode handler result of type {type(result).__name__}")

    @staticmethod
    def _error(message: RMCMessage, code: int) -> bytes:
        return RMCMessage.failure(message.protocol, message.method, message.call_id, code).encode()


def is_coroutine(obj: Any) -> bool:
    return inspect.iscoroutine(obj)
