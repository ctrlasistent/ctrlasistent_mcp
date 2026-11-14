# tool_xmpp.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import asyncio
import os
import sys
import ssl
import json
import logging
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Set, List
from slixmpp import ClientXMPP
from slixmpp.jid import JID
from slixmpp.plugins import register_plugin
from omemo.storage import Just, Maybe, Nothing, Storage
from omemo.types import DeviceInformation, JSONType
from slixmpp_omemo import TrustLevel, XEP_0384

from core.CtrlAsistent_tools import Tool, register_tool

logger = logging.getLogger(__name__)


class OMEMOStorage(Storage):
    def __init__(self, json_file_path: Path):
        super().__init__()
        self.__json_file_path = json_file_path
        self.__data: Dict[str, JSONType] = {}
        if self.__json_file_path.exists():
            try:
                with open(self.__json_file_path, "r", encoding="utf-8") as f:
                    self.__data = json.load(f)
            except Exception:
                self.__data = {}

    def _save_to_disk(self):
        try:
            self.__json_file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.__json_file_path, "w", encoding="utf-8") as f:
                json.dump(self.__data, f)
        except Exception as e:
            logger.error(f"Spremanje nije uspjelo: {e}")

    async def _load(self, key: str):
        if key in self.__data:
            return Just(self.__data[key])
        return Nothing()

    async def _store(self, key: str, value):
        self.__data[key] = value
        self._save_to_disk()

    async def _delete(self, key: str):
        self.__data.pop(key, None)
        self._save_to_disk()


class XEP_0384Impl(XEP_0384):
    default_config = {
        "fallback_message": "This message is OMEMO encrypted.",
        "json_file_path": None,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__storage: Optional[Storage] = None

    def plugin_init(self):
        json_path = self.config.get("json_file_path")
        if not json_path:
            raise ValueError("json_file_path mora biti naveden")
        self.__storage = OMEMOStorage(Path(json_path))
        super().plugin_init()

    @property
    def storage(self) -> Storage:
        if self.__storage is None:
            raise RuntimeError("OMEMO pohrana nije inicijalizirana")
        return self.__storage

    @property
    def _btbv_enabled(self) -> bool:
        return True

    async def _devices_blindly_trusted(self, blindly_trusted, identifier):
        logger.info(f"[OMEMO] Automatski pouzdani uređaji za {identifier}")

    async def _prompt_manual_trust(self, manually_trusted, identifier):
        sm = await self.get_session_manager()
        for device in manually_trusted:
            await sm.set_trust(
                device.bare_jid, device.identity_key, TrustLevel.TRUSTED.value
            )


register_plugin(XEP_0384Impl)


@register_tool
class XMPPOMEMOTool(Tool):
    """Šalje i prima OMEMO šifrirane poruke putem XMPP-a."""

    def __init__(self):
        self.address = os.environ.get("XMPP_ADDRESS", "")
        self.password = os.environ.get("XMPP_PASSWORD", "")
        if not self.address or not self.password:
            logger.warning("XMPP_ADDRESS ili XMPP_PASSWORD nisu postavljeni.")

    @property
    def name(self) -> str:
        return "xmpp_omemo"

    @property
    def description(self) -> str:
        return (
            "Šalje i prima OMEMO šifrirane poruke putem XMPP-a. Ako korisnik pita da provjeriš nove poruke, ti provjeri i ispiše sve poruke.\n\n"
            "PRIMJERI KORIŠTENJA:\n\n"
            "1. Slanje poruke:\n"
            '{"tool": "xmpp_omemo", "arguments": {"action": "send_message", "recipient": "korisnik@example.com", "message": "Bok, kako si?"}}\n\n'
            "2. Provjera novih poruka:\n"
            '{"tool": "xmpp_omemo", "arguments": {"action": "check_messages"}}\n\n'
            "3. Slanje i čekanje odgovora (duže):\n"
            '{"tool": "xmpp_omemo", "arguments": {"action": "send_message", "recipient": "korisnik@example.com", "message": "Pitanje?", "wait_seconds": 20}}\n\n'
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "action": {
                "type": "string",
                "enum": ["send_message", "send_text", "check_messages"],
                "description": "Akcija: send_message/send_text za slanje poruke, check_messages za provjeru novih poruka",
            },
            "recipient": {
                "type": "string",
                "description": "JID primatelja (npr. 'korisnik@example.com'). Obavezno za slanje.",
            },
            "message": {
                "type": "string",
                "description": "Tekst poruke koji želite poslati. Obavezno za slanje.",
            },
            "wait_seconds": {
                "type": "integer",
                "description": "Koliko sekundi čekati na odgovore nakon slanja (default 10, max 60)",
                "default": 10,
            },
        }

    def execute(
        self,
        action: str,
        recipient: str = "",
        message: str = "",
        wait_seconds: int = 10,
    ) -> str:
        if action in ("send_text", "send_message"):
            if not recipient or not message:
                return "GREŠKA: recipient i message su obavezni za slanje poruke."
            return self._run_omemo(recipient, message, wait_seconds)
        elif action == "check_messages":
            return self._run_check_messages(wait_seconds)
        return f"GREŠKA: Nepoznata akcija '{action}'. Koristite send_message ili check_messages."

    def _run_omemo(self, recipient: str, message: str, wait_seconds: int) -> str:
        if not self.address or not self.password:
            return "GREŠKA: XMPP vjerodajnice nisu postavljene."
        try:
            return asyncio.run(self._async_send_omemo(recipient, message, wait_seconds))
        except Exception as e:
            logger.exception("Greška pri slanju")
            return f"GREŠKA: {e}"

    def _run_check_messages(self, wait_seconds: int) -> str:
        if not self.address or not self.password:
            return "GREŠKA: XMPP vjerodajnice nisu postavljene."
        try:
            return asyncio.run(self._async_check_messages(wait_seconds))
        except Exception as e:
            logger.exception("Greška pri provjeri poruka")
            return f"GREŠKA: {e}"

    def _format_response(
        self, sent: bool, sent_encrypted: bool, received: List[Dict], error: str = None
    ) -> str:
        """Formatira odgovor za AI model"""
        lines = []

        if error:
            lines.append(f"❌ GREŠKA: {error}")
            lines.append("")

        if sent:
            enc_status = (
                "ENKRIPTIRANO (OMEMO)"
                if sent_encrypted
                else "NE-ENKRIPTIRANO (plain fallback)"
            )
            lines.append(f"✅ Poruka uspješno poslana [{enc_status}]")
        elif not error:
            lines.append("ℹ️ Provjera poruka (bez slanja)")

        lines.append("")

        if received:
            lines.append(f"📥 PRIMLJENE PORUKE ({len(received)}):")
            lines.append("-" * 50)
            for i, msg in enumerate(received, 1):
                enc_icon = "🔒" if msg.get("encrypted") else "📨"
                lines.append(f"{i}. {enc_icon} Od: {msg['from']}")
                lines.append(f"   Poruka: {msg['body']}")
                lines.append("")
        else:
            lines.append("📭 Nema novih primljenih poruka.")

        return "\n".join(lines)

    async def _async_send_omemo(
        self, recipient: str, message: str, wait_seconds: int
    ) -> str:
        """Šalje poruku i čeka odgovore"""

        class OMEMOSender(ClientXMPP):
            def __init__(self, jid, pw):
                super().__init__(jid, pw, sasl_mech="SCRAM-SHA-1")

                # DODANO: xep_0184 (Delivery Receipts) i xep_0333 (Chat Markers)
                for plugin in [
                    "xep_0030",
                    "xep_0045",
                    "xep_0060",
                    "xep_0163",
                    "xep_0184",
                    "xep_0199",
                    "xep_0280",
                    "xep_0333",
                    "xep_0334",
                    "xep_0380",
                ]:
                    self.register_plugin(plugin)

                safe_jid = jid.split("@")[0].replace(".", "_")
                data_dir = Path.home() / ".slixmpp_omemo"
                data_dir.mkdir(parents=True, exist_ok=True)

                self.register_plugin(
                    "xep_0384",
                    {"json_file_path": str(data_dir / f"{safe_jid}.json")},
                    module=sys.modules[__name__],
                )

                self.add_event_handler("session_start", self.session_start)
                self.add_event_handler("failed_auth", self.failed_auth)
                self.add_event_handler("message", self.message_handler)

                self.recipient = recipient
                self.msg = message
                self.sent_event = asyncio.Event()
                self.send_error = None
                self.sent_encrypted = False
                self.received_messages: List[Dict] = []

            async def failed_auth(self, event):
                logger.error(f"AUTENTIFIKACIJA NEUSPJEŠNA: {event}")
                self.send_error = f"AUTENTIFIKACIJA NEUSPJEŠNA: {event}"
                self.disconnect()

            async def session_start(self, event):
                try:
                    self.send_presence()
                    await self.get_roster()

                    init_event = asyncio.Event()
                    self.add_event_handler(
                        "omemo_initialized", lambda _: init_event.set()
                    )
                    try:
                        await asyncio.wait_for(init_event.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Timeout na OMEMO inicijalizaciju, nastavljam..."
                        )

                    recipient_jid = JID(self.recipient)
                    stanza = self.make_message(mto=recipient_jid, mtype="chat")
                    stanza["body"] = self.msg

                    encrypt_for: Set[JID] = {recipient_jid}

                    omemo_success = False
                    try:
                        enc_msg, enc_errors = await self["xep_0384"].encrypt_message(
                            stanza, encrypt_for
                        )
                        if enc_msg is not None:
                            enc_msg.send()
                            logger.info("✅ OMEMO poruka uspješno poslana!")
                            omemo_success = True
                            self.sent_encrypted = True
                    except Exception as e:
                        logger.warning(f"OMEMO enkripcija neuspješna: {e}")

                    if not omemo_success:
                        logger.info("Šaljem plain poruku kao fallback...")
                        plain_msg = self.make_message(mto=recipient_jid, mtype="chat")
                        plain_msg["body"] = self.msg
                        plain_msg.send()
                        logger.info("✅ Plain poruka uspješno poslana!")
                        self.sent_encrypted = False

                    await asyncio.sleep(1.5)
                    self.sent_event.set()

                except Exception as e:
                    self.send_error = str(e)
                    logger.error(self.send_error, exc_info=True)
                    self.disconnect()

            async def send_receipts(self, msg):
                """Šalje potvrde primitka i čitanja (XEP-0184 i XEP-0333)"""
                try:
                    from_jid = msg["from"]
                    msg_id = msg.get("id")

                    if not msg_id:
                        return

                    # 1. Delivery Receipt (XEP-0184) - potvrda da je poruka dostavljena
                    if msg.get("request_receipt"):
                        receipt = self.make_message(mto=from_jid, mtype="chat")
                        receipt["receipt"] = msg_id
                        receipt.send()
                        logger.info(f"✅ Delivery receipt poslan za poruku {msg_id}")

                    # 2. Chat Marker - Read Receipt (XEP-0333) - potvrda da je poruka pročitana
                    displayed_marker = self.make_message(mto=from_jid, mtype="chat")
                    displayed_marker["chat_marker"] = {
                        "type": "displayed",
                        "id": msg_id,
                    }
                    displayed_marker.send()
                    logger.info(
                        f"✅ Read receipt (displayed marker) poslan za poruku {msg_id}"
                    )

                except Exception as e:
                    logger.warning(f"Greška pri slanju potvrda: {e}")

            async def message_handler(self, msg):
                """Handler za dolazne poruke"""
                if msg["type"] in ("chat", "normal"):
                    from_jid = JID(msg["from"]).bare

                    # KLJUČNO: Pošalji potvrde primitka i čitanja
                    await self.send_receipts(msg)

                    namespaces = self["xep_0384"].is_encrypted(msg)

                    if namespaces:
                        try:
                            decrypted_msg, device_info = await self[
                                "xep_0384"
                            ].decrypt_message(msg)
                            body = decrypted_msg.get("body", "(prazno)")
                            logger.info(f"🔒 OMEMO poruka od {from_jid}: {body}")
                            self.received_messages.append(
                                {"from": str(from_jid), "body": body, "encrypted": True}
                            )
                        except Exception as e:
                            logger.warning(f"Neuspjela dekripcija OMEMO poruke: {e}")
                    else:
                        body = msg["body"]
                        if body:
                            logger.info(f"📨 Poruka od {from_jid}: {body}")
                            self.received_messages.append(
                                {
                                    "from": str(from_jid),
                                    "body": body,
                                    "encrypted": False,
                                }
                            )

        xmpp = OMEMOSender(self.address, self.password)

        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        xmpp.ssl_context = context

        try:
            await xmpp.connect()

            try:
                await asyncio.wait_for(xmpp.sent_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.error("Timeout pri slanju poruke")
                xmpp.disconnect()
                return self._format_response(
                    sent=False,
                    sent_encrypted=False,
                    received=[],
                    error="Timeout pri slanju poruke",
                )

            wait_seconds = min(wait_seconds, 60)
            logger.info(f"Čekam odgovore ({wait_seconds} sekundi)...")

            for i in range(wait_seconds):
                if not xmpp.is_connected():
                    logger.warning("Veza prekinuta tijekom čekanja")
                    break

                if i % 3 == 0:
                    try:
                        await xmpp["xep_0199"].ping(xmpp.boundjid.host, timeout=3)
                    except Exception:
                        pass

                await asyncio.sleep(1)

            xmpp.disconnect()

            return self._format_response(
                sent=True,
                sent_encrypted=xmpp.sent_encrypted,
                received=xmpp.received_messages,
                error=xmpp.send_error,
            )

        except Exception as e:
            logger.exception("Greška pri slanju")
            return self._format_response(
                sent=False, sent_encrypted=False, received=[], error=str(e)
            )

    async def _async_check_messages(self, wait_seconds: int) -> str:
        """Samo provjerava nove poruke bez slanja"""

        class OMEMOChecker(ClientXMPP):
            def __init__(self, jid, pw):
                super().__init__(jid, pw, sasl_mech="SCRAM-SHA-1")

                # DODANO: xep_0184 i xep_0333
                for plugin in [
                    "xep_0030",
                    "xep_0045",
                    "xep_0060",
                    "xep_0163",
                    "xep_0184",
                    "xep_0199",
                    "xep_0280",
                    "xep_0333",
                    "xep_0334",
                    "xep_0380",
                ]:
                    self.register_plugin(plugin)

                safe_jid = jid.split("@")[0].replace(".", "_")
                data_dir = Path.home() / ".slixmpp_omemo"
                data_dir.mkdir(parents=True, exist_ok=True)

                self.register_plugin(
                    "xep_0384",
                    {"json_file_path": str(data_dir / f"{safe_jid}.json")},
                    module=sys.modules[__name__],
                )

                self.add_event_handler("session_start", self.session_start)
                self.add_event_handler("failed_auth", self.failed_auth)
                self.add_event_handler("message", self.message_handler)

                self.init_event = asyncio.Event()
                self.send_error = None
                self.received_messages: List[Dict] = []

            async def failed_auth(self, event):
                logger.error(f"AUTENTIFIKACIJA NEUSPJEŠNA: {event}")
                self.send_error = f"AUTENTIFIKACIJA NEUSPJEŠNA: {event}"
                self.disconnect()

            async def session_start(self, event):
                self.send_presence()
                await self.get_roster()

                self.add_event_handler(
                    "omemo_initialized", lambda _: self.init_event.set()
                )
                try:
                    await asyncio.wait_for(self.init_event.wait(), timeout=15)
                except asyncio.TimeoutError:
                    logger.warning("Timeout na OMEMO inicijalizaciju")

            async def send_receipts(self, msg):
                """Šalje potvrde primitka i čitanja (XEP-0184 i XEP-0333)"""
                try:
                    from_jid = msg["from"]
                    msg_id = msg.get("id")

                    if not msg_id:
                        return

                    # 1. Delivery Receipt (XEP-0184)
                    if msg.get("request_receipt"):
                        receipt = self.make_message(mto=from_jid, mtype="chat")
                        receipt["receipt"] = msg_id
                        receipt.send()
                        logger.info(f"✅ Delivery receipt poslan za poruku {msg_id}")

                    # 2. Chat Marker - Read Receipt (XEP-0333)
                    displayed_marker = self.make_message(mto=from_jid, mtype="chat")
                    displayed_marker["chat_marker"] = {
                        "type": "displayed",
                        "id": msg_id,
                    }
                    displayed_marker.send()
                    logger.info(
                        f"✅ Read receipt (displayed marker) poslan za poruku {msg_id}"
                    )

                except Exception as e:
                    logger.warning(f"Greška pri slanju potvrda: {e}")

            async def message_handler(self, msg):
                """Handler za dolazne poruke"""
                if msg["type"] in ("chat", "normal"):
                    from_jid = JID(msg["from"]).bare

                    # KLJUČNO: Pošalji potvrde primitka i čitanja
                    await self.send_receipts(msg)

                    namespaces = self["xep_0384"].is_encrypted(msg)

                    if namespaces:
                        try:
                            decrypted_msg, device_info = await self[
                                "xep_0384"
                            ].decrypt_message(msg)
                            body = decrypted_msg.get("body", "(prazno)")
                            logger.info(f"🔒 OMEMO poruka od {from_jid}: {body}")
                            self.received_messages.append(
                                {"from": str(from_jid), "body": body, "encrypted": True}
                            )
                        except Exception as e:
                            logger.warning(f"Neuspjela dekripcija: {e}")
                    else:
                        body = msg["body"]
                        if body:
                            logger.info(f"📨 Poruka od {from_jid}: {body}")
                            self.received_messages.append(
                                {
                                    "from": str(from_jid),
                                    "body": body,
                                    "encrypted": False,
                                }
                            )

        xmpp = OMEMOChecker(self.address, self.password)

        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        xmpp.ssl_context = context

        try:
            await xmpp.connect()

            try:
                await asyncio.wait_for(xmpp.init_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Timeout na inicijalizaciju")

            wait_seconds = min(wait_seconds, 60)
            logger.info(f"Provjeravam nove poruke ({wait_seconds} sekundi)...")

            for i in range(wait_seconds):
                if not xmpp.is_connected():
                    logger.warning("Veza prekinuta tijekom provjere")
                    break

                if i % 3 == 0:
                    try:
                        await xmpp["xep_0199"].ping(xmpp.boundjid.host, timeout=3)
                    except Exception:
                        pass

                await asyncio.sleep(1)

            xmpp.disconnect()

            return self._format_response(
                sent=False,
                sent_encrypted=False,
                received=xmpp.received_messages,
                error=xmpp.send_error,
            )

        except Exception as e:
            logger.exception("Greška pri provjeri poruka")
            return self._format_response(
                sent=False, sent_encrypted=False, received=[], error=str(e)
            )
