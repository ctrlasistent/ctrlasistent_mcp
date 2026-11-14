# tool_matrix.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import sys
import requests
import aiohttp
from aiohttp import abc

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class SyncResolver(abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, family, socket.SOCK_STREAM
        )
        return [
            {
                "hostname": host,
                "host": info[4][0],
                "port": info[4][1],
                "family": info[0],
                "proto": info[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for info in infos
        ]

    async def close(self):
        pass


_original_tcp_connector_init = aiohttp.TCPConnector.__init__


def _patched_tcp_connector_init(self, *args, **kwargs):
    kwargs.setdefault("family", socket.AF_INET)  # Forsiraj IPv4
    kwargs.setdefault("resolver", SyncResolver())  # Koristi naš resolver
    _original_tcp_connector_init(self, *args, **kwargs)


aiohttp.TCPConnector.__init__ = _patched_tcp_connector_init

from pathlib import Path
from typing import Any, Dict, List
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from nacl.signing import SigningKey

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginResponse,
    RoomSendResponse,
    RoomCreateResponse,
    SyncResponse,
    RoomMessageText,
    MegolmEvent,
)

from core.CtrlAsistent_tools import Tool, register_tool

logger = logging.getLogger(__name__)

# Smanji spam
logging.getLogger("nio.events.misc").setLevel(logging.ERROR)
logging.getLogger("nio.rooms").setLevel(logging.WARNING)

E2EE_AVAILABLE = False
try:
    import olm

    E2EE_AVAILABLE = True
except ImportError:
    logger.warning("⚠️ E2EE nije dostupan")


# ============================================================
# CROSS-SIGNING POMOĆNE FUNKCIJE
# ============================================================

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(s: str) -> bytes:
    num = 0
    for char in s:
        num *= 58
        if char not in _B58_ALPHABET:
            raise ValueError(f"Nevaljan base58 znak: {char!r}")
        num += _B58_ALPHABET.index(char)
    combined = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    n_leading = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_leading + combined


def _decode_recovery_key(recovery_key: str) -> bytes:
    cleaned = recovery_key.replace(" ", "").strip()
    raw = _base58_decode(cleaned)
    if len(raw) != 35:
        raise ValueError(f"Neispravna duljina recovery key-a: {len(raw)}")
    prefix, key_bytes = raw[:2], raw[2:34]
    parity = 0
    for b in raw:
        parity ^= b
    if parity != 0:
        raise ValueError("Parity check nije prošao - pogrešan recovery key")
    if prefix != b"\x8b\x01":
        logger.warning(f"Neočekivan prefix: {prefix!r}")
    return key_bytes


def _derive_ssss_keys(secret_storage_key: bytes, info: str) -> tuple:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=b"\x00" * 32,
        info=info.encode("utf-8"),
    )
    expanded = hkdf.derive(secret_storage_key)
    return expanded[:32], expanded[32:]


def _verify_ssss_key(secret_storage_key: bytes, key_account_data: dict) -> bool:
    aes_key, hmac_key = _derive_ssss_keys(secret_storage_key, "")
    iv = bytearray(base64.b64decode(key_account_data["iv"]))
    iv[8] &= 0x7F
    cipher = Cipher(algorithms.AES(aes_key), modes.CTR(bytes(iv)))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(b"\x00" * 32) + encryptor.finalize()
    mac = base64.b64encode(
        hmac.new(hmac_key, ciphertext, hashlib.sha256).digest()
    ).decode("utf-8")
    return mac == key_account_data["mac"]


def _decrypt_ssss_secret(
    secret_storage_key: bytes, secret_name: str, encrypted: dict
) -> bytes:
    aes_key, hmac_key = _derive_ssss_keys(secret_storage_key, secret_name)
    ciphertext = base64.b64decode(encrypted["ciphertext"])
    expected_mac = encrypted["mac"]
    actual_mac = base64.b64encode(
        hmac.new(hmac_key, ciphertext, hashlib.sha256).digest()
    ).decode("utf-8")
    if not hmac.compare_digest(actual_mac, expected_mac):
        raise ValueError(f"MAC provjera nije prošla za {secret_name}")
    iv = base64.b64decode(encrypted["iv"])
    cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _unpadded_b64decode(s: str) -> bytes:
    s = s.strip()
    padding = (-len(s)) % 4
    return base64.b64decode(s + "=" * padding)


def _canonical_json_bytes(obj: dict) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _cross_sign_device(
    user_id: str, device_id: str, homeserver: str, access_token: str, recovery_key: str
) -> bool:
    """Cross-signira device s Recovery Key-om."""
    try:
        logger.info("Započinjem cross-signing verifikaciju...")

        secret_storage_key = _decode_recovery_key(recovery_key)

        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {access_token}"})

        default_key_data = session.get(
            f"{homeserver}/_matrix/client/v3/user/{user_id}/account_data/m.secret_storage.default_key"
        ).json()
        key_id = default_key_data["key"]
        logger.info(f"Secret storage key ID: {key_id}")

        key_account_data = session.get(
            f"{homeserver}/_matrix/client/v3/user/{user_id}/account_data/m.secret_storage.key.{key_id}"
        ).json()

        if not _verify_ssss_key(secret_storage_key, key_account_data):
            logger.error("Recovery key ne odgovara računu")
            return False

        ss_secret_data = session.get(
            f"{homeserver}/_matrix/client/v3/user/{user_id}/account_data/m.cross_signing.self_signing"
        ).json()
        encrypted = ss_secret_data["encrypted"][key_id]

        plaintext = _decrypt_ssss_secret(
            secret_storage_key, "m.cross_signing.self_signing", encrypted
        )
        seed = _unpadded_b64decode(plaintext.decode("utf-8"))
        signing_key = SigningKey(seed)
        self_signing_pub_b64 = (
            base64.b64encode(bytes(signing_key.verify_key)).decode("utf-8").rstrip("=")
        )
        self_signing_key_id = f"ed25519:{self_signing_pub_b64}"

        query_result = session.post(
            f"{homeserver}/_matrix/client/v3/keys/query",
            json={"device_keys": {user_id: [device_id]}},
        ).json()

        device_key_obj = query_result["device_keys"][user_id][device_id]

        signable = {
            k: v
            for k, v in device_key_obj.items()
            if k not in ("signatures", "unsigned")
        }

        signature = signing_key.sign(_canonical_json_bytes(signable)).signature
        signature_b64 = base64.b64encode(signature).decode("utf-8").rstrip("=")

        signed_obj = dict(signable)
        signed_obj["signatures"] = dict(device_key_obj.get("signatures", {}))
        signed_obj["signatures"].setdefault(user_id, {})
        signed_obj["signatures"][user_id][self_signing_key_id] = signature_b64

        upload_result = session.post(
            f"{homeserver}/_matrix/client/v3/keys/signatures/upload",
            json={user_id: {device_id: signed_obj}},
        ).json()

        if upload_result.get("failures"):
            logger.error(f"Upload nije uspio: {upload_result['failures']}")
            return False

        logger.info("✅ Cross-signing verifikacija uspješna!")
        return True

    except Exception as e:
        logger.error(f"Greška pri cross-signingu: {e}")
        import traceback

        traceback.print_exc()
        return False
    finally:
        if "session" in locals():
            session.close()


@register_tool
class MatrixChatTool(Tool):
    """Matrix chat klijent kao trajni uredjaj"""

    STORE_PATH = Path.home() / ".matrix_nio_store"
    CREDENTIALS_FILE = STORE_PATH / "credentials.json"

    def __init__(self):
        self.STORE_PATH.mkdir(parents=True, exist_ok=True)

        self.homeserver = os.environ.get("MATRIX_HOMESERVER", "https://matrix.org")
        self.user_id = os.environ.get("MATRIX_USER_ID", "")
        self.password = os.environ.get("MATRIX_PASSWORD", "")
        self.recovery_key = os.environ.get("MATRIX_RECOVERY_KEY", "")

        self.credentials = self._load_credentials()

        # Generiraj device_id ako ne postoji
        if not self.credentials.get("device_id"):
            self.credentials["device_id"] = secrets.token_hex(5).upper()
            logger.info(f"Generiran novi device_id: {self.credentials['device_id']}")

        if not self.user_id and self.credentials.get("user_id"):
            self.user_id = self.credentials["user_id"]

    def _load_credentials(self) -> Dict:
        if self.CREDENTIALS_FILE.exists():
            try:
                with open(self.CREDENTIALS_FILE) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Greška pri učitavanju podataka: {e}")
        return {}

    def _save_credentials(self):
        """Spremi trenutne pristupne podatke na disk."""
        try:
            with open(self.CREDENTIALS_FILE, "w") as f:
                json.dump(self.credentials, f, indent=2)
            logger.debug(
                f"Podaci spremljeni za device {self.credentials.get('device_id')}"
            )
        except Exception as e:
            logger.error(f"Greška pri spremanju pristupnih podataka: {e}")

    @property
    def name(self) -> str:
        return "matrix_chat"

    @property
    def description(self) -> str:
        e2ee = "ENKRIPTIRANO (E2EE)" if E2EE_AVAILABLE else "PLAIN TEXT"
        return (
            f"Matrix chat. [{e2ee}]\n\n"
            "PRIMJERI:\n"
            '{"tool": "matrix_chat", "arguments": {"action": "send_message", "recipient": "@user:matrix.org", "message": "Bok"}}\n'
            '{"tool": "matrix_chat", "arguments": {"action": "check_messages"}}'
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "action": {"type": "string", "enum": ["send_message", "check_messages"]},
            "recipient": {"type": "string"},
            "message": {"type": "string"},
            "wait_seconds": {"type": "integer", "default": 10},
        }

    def execute(
        self,
        action: str,
        recipient: str = "",
        message: str = "",
        wait_seconds: int = 10,
    ) -> str:
        if not self.user_id:
            return "❌ MATRIX_USER_ID nije postavljen"

        try:
            if action in ("send_message", "send_text"):
                return asyncio.run(self._async_send(recipient, message, wait_seconds))
            elif action == "check_messages":
                return asyncio.run(self._async_check())
            return f"❌ Nepoznata akcija: {action}"
        except Exception as e:
            logger.exception("Greška")
            return f"❌ {e}"

    def _create_client(self) -> AsyncClient:
        config = AsyncClientConfig(
            max_limit_exceeded=0,
            max_timeouts=0,
            encryption_enabled=E2EE_AVAILABLE,
            store_sync_tokens=True,
        )

        # Uvijek koristi device_id
        device_id = self.credentials.get("device_id")
        if not device_id:
            raise RuntimeError("device_id nije postavljen u credentials")

        return AsyncClient(
            self.homeserver,
            self.user_id,
            device_id=device_id,  # ← Uvijek isti device_id
            store_path=str(self.STORE_PATH),
            config=config,
        )

    async def _login(self, client: AsyncClient) -> bool:
        is_first_login = not self.credentials.get("access_token")

        # 1. Pokušaj s pohranjenim podacima
        if self.credentials.get("access_token"):
            client.restore_login(
                user_id=self.credentials["user_id"],
                device_id=self.credentials["device_id"],
                access_token=self.credentials["access_token"],
            )
            if E2EE_AVAILABLE:
                client.load_store()
            try:
                whoami = await client.whoami()
                if hasattr(whoami, "user_id") and whoami.user_id:
                    logger.info(f"✅ Koristim pohranjeni access token")
                    return True
            except Exception as e:
                logger.warning(f"Access token nije validan: {e}")
                # Nastavi s password login

        # 2. Password login
        if not self.password:
            logger.error("MATRIX_PASSWORD nije postavljen")
            return False

        response = await client.login(self.password, device_name="CtrlAsistent")
        if not isinstance(response, LoginResponse):
            logger.error(f"Login neuspješan: {response}")
            return False

        # Provjeri je li server vratio isti device_id
        if response.device_id != self.credentials["device_id"]:
            logger.warning(
                f"⚠️ Server vratio drugačiji device_id! "
                f"Očekivano: {self.credentials['device_id']}, "
                f"Dobiveno: {response.device_id}"
            )
            # Ažuriraj pristupne podatke s novim device_id-om
            self.credentials["device_id"] = response.device_id

        # Spremi pristupne podatke
        self.credentials["user_id"] = response.user_id
        self.credentials["access_token"] = response.access_token
        self._save_credentials()

        if E2EE_AVAILABLE:
            client.load_store()
            if client.should_upload_keys:
                await client.keys_upload()

        # 3. Automatska cross-signing verifikacija pri prvom loginu
        if is_first_login and self.recovery_key:
            logger.info("Prvi login - automatska cross-signing verifikacija...")

            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None,
                _cross_sign_device,
                response.user_id,
                response.device_id,
                self.homeserver,
                response.access_token,
                self.recovery_key,
            )

            if success:
                logger.info("✅ Uređaj automatski verificiran!")
            else:
                logger.warning("⚠️ Automatska verifikacija nije uspjela")

        return True

    async def _get_or_create_room(self, client: AsyncClient, recipient: str) -> str:
        await client.sync(timeout=10000)
        for room_id, room in client.rooms.items():
            if recipient in room.users:
                return room_id

        response = await client.room_create(
            invite=[recipient],
            is_direct=True,
            name="AI Chat",
        )

        if not isinstance(response, RoomCreateResponse):
            raise RuntimeError(f"Neuspješno kreiranje sobe: {response}")

        if E2EE_AVAILABLE:
            try:
                await client.room_put_state(
                    room_id=response.room_id,
                    event_type="m.room.encryption",
                    content={"algorithm": "m.megolm.v1.aes-sha2"},
                )
            except Exception:
                pass

        return response.room_id

    async def _async_send(self, recipient: str, message: str, wait_seconds: int) -> str:
        client = self._create_client()
        try:
            if not await self._login(client):
                return "❌ Neuspješna prijava"

            await client.sync(timeout=30000, full_state=True)
            room_id = await self._get_or_create_room(client, recipient)

            if room_id not in client.rooms:
                await client.join(room_id)

            response = await client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": message},
                ignore_unverified_devices=True,
            )

            if not isinstance(response, RoomSendResponse):
                return f"❌ Neuspješno slanje: {response}"

            received = await self._collect_messages(client, wait_seconds)
            return self._format_output(sent=True, received=received)
        except Exception as e:
            logger.exception("Greška pri slanju")
            return f"❌ {e}"
        finally:
            await client.close()

    async def _async_check(self) -> str:
        client = self._create_client()
        try:
            if not await self._login(client):
                return "❌ Neuspješna prijava"

            await client.sync(timeout=20000, full_state=True)

            received = []
            for room_id in client.rooms:
                try:
                    resp = await client.room_messages(room_id, limit=10, direction="b")
                    if hasattr(resp, "chunk"):
                        for event in resp.chunk:
                            if (
                                isinstance(event, RoomMessageText)
                                and event.sender != self.user_id
                            ):
                                received.append(
                                    {
                                        "from": event.sender,
                                        "body": event.body,
                                        "encrypted": True,
                                    }
                                )
                except Exception:
                    pass

            new_messages = await self._collect_messages(client, 3)
            existing = {(m["from"], m["body"]) for m in received}
            for msg in new_messages:
                if (msg["from"], msg["body"]) not in existing:
                    received.append(msg)

            return self._format_output(sent=False, received=received)
        except Exception as e:
            logger.exception("Greška pri provjeri")
            return f"❌ {e}"
        finally:
            await client.close()

    async def _collect_messages(
        self, client: AsyncClient, wait_seconds: int
    ) -> List[Dict]:
        received = []
        seen = set()
        wait_seconds = min(wait_seconds, 60)

        for _ in range(wait_seconds):
            try:
                sync_resp = await client.sync(timeout=1000, full_state=False)
                if isinstance(sync_resp, SyncResponse):
                    for room_id, room in sync_resp.rooms.join.items():
                        if not (
                            hasattr(room, "timeline")
                            and hasattr(room.timeline, "events")
                        ):
                            continue
                        for event in room.timeline.events:
                            if event.event_id in seen:
                                continue
                            seen.add(event.event_id)
                            if isinstance(event, RoomMessageText):
                                if event.sender != self.user_id:
                                    received.append(
                                        {
                                            "from": event.sender,
                                            "body": event.body,
                                            "encrypted": getattr(
                                                event, "decrypted", False
                                            ),
                                        }
                                    )
            except Exception:
                pass
            await asyncio.sleep(1)

        return received

    def _format_output(self, sent: bool, received: List[Dict]) -> str:
        lines = []
        if sent:
            lines.append("✅ Poruka poslana")
        if received:
            lines.append(f"\n📥 PRIMLJENE PORUKE ({len(received)}):")
            lines.append("-" * 50)
            for i, msg in enumerate(received, 1):
                icon = "🔒" if msg.get("encrypted") else "📨"
                lines.append(f"{i}. {icon} Od: {msg['from']}")
                lines.append(f"   {msg['body']}")
        else:
            lines.append("📭 Nema novih poruka.")
        return "\n".join(lines)
