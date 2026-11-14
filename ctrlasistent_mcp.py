# ctrlasistent_mcp.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

######################################################################
# Load .env file (credentials, tokens) - no client config needed
from pathlib import Path

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())
####################################################################

import re
import random
import subprocess
import platform
import math
import logging
from datetime import datetime
from fastmcp import FastMCP

from tools_server import attach_tolerant_args_middleware, mcp_tools
from skills_server import mcp_skills

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("CtrlAsistent MCP server")


# Create MCP server
mcp = FastMCP("CtrlAsistent MCP server")

# L1 tolerance also for tools inc. inline @mcp.tool() (number_converter and others)
attach_tolerant_args_middleware(mcp)


# inline tool
@mcp.tool()
def number_converter(broj: str) -> str:
    """
    Universal number converter between decimal, hexadecimal, octal and binary
    representations (32-bit, two's complement).

    Automatically detects the input format:
    - Decimal:  "255", "-42", "1000"
    - Hex:      "0xFF", "0x1A", "-0xAB"
    - Binary:   "0b1010", "-0b1101"
    - Octal:    "0o377", "-0o52"
    """
    originalni_unos = broj.strip()

    if originalni_unos[:1] in ("+", "-"):
        predznak = originalni_unos[0]
        bez_predznaka = originalni_unos[1:]
    else:
        predznak, bez_predznaka = "", originalni_unos

    niska = bez_predznaka.lower()
    negativan = predznak == "-"

    if niska.startswith("0x"):
        ulazni_format = "hexadecimal"
        vrijednost_str = bez_predznaka[2:]
        if not re.fullmatch(r"[0-9A-Fa-f]+", vrijednost_str):
            return f"❌ Error: '{originalni_unos}' is not a valid hexadecimal number (use 0-9, A-F)"
        dekadski = -int(vrijednost_str, 16) if negativan else int(vrijednost_str, 16)

    elif niska.startswith("0b"):
        ulazni_format = "binary"
        vrijednost_str = bez_predznaka[2:]
        if not re.fullmatch(r"[01]+", vrijednost_str):
            return f"❌ Error: '{originalni_unos}' is not a valid binary number (only 0 and 1)"
        dekadski = -int(vrijednost_str, 2) if negativan else int(vrijednost_str, 2)

    elif niska.startswith("0o"):
        ulazni_format = "octal"
        vrijednost_str = bez_predznaka[2:]
        if not re.fullmatch(r"[0-7]+", vrijednost_str):
            return f"❌ Error: '{originalni_unos}' is not a valid octal number (use 0-7)"
        dekadski = -int(vrijednost_str, 8) if negativan else int(vrijednost_str, 8)

    else:
        ulazni_format = "decimal"
        if not re.fullmatch(r"[0-9]+", bez_predznaka):
            return f"❌ Error: '{originalni_unos}' is not a valid integer"
        dekadski = -int(bez_predznaka) if negativan else int(bez_predznaka)

    min_32bit, max_32bit = -2147483648, 2147483647
    if dekadski < min_32bit or dekadski > max_32bit:
        return f"❌ Error: {dekadski} is outside the 32-bit range [{min_32bit}, {max_32bit}]"

    maska_32bit = 0xFFFFFFFF
    nepotpisani = dekadski & maska_32bit

    decimalno = str(dekadski)
    heks_kratko = f"0x{nepotpisani:X}"
    binarno_kratko = f"0b{nepotpisani:b}"
    oktalno_str = f"0o{nepotpisani:o}"

    heksadecimalno = f"0x{nepotpisani:08X}"
    binarno_32bit = f"{nepotpisani:032b}"
    binarno_grouped = " ".join(binarno_32bit[i : i + 4] for i in range(0, 32, 4))
    oktalno = f"0o{nepotpisani:011o}"

    return (
        f"NUMBER CONVERSION (32-bit, two's complement)\n"
        f"\n"
        f"Input:        {originalni_unos}\n"
        f"Input format: {ulazni_format}\n"
        f"\n"
        f"1. Decimal: {decimalno}\n"
        f"2. Hex:     {heks_kratko}\n"
        f"3. Binary:  {binarno_kratko}\n"
        f"4. Octal:   {oktalno_str}\n"
        f"\n"
        f"FULL 32-BIT VIEW:\n"
        f"Hex: {heksadecimalno}\n"
        f"Bin: {binarno_grouped}\n"
        f"Oct: {oktalno}"
    )


mcp.mount(mcp_tools)
mcp.mount(mcp_skills)

if __name__ == "__main__":
    # Run server (stdio transport)
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass
