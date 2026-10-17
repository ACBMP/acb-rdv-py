"""Per-game PRUDP/RMC settings.

Everything a Quazal game changes about the transport lives here, so the
library itself stays game-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .prudp.packet import PacketCodec, StreamType


@dataclass
class GameProfile:
    name: str
    # RC4 key per source stream type (acb-rdv: "CD&ML" for OldRVSec)
    rc4_keys: dict[int, bytes] = field(default_factory=lambda: {StreamType.OLD_RV_SEC: b"CD&ML"})
    # checksum constant per stream type (acb-rdv: 0xB5 for stream 3)
    checksum_settings: dict[int, int] = field(default_factory=lambda: {StreamType.OLD_RV_SEC: 0xB5})
    # stream type used for RMC traffic
    rmc_stream: int = StreamType.OLD_RV_SEC
    # the server's own vport port number (0x31 -> stream 3, port 1)
    server_port_number: int = 1
    # the client vport that carries the player's RMC traffic
    player_port_number: int = 15
    # largest RMC chunk per DATA packet before fragmenting
    fragment_size: int = 963
    # string encoding on the wire
    string_encoding: str = "latin-1"
    # password fed to the Kerberos key derivation
    kerberos_password: str = "UbiDummyPwd"
    # server connection signatures handed out on SYN: random when None, else
    # sequential from this value (acb-rdv used 0x12345678, which is guessable)
    first_connection_id: int | None = None
    compression_level: int = 6

    def codec(self) -> PacketCodec:
        return PacketCodec(
            rc4_keys=self.rc4_keys,
            checksum_settings=self.checksum_settings,
            compression_level=self.compression_level,
        )
