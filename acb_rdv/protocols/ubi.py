"""Ubisoft service protocols used by ACB (acb-rdv ``UbiAccountMgmtService``,
``PrivilegesService``, ``NewsService``, ``UbiNewsService``, ``UplayWinService``,
``TrackingService``, ``TrackingExtService``, ``LocalizationService``,
``PersistentStoreService``, ``GameInfoService`` and ``VirginService``)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from quazal.context import ServerContext, player
from quazal.ddl import U16, U32, U64, U8, Bool, DateTime, ListOf, ResultRange, String, Structure, field, qdatetime_from
from quazal.errors import QError
from quazal.rmc import Call, Protocol, RMCError, method
from quazal.storage import UbiStorage

from ..news import NewsItem, NewsSource
from ..settings import SettingsError, SettingsProvider

log = logging.getLogger("acb.ubi")


class Gathering(Structure):
    id: U32 = 0
    owner_pid: U32 = 0
    host_pid: U32 = 0
    min_participants: U16 = 0
    max_participants: U16 = 8
    participation_policy: U32 = 1
    policy_argument: U32 = 0
    flags: U32 = 1
    state: U32 = 0
    description: String = ""


NEWS_CHANNEL_POLICY = 9


class UbiAccountStatus(Structure):
    basic_status: U32 = 2
    missing_required_information: Bool = False
    recovering_password: Bool = False
    pending_deactivation: Bool = False


class UbiAccount(Structure):
    ubi_account_id: String = ""
    username: String = ""
    password: String = ""
    status: UbiAccountStatus = field(default_factory=UbiAccountStatus)
    email: String = ""
    date_of_birth: DateTime = qdatetime_from(dt.datetime(2001, 9, 27))
    gender: U32 = 0
    country_code: String = ""
    opt_in: Bool = False
    third_party_opt_in: Bool = False
    first_name: String = ""
    last_name: String = ""
    preferred_language: String = "en"
    external_accounts: ListOf(UbiAccountStatus) = field(default_factory=list)


def ubi_account_id(user) -> str:
    if user.ubi_id:
        return user.ubi_id
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"acb-rdv:{user.pid}"))


class UbiAccountManagementProtocol(Protocol):
    protocol_id = 0x1D
    name = "UbiAccountManagement"

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(3, "GetAccount")
    async def get_account(self, call: Call):
        user = call.client.user
        account = UbiAccount(ubi_account_id=ubi_account_id(user), username=user.name, email=user.email,
                             country_code=user.country_code)
        w = self.ctx.writer()
        account.write(w)
        return w.bool(True)


class PrivilegesProtocol(Protocol):
    protocol_id = 0x23

    def __init__(self, ctx: ServerContext, storage: UbiStorage):
        self.ctx = ctx
        self.storage = storage

    @method(1, "GetPrivileges")
    async def get_privileges(self, call: Call):
        locale = call.args.string()
        privileges = {p.id: p for p in await self.storage.privileges(locale)}
        w = self.ctx.writer().u32(len(privileges))
        for pid, priv in privileges.items():
            w.u32(pid).u32(priv.id).string(priv.description)
        return w


class NewsChannel(Gathering):
    name: String = ""
    class_name: String = ""
    locale: String = "en-US"
    subscribable: Bool = False


class NewsHeader(Structure):
    """The client's ``_DDL_NewsHeader``: a UserMessage plus a link."""

    id: U32 = 0
    recipient_id: U32 = 0
    recipient_type: U32 = 0
    parent_id: U32 = 0
    sender_id: U32 = 0
    publication_time: DateTime = 0
    lifetime: U32 = 0
    flags: U32 = 0
    subject: String = ""
    sender_name: String = ""
    link: String = ""


class NewsMessage(NewsHeader):
    body: String = ""


# the channel the game reads its news from; RetrieveUbiNews re-requests the
# channel while this is 0
UBINEWS_CHANNEL_ID = 2
UBINEWS_CHANNEL_TYPE = "UbiNews"
MAX_NEWS_RANGE = 100


def ubinews_channel(owner_pid: int) -> NewsChannel:
    return NewsChannel(UBINEWS_CHANNEL_ID, owner_pid, 0, 0, 64, NEWS_CHANNEL_POLICY, 0, 1, 0, "Description",
                       name="Name", class_name=UBINEWS_CHANNEL_TYPE)


def _header_fields(item: NewsItem, recipient_type: int) -> dict:
    return dict(id=item.id, recipient_id=UBINEWS_CHANNEL_ID, recipient_type=recipient_type,
                publication_time=qdatetime_from(item.published), subject=item.subject,
                sender_name=item.sender, link=item.link)


class NewsProtocol(Protocol):
    """The game's news panel: one UbiNews channel fed by a ``NewsSource``.

    Other channel types the client asks for (``Maintenance`` among them) get
    no channels, as before.
    """

    protocol_id = 0x1F

    def __init__(self, ctx: ServerContext, source: NewsSource):
        self.ctx = ctx
        self.source = source

    @method(2, "GetChannelsByTypes")
    async def get_channels_by_types(self, call: Call):
        types = call.args.string_list()
        rng = ResultRange.read(call.args)
        channels = [ubinews_channel(call.client.pid)] if UBINEWS_CHANNEL_TYPE in types and rng.offset == 0 \
            and rng.size > 0 else []
        log.debug("%s GetChannelsByTypes %s -> %d channel(s)", call.client, types, len(channels))
        return self.ctx.writer().list(channels, lambda w, c: c.write(w))

    @method(8, "GetNewsHeaders")
    async def get_news_headers(self, call: Call):
        recipient_id = call.args.u32()
        recipient_type = call.args.u32()
        rng = ResultRange.read(call.args)
        headers = []
        if recipient_id == UBINEWS_CHANNEL_ID:
            items = (await self.source.items())[rng.offset:rng.offset + min(rng.size, MAX_NEWS_RANGE)]
            headers = [NewsHeader(**_header_fields(i, recipient_type)) for i in items]
        log.info("%s GetNewsHeaders channel %d [%d+%d] -> %d", call.client, recipient_id, rng.offset, rng.size,
                 len(headers))
        return self.ctx.writer().list(headers, lambda w, h: h.write(w))

    @method(9, "GetNewsMessages")
    async def get_news_messages(self, call: Call):
        ids = call.args.u32_list()
        by_id = {i.id: i for i in await self.source.items()}
        messages = [NewsMessage(**_header_fields(by_id[i], 0), body=by_id[i].body) for i in ids if i in by_id]
        log.info("%s GetNewsMessages %s -> %d", call.client, ids, len(messages))
        return self.ctx.writer().list(messages, lambda w, m: m.write(w))

    @method(10, "GetNumberOfNews")
    async def get_number_of_news(self, call: Call):
        recipient_id = call.args.u32()
        call.args.u32()
        count = len(await self.source.items()) if recipient_id == UBINEWS_CHANNEL_ID else 0
        return self.ctx.writer().u32(count)


class UbiNewsProtocol(Protocol):
    protocol_id = 0x21

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(1, "GetNewsChannel")
    async def get_news_channel(self, call: Call):
        w = self.ctx.writer()
        ubinews_channel(call.client.pid).write(w)
        return w


class UplayWinProtocol(Protocol):
    protocol_id = 0x78

    def __init__(self, ctx: ServerContext, storage: UbiStorage, platform: str = "PC"):
        self.ctx = ctx
        self.storage = storage
        self.platform = platform

    @method(5, "GetRewards")
    async def get_rewards(self, call: Call):
        call.args.u32()     # start row
        call.args.u32()     # max rows
        call.args.string()  # sort expression
        call.args.string()  # culture
        rewards = await self.storage.rewards()
        w = self.ctx.writer().u32(len(rewards))
        for r in rewards:
            w.string(r.code).string(r.name).string(r.description).u32(r.value)
            w.string("Unlockable").string("ACB")
            w.u32(1).string(self.platform).bool(True)
        return w


class TrackingInformation(Structure):
    ipn: U32 = 0
    user_id: String = ""
    machine_id: String = ""
    visitor_id: String = ""
    uts_version: String = ""


# values dumped from the original traffic (acb-rdv SendUserInfo response)
TRACKING_INFO = dict(machine_id="e8de277b6f25", visitor_id="95E866FC-1685-41E3-9DE9-D78227FD61F9",
                     uts_version="30053")
TRACKING_ID = 0x5A7A0090


class TrackingProtocol(Protocol):
    protocol_id = 0x24

    def __init__(self, ctx: ServerContext, storage: UbiStorage):
        self.ctx = ctx
        self.storage = storage

    @method(1, "SendTag")
    async def send_tag(self, call: Call):
        tracking_id = call.args.u32()
        tag = call.args.string()
        attributes = call.args.string()
        delta = call.args.u32()
        call.after(lambda: self.storage.save_telemetry(tracking_id, tag, attributes, delta))
        return None

    @method(3, "SendUserInfo")
    async def send_user_info(self, call: Call):
        TrackingInformation.read(call.args)
        call.args.u32()
        tracking = call.client.tracking_user
        info = TrackingInformation(ipn=tracking.pid if tracking else 105, **TRACKING_INFO)
        w = self.ctx.writer()
        info.write(w)
        return w.u32(TRACKING_ID)

    @method(4, "GetConfiguration")
    async def get_configuration(self, call: Call):
        return self.ctx.writer().string_list(await self.storage.telemetry_tags())


class TrackingExtProtocol(Protocol):
    protocol_id = 0x6F

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(1, "IsTrackingSessionActive")
    async def is_tracking_session_active(self, call: Call):
        call.args.string()
        return self.ctx.writer().bool(True)


class LocalizationProtocol(Protocol):
    protocol_id = 0x27

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(2, "SetLocaleCode")
    async def set_locale_code(self, call: Call):
        player(call.client).locale = call.args.string()
        return None


class PersistentStoreProtocol(Protocol):
    """Serves the game settings (CXB) for every item request, like acb-rdv."""

    protocol_id = 0x18

    def __init__(self, ctx: ServerContext, settings: SettingsProvider):
        self.ctx = ctx
        self.settings = settings

    @method(4, "GetItem")
    async def get_item(self, call: Call):
        call.args.u32()     # group
        call.args.string()  # tag
        try:
            content = (await self.settings.current()).data
        except SettingsError as exc:
            # acb-rdv answered with an empty file when it had none
            log.error("no game settings to serve: %s", exc)
            content = b""
        return self.ctx.writer().buffer(content).bool(True)


# QFile size dumped from the original traffic
GAME_STORAGE_FILE_SIZE = 58484


class GameInfoProtocol(Protocol):
    protocol_id = 0x71

    def __init__(self, ctx: ServerContext, file_size: int = GAME_STORAGE_FILE_SIZE):
        self.ctx = ctx
        self.file_size = file_size

    @method(5, "GetFileListOnGameStorage")
    async def get_file_list_on_game_storage(self, call: Call):
        call.args.u32()
        call.args.u32()
        name = call.args.string()
        return self.ctx.writer().u32(1).string(name).u32(self.file_size)


class VirginInfo(Structure):
    pid: U32 = 0
    session_id: U64 = 0
    begin: DateTime = 0
    end: DateTime = 0
    host: Bool = False
    has_quit: Bool = False
    unique_player_max: U32 = 0
    game_mode: U8 = 0
    platform: U8 = 0
    score: U32 = 0
    kills: U32 = 0


class VirginProtocol(Protocol):
    """End-of-match summary; logged only (acb-rdv discarded it as well)."""

    protocol_id = 0x76

    def __init__(self, ctx: ServerContext):
        self.ctx = ctx

    @method(1, "SendVirginInfo")
    async def send_virgin_info(self, call: Call):
        try:
            info = VirginInfo.read(call.args)
            log.info("%s match summary: %s", call.client, info)
        except Exception:  # noqa: BLE001
            raise RMCError(QError.Core_InvalidArgument) from None
        return None
