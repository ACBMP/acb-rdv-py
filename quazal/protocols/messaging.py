"""Messaging (0x17) and MessageDelivery (0x1B), acb-rdv ``MessagingService`` and
``MessageDeliveryService``.

Players send ``TextMessage`` objects (wrapped in an ``AnyDataHolder``) through
MessageDelivery; they are stored until the recipient fetches them through
Messaging. A message with the subject ``Notification`` and body ``Check for
new Messages`` is not stored but forwarded live to the recipient.
"""

from __future__ import annotations

import logging

from ..context import ServerContext
from ..ddl import AnyDataHolder, DateTime, Reader, ResultRange, String, Structure, U32, qdatetime_now
from ..rmc import Call, Protocol, method
from ..storage import StoredMessage

log = logging.getLogger("quazal.messaging")

TEXT_MESSAGE = "TextMessage"
NOTIFICATION_SUBJECT = "Notification"
CHECK_MESSAGES_BODY = "Check for new Messages"


class UserMessage(Structure):
    id: U32 = 0
    recipient_id: U32 = 0
    recipient_type: U32 = 0
    parent_id: U32 = 0
    sender_id: U32 = 0
    reception_time: DateTime = 0
    lifetime: U32 = 0
    flags: U32 = 0
    subject: String = ""
    sender_name: String = ""


class TextMessage(UserMessage):
    body: String = ""

    @classmethod
    def read_holder(cls, r: Reader) -> "TextMessage":
        holder = AnyDataHolder.read(r)
        return cls.read(Reader(holder.payload, encoding=r.encoding))

    def holder(self, encoding: str) -> AnyDataHolder:
        return AnyDataHolder(TEXT_MESSAGE, self.encode(encoding))

    @classmethod
    def from_stored(cls, m: StoredMessage) -> "TextMessage":
        return cls(m.id, m.recipient_pid, m.recipient_type, m.parent_id, m.sender_pid, m.reception_time,
                   m.lifetime, m.flags, m.subject, m.sender_name, m.body)

    def to_stored(self) -> StoredMessage:
        return StoredMessage(self.id, self.recipient_id, self.recipient_type, self.parent_id, self.sender_id,
                             self.reception_time, self.lifetime, self.flags, self.subject, self.sender_name,
                             self.body)


def header_of(m: StoredMessage) -> UserMessage:
    return UserMessage(m.id, m.recipient_pid, m.recipient_type, m.parent_id, m.sender_pid, m.reception_time,
                       m.lifetime, m.flags, m.subject, m.sender_name)


class MessagingProtocol(Protocol):
    protocol_id = 0x17

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    def _recipient(self, call: Call) -> int:
        pid = call.args.u32()
        call.args.u32()  # gathering id
        if pid != call.client.pid:
            log.warning("%s asked for messages of pid %d; using its own", call.client, pid)
        return call.client.pid

    @method(3, "GetMessagesHeaders")
    async def get_messages_headers(self, call: Call):
        pid = self._recipient(call)
        ResultRange.read(call.args)
        pending = await self.ctx.storage.pending_messages(pid)
        return self.ctx.writer().list([header_of(m) for m in pending], lambda w, h: h.write(w))

    @method(5, "RetrieveMessages")
    async def retrieve_messages(self, call: Call):
        pid = self._recipient(call)
        ids = call.args.u32_list()
        call.args.bool()  # leave on server
        messages = [m for m in await self.ctx.storage.messages_by_ids(ids) if m.recipient_pid == pid]
        enc = self.ctx.encoding
        w = self.ctx.writer().list(messages, lambda w, m: TextMessage.from_stored(m).holder(enc).write(w))
        delivered = [m.id for m in messages]
        call.after(lambda: self.ctx.storage.mark_delivered(delivered))
        return w


class MessageDeliveryProtocol(Protocol):
    protocol_id = 0x1B

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(1, "DeliverMessage")
    async def deliver_message(self, call: Call):
        msg = TextMessage.read_holder(call.args)
        client = call.client
        msg.sender_id = client.pid
        msg.sender_name = client.name
        if msg.subject != NOTIFICATION_SUBJECT:
            msg.reception_time = qdatetime_now()
            stored = msg.to_stored()
            call.after(lambda: self.ctx.storage.add_message(stored))
        elif msg.body == CHECK_MESSAGES_BODY:
            def forward() -> None:
                target = self.ctx.online(msg.recipient_id)
                if target is not None:
                    w = self.ctx.writer()
                    msg.holder(self.ctx.encoding).write(w)
                    self.ctx.push(target, self.protocol_id, 1, w.bytes())

            call.after(forward)
        return None
