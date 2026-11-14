# tool_kdeconnect.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import os
import tempfile
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.CtrlAsistent_tools import Tool, register_tool

logger = logging.getLogger(__name__)


@register_tool
class KDEConnectTool(Tool):
    """Omogućuje slanje datoteka i poruka putem KDE Connect."""

    @property
    def name(self) -> str:
        return "kdeconnect"

    @property
    def description(self) -> str:
        return (
            "Upravlja mobilnim/smartphone uređajima. Može poslati datoteku, poruku, "
            "pingati uređaj ili izlistati dostupne uređaje.\n"
            "Dostupne akcije (action): list, ping, send_file, send_text\n"
            "Primjeri:\n"
            '{"tool": "kdeconnect", "arguments": {"action": "list"}}\n'
            '{"tool": "kdeconnect", "arguments": {"action": "send_file", "file_path": "test.txt"}}\n'
            '{"tool": "kdeconnect", "arguments": {"action": "send_text", "message": "Pozdrav"}}'
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "action": {
                "type": "string",
                "description": "Akcija: list, ping, send_file, send_text",
                "enum": ["list", "ping", "send_file", "send_text"],
            },
            "device_id": {
                "type": "string",
                "description": "ID uređaja ili njegovo ime (npr. 'Galaxy S21'). Ako nije navedeno, koristi se prvi dostupni uređaj.",
                "default": "",
            },
            "file_path": {
                "type": "string",
                "description": "Putanja do datoteke koju treba poslati (za send_file)",
                "default": "",
            },
            "message": {
                "type": "string",
                "description": "Tekst poruke (za send_text)",
                "default": "",
            },
        }

    def execute(
        self, action: str, device_id: str = "", file_path: str = "", message: str = ""
    ) -> str:
        try:
            if action == "list":
                return self._list_devices()

            elif action in ("ping", "send_file", "send_text"):
                # ID uređaja (podržava i ime i ID)
                resolved_id = self._resolve_device_id(device_id)
                if not resolved_id:
                    return "Greška: nema dostupnih KDE Connect uređaja. Provjerite da je mobitel uparen i dostupan."

                if action == "ping":
                    return self._run_command(["-d", resolved_id, "--ping"])

                elif action == "send_file":
                    if not file_path:
                        return "Greška: file_path je obavezan za slanje datoteke."

                    file_path = str(Path(file_path).resolve())

                    if not Path(file_path).is_file():
                        return f"Greška: datoteka '{file_path}' ne postoji."

                    result = self._run_command(
                        ["-d", resolved_id, "--share", file_path]
                    )

                    return f"Slanje na uređaj {resolved_id}:\n{result}"

                elif action == "send_text":
                    if not message:
                        return "Greška: message je obavezan za slanje teksta."

                    tmp_file = Path(tempfile.gettempdir()) / "kdeconnect_message.txt"

                    tmp_file.write_text(message, encoding="utf-8")
                    result = self._run_command(
                        ["-d", resolved_id, "--share", str(tmp_file)]
                    )
                    return f"Poruka poslana na uređaj {resolved_id}:\n{result}"

            return f"Nepoznata akcija: {action}. Dostupne akcije: list, ping, send_file, send_text"
        except Exception as e:
            logger.error(f"Greška u KDE Connect alatu: {e}")
            return f"Greška pri izvršavanju KDE Connect naredbe: {e}"

    def _list_devices(self) -> str:
        """Vraća popis uređaja s ID-om i imenom."""
        cli = self._get_cli_path()

        result = subprocess.run(
            [cli, "-l"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return f"Greška pri dohvaćanju uređaja: {result.stderr.strip()}"

        devices = result.stdout.strip().splitlines()
        if not devices:
            return "Nema dostupnih KDE Connect uređaja."

        formatted = []
        for line in devices:
            # Primjer linije: "- Galaxy S9: 123abc [paired]"
            line = line.strip()
            if not line or line.startswith("-") is False:
                continue
            # Ukloni vodeću crticu
            line = line.lstrip("-").strip()
            if ":" in line:
                name, id_and_status = line.split(":", 1)
                id_part = (
                    id_and_status.strip().split()[0]
                    if id_and_status.strip()
                    else "nepoznat"
                )
                formatted.append(f"Ime: {name.strip()}, ID: {id_part}")
            else:
                formatted.append(line)

        return (
            "Dostupni uređaji:\n" + "\n".join(formatted)
            if formatted
            else "Nema dostupnih uređaja."
        )

    def _resolve_device_id(self, device_id: str) -> Optional[str]:
        """ID uređaj iz imena ili ID-a."""
        devices = self._get_device_list()
        if not devices:
            return None

        # Ako je device_id naveden, traži podudaranje
        if device_id:
            for dev in devices:
                if (
                    device_id.lower() == dev["id"].lower()
                    or device_id.lower() == dev["name"].lower()
                ):
                    return dev["id"]
            # Ako nema izravnog podudaranja, možda je dio ID-a
            for dev in devices:
                if device_id.lower() in dev["id"].lower():
                    return dev["id"]
            return None

        # Ako nije naveden, vrati prvi ID
        return devices[0]["id"] if devices else None

    def _get_cli_path(self) -> str:
        """Pronalazi kdeconnect-cli na Windowsu ili Linuxu."""

        # Ako je već u PATH-u
        cli = shutil.which("kdeconnect-cli")
        if cli:
            return cli

        # Windows standardna instalacija
        if os.name == "nt":
            candidates = [
                Path(os.environ.get("PROGRAMFILES", ""))
                / "KDE Connect"
                / "bin"
                / "kdeconnect-cli.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", ""))
                / "KDE Connect"
                / "bin"
                / "kdeconnect-cli.exe",
            ]

            for path in candidates:
                if path.exists():
                    return str(path)

        raise FileNotFoundError(
            "kdeconnect-cli nije pronađen. " "Provjerite instalaciju KDE Connecta."
        )

    def _get_device_list(self) -> List[Dict[str, str]]:
        """Vraća listu uređaja kao rječnike s 'id' i 'name'."""
        cli = self._get_cli_path()

        result = subprocess.run(
            [cli, "-l"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        devices = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line or not line.startswith("-"):
                continue
            line = line.lstrip("-").strip()
            if ":" in line:
                name, id_and_status = line.split(":", 1)
                id_part = (
                    id_and_status.strip().split()[0] if id_and_status.strip() else ""
                )
                if id_part:
                    devices.append({"name": name.strip(), "id": id_part})
        return devices

    def _run_command(self, args: List[str]) -> str:
        cli = self._get_cli_path()
        cmd = [cli] + args

        logger.info("KDE command: %r", cmd)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        logger.info("KDE returncode: %s", result.returncode)
        logger.info("KDE stdout: %r stderr: %r", result.stdout, result.stderr)

        if result.returncode != 0:
            return f"Greška: {result.stderr.strip() or 'nepoznata greška'}"

        return result.stdout.strip() if result.stdout else "Naredba uspješno izvršena."
