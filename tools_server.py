# tools_server.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.


# PATCHED 2026-11-14 (version-resilient edition, tested on fastmcp
# 2.14.3 and 4.2.0):
#
#   Problem: FastMCP validates every tool call with pydantic BEFORE the
#   tool function runs. When the LLM emits an argument the schema does
#   not declare (a frequent hallucination, e.g. a stray "description"
#   kwarg), the whole call aborts with:
#       1 validation error for call[<fn>]  /  unexpected_keyword_argument
#   The old safe_fn kwargs-filter never ran, because validation happens
#   first.
#
#   Fix = three independent layers, so the chain survives internal
#   changes in future fastmcp versions. If a layer breaks, it degrades
#   with a logged warning instead of crashing:
#
#     L1  TolerantArgsMiddleware - server-level filter (documented,
#         stable middleware API; strips undeclared arguments for every
#         tool of THIS server before validation).
#     L2  TolerantFunctionTool   - Tool subclass that filters arguments
#         against the advertised schema inside run().
#     L3  safe_fn kwargs-filter  - plain Python, version independent.
#
#   Also fixed along the way:
#   - _has_type_hints() is no longer defeated by an unannotated
#     **kwargs: real execute() signatures (incl. defaults like
#     command="") are now used, so "missing argument" becomes the
#     tool's friendly error instead of a pydantic crash.
#   - Per-parameter descriptions from parameters_schema reach the
#     advertised JSON schema via Annotated[Type, "description"].
#   - *args/**kwargs are stripped from signatures handed to FastMCP
#     (it refuses to register **kwargs functions) while defaults and
#     named parameters are preserved.
#   - All status output goes to stderr: in a stdio MCP server stdout is
#     the JSON-RPC transport and print() to stdout can corrupt it.
#   - Dead code _wrap_with_schema removed.

import importlib.util
import inspect
import logging
import sys
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Dict

from fastmcp import FastMCP
from fastmcp.tools import Tool as FastMCPTool
from core.CtrlAsistent_tools import Tool as BaseTool

# L2 is optional: if a future fastmcp moves/renames FunctionTool, we
# degrade to the plain Tool registration (strict validation, but the
# server still runs and L1/L3 keep filtering what they can).
try:
    from fastmcp.tools import FunctionTool
except Exception:  # pragma: no cover - future-proofing
    FunctionTool = None

# L1 is optional in the same spirit: middleware exists since fastmcp
# 2.11 with the same shape (verified in 2.14.3 and 4.2.0), but if it
# disappears we simply lose one layer of tolerance.
try:
    from fastmcp.server.middleware import Middleware
except Exception:  # pragma: no cover - future-proofing
    Middleware = None

mcp_tools = FastMCP("CtrlAsistent Tools")

logger = logging.getLogger("CtrlAsistent Tools")

# Path relative to this file, not to cwd
_TOOLS_DIR = Path(__file__).parent / "tools"


#  DISABLE TOOLS HERE ################################
DISABLED = set()
# Write the tool name here if you want to disable it
DISABLED = {
    # "rag_index",
    # "rag_status",
    # "rag_search",
    # "ddg_search",
    # "write_file",
    # "cmd",
    # "kdeconnect",
    # "matrix_chat",
    # "xmpp_omemo",
    # "memory",
    # "memory_encrypted",
    # "caldav",
    # "image",
    # "weather",
}
# ####################################################


def _log(msg: str) -> None:
    """Status output must go to stderr: in a stdio MCP server stdout is
    the JSON-RPC transport and print() to stdout can corrupt the stream."""
    print(msg, file=sys.stderr)


# ------------------------------------------------------------------
# 1. Loading modules WITHOUT __init__.py
# ------------------------------------------------------------------
def _load_module_from_file(py_file: Path):
    spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[py_file.stem] = module
    spec.loader.exec_module(module)
    return module


def discover_tools(tools_dir: Path) -> Dict[str, BaseTool]:
    if not tools_dir.is_dir():
        _log(f"⚠️  Folder {tools_dir} does not exist")
        return {}

    discovered: Dict[str, BaseTool] = {}
    for py_file in tools_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        try:
            module = _load_module_from_file(py_file)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseTool) and obj is not BaseTool:
                    try:
                        inst = obj()
                        discovered[inst.name] = inst
                    except Exception as e:
                        _log(f"⚠️  Cannot instantiate {obj.__name__}: {e}")
        except Exception as e:
            _log(f"⚠️  Error loading {py_file.name}: {e}")
    return discovered


# ------------------------------------------------------------------
# 2. Signature helpers
# ------------------------------------------------------------------
_NAMED_KINDS = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)
_VAR_KINDS = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _has_type_hints(bound_execute) -> bool:
    """Does the bound method have type hints for all NAMED parameters?

    *args / **kwargs are ignored: they carry no per-argument schema info.
    (An unannotated **kwargs used to make this check fail even for fully
    hinted execute() methods, silently forcing the schema fallback.)"""
    sig = inspect.signature(bound_execute)
    named = [p for p in sig.parameters.values() if p.kind in _NAMED_KINDS]
    return bool(named) and all(
        p.annotation is not inspect.Parameter.empty for p in named
    )


def _signature_from_schema(schema: Dict[str, Any]) -> inspect.Signature:
    """Build an inspect.Signature from BaseTool.parameters_schema."""
    params = [
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=info.get("default", inspect.Parameter.empty),
            annotation=_TYPE_MAP.get(info.get("type", "string"), str),
        )
        for name, info in schema.items()
    ]
    return inspect.Signature(params, return_annotation=Any)


def _strip_var_params(sig: inspect.Signature) -> inspect.Signature:
    """Remove *args/**kwargs from a signature.

    FastMCP refuses to register functions whose signature contains
    *args/**kwargs, and its pydantic call validation is driven by the
    same signature. Named parameters and their defaults are preserved;
    runtime extras are dropped by L1/L2/L3 instead."""
    params = [p for p in sig.parameters.values() if p.kind not in _VAR_KINDS]
    if len(params) == len(sig.parameters):
        return sig
    return sig.replace(parameters=params)


def _annotations_from_signature(
    sig: inspect.Signature, schema: Dict[str, Any]
) -> Dict[str, Any]:
    """Annotations for the wrapper function.

    Parameter descriptions from parameters_schema are attached as
    Annotated[Type, "description"]; FastMCP converts them to
    Field(description=...), so the advertised JSON schema documents
    every parameter for the LLM (previously the descriptions were
    lost, which invites exactly the hallucinated arguments we filter)."""
    annotations: Dict[str, Any] = {}
    for p in sig.parameters.values():
        info = schema.get(p.name) or {}
        base = (
            p.annotation
            if p.annotation is not inspect.Parameter.empty
            else _TYPE_MAP.get(info.get("type", "string"), str)
        )
        desc = info.get("description")
        if isinstance(desc, str) and desc.strip():
            base = Annotated[base, desc]
        annotations[p.name] = base
    annotations["return"] = Any
    return annotations


def _declared_params(tool: Any) -> frozenset | None:
    """Set of parameter names a Tool object declares, across versions.

    The Tool attribute holding the input JSON schema is `parameters` in
    fastmcp 2.x and 4.x; `input_schema` is checked as a fallback for
    future renames. Returns None when the schema cannot be read."""
    schema = getattr(tool, "parameters", None)
    if not isinstance(schema, dict):
        schema = getattr(tool, "input_schema", None)
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return frozenset()
    return frozenset(props)


# ------------------------------------------------------------------
# 3. L1: server-level tolerance (middleware)
# ------------------------------------------------------------------
# If a future fastmcp removes/renames the middleware API (Middleware
# becomes None above), the class still has to be importable - it just
# never gets attached. Hence the fallback base class.
_MiddlewareBase = Middleware if Middleware is not None else object


class TolerantArgsMiddleware(_MiddlewareBase):
    """Strips undeclared arguments before FastMCP validates the call.

    Works at the documented middleware layer, which is the most stable
    public API across fastmcp versions, and covers every tool served by
    the FastMCP instance it is attached to. Unknown tools or unreadable
    schemas are passed through untouched (graceful degradation)."""

    def __init__(self, server: "FastMCP"):
        self._server = server
        # name -> declared params (frozenset). Only successful lookups
        # are cached; failures are retried so late-registered tools
        # are still covered.
        self._cache: Dict[str, frozenset] = {}

    async def _params_for(self, name: str) -> frozenset | None:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        try:
            tool = await self._server.get_tool(name)
        except Exception:
            return None
        params = _declared_params(tool)
        if params is not None:
            self._cache[name] = params
        return params

    async def on_call_tool(self, context, call_next):
        try:
            message = context.message
            name = getattr(message, "name", None)
            args = getattr(message, "arguments", None)
            if name and isinstance(args, dict):
                declared = await self._params_for(name)
                if declared is not None:
                    dropped = set(args) - set(declared)
                    if dropped:
                        logger.warning(
                            "%s: dropping arguments not declared in schema: %s",
                            name,
                            sorted(dropped),
                        )
                        # arguments is a plain dict shared with the whole
                        # downstream call path; mutating it in place keeps
                        # this independent of context-copy semantics.
                        for key in dropped:
                            args.pop(key, None)
        except Exception:  # never break the call because of tolerance
            logger.exception("TolerantArgsMiddleware failed; passing through")
        return await call_next(context)


def attach_tolerant_args_middleware(server: "FastMCP") -> None:
    """Attach the L1 argument filter to ANY FastMCP server instance.

    Public helper for parent servers: call it on the main server before
    mounting, e.g.
        attach_tolerant_args_middleware(mcp)
        mcp.mount(mcp_tools)
    so its inline @mcp.tool() tools get the same tolerance as the tools
    registered here. Safe no-op if the middleware API is unavailable in
    a future fastmcp version (L2/L3 keep working)."""
    if Middleware is None:
        logger.warning(
            "fastmcp middleware API unavailable; "
            "TolerantArgsMiddleware not attached (L2/L3 still active)"
        )
        return
    try:
        server.add_middleware(TolerantArgsMiddleware(server))
    except Exception:  # pragma: no cover - future-proofing
        logger.exception("Could not attach TolerantArgsMiddleware")


# ------------------------------------------------------------------
# 4. L2: tool-level tolerance (Tool subclass)
# ------------------------------------------------------------------
if FunctionTool is not None:

    class TolerantFunctionTool(FunctionTool):
        """FunctionTool that drops undeclared arguments instead of failing.

        FastMCP validates each tool call with a pydantic TypeAdapter
        built over the registered function BEFORE the function body
        executes. This subclass filters the arguments against the
        advertised schema first. Kept as an independent layer: even if
        middleware disappears in a future version, tools registered
        through this class stay tolerant."""

        async def run(self, arguments, *args, **kwargs):
            params = _declared_params(self)
            if params is not None and isinstance(arguments, dict):
                dropped = set(arguments) - set(params)
                if dropped:
                    logger.warning(
                        "%s: dropping arguments not declared in schema: %s",
                        getattr(self, "name", "?"),
                        sorted(dropped),
                    )
                    arguments = {k: v for k, v in arguments.items() if k in params}
            return await super().run(arguments, *args, **kwargs)

else:  # pragma: no cover - future-proofing
    TolerantFunctionTool = None


def register_tool(base_tool: BaseTool) -> None:
    if base_tool.name in DISABLED:
        _log(f"⏭️  Skipping: {base_tool.name}")
        return

    execute = base_tool.execute
    schema = getattr(base_tool, "parameters_schema", {}) or {}

    if _has_type_hints(execute):
        sig = inspect.signature(execute)
    else:
        sig = _signature_from_schema(schema)

    # *args/**kwargs must never reach FastMCP (registration would raise
    # ValueError); named parameters and their defaults are preserved.
    sig = _strip_var_params(sig)

    accepted = {p.name for p in sig.parameters.values() if p.kind in _NAMED_KINDS}

    @wraps(execute)
    def safe_fn(*args, **kwargs):
        # L3: last line of defence, plain Python - covers any caller
        # that invokes the function directly, on any fastmcp version.
        filtered = {k: v for k, v in kwargs.items() if k in accepted}
        dropped = set(kwargs) - set(filtered)
        if dropped:
            logger.warning(
                "%s: dropped unknown kwargs: %s", base_tool.name, sorted(dropped)
            )
        return execute(*args, **filtered)

    safe_fn.__signature__ = sig
    safe_fn.__annotations__ = _annotations_from_signature(sig, schema)

    # L2 when available, plain Tool otherwise. Only fn/name/description
    # are passed - the most stable subset of the from_function API.
    tool = None
    for factory in (TolerantFunctionTool, FastMCPTool):
        if factory is None:
            continue
        try:
            tool = factory.from_function(
                fn=safe_fn,
                name=base_tool.name,
                description=base_tool.description,
            )
            break
        except Exception as e:
            _log(
                f"⚠️  {base_tool.name}: {getattr(factory, '__name__', factory)}"
                f".from_function failed ({e!r}); trying fallback"
            )
    if tool is None:
        raise RuntimeError(f"Cannot register tool {base_tool.name!r}")

    mcp_tools.add_tool(tool)
    _log(f"✅ Registered tool: {base_tool.name} (params: {sorted(accepted) or '-'})")


# ------------------------------------------------------------------
# 5. Automatic Registration
# ------------------------------------------------------------------
for _tool in discover_tools(_TOOLS_DIR).values():
    register_tool(_tool)

# L1: attach the server-level filter to THIS server. Tools mounted into
# another FastMCP instance inherit it through the mount; inline tools
# registered directly on a parent server are covered by calling the
# public helper there BEFORE the mounts:
#     from tools_server import attach_tolerant_args_middleware
#     attach_tolerant_args_middleware(mcp)
attach_tolerant_args_middleware(mcp_tools)


if __name__ == "__main__":
    mcp_tools.run(transport="stdio")
