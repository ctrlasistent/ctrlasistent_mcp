# tool_caldav.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

# python -m radicale --auth-type none
#
# Configuration (all env vars, all optional):
#   CALDAV_USER                 – override the OS-detected username
#   CALDAV_BASE                 – server URL (default http://127.0.0.1:5232)
#   CALDAV_TZID                 – timezone for stored items (default Europe/Zagreb)
#   CALDAV_EVENTS_COLLECTION    – path for events collection
#                                 (default <user>/moj-kalendar, kept for
#                                  backward compatibility with existing data)
#   CALDAV_TASKS_COLLECTION     – path for tasks collection
#                                 (default <user>/moji-zadaci)
#   CALDAV_MEMOS_COLLECTION     – path for memos collection
#                                 (default <user>/moje-biljeske)
#
# Example interactions:
#   User:      Set up CalDAV
#   Assistant: CalDAV is set up. Created collections:
#              * <user>/calendar
#              * <user>/tasks
#              * <user>/notes
#              Ready to add events, tasks, and notes.
#
#   User:      Add a team meeting today at 19:30
#   Assistant: Team meeting has been added for today at 19:30.
#
#   User:      What do I have in my calendar today?
#   Assistant: You have a team meeting today from 19:30 to 20:30.

import os
import uuid
import logging
import xml.etree.ElementTree as ET
import requests
import vobject
import getpass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

from core.CtrlAsistent_tools import Tool, register_tool

logger = logging.getLogger(__name__)


def _get_current_user() -> str:
    # 1) Explicit override
    explicit = os.environ.get("CALDAV_USER")
    if explicit:
        return explicit

    # 2) getpass — has its own fallback chain
    #    (LOGNAME/USER/LNAME/USERNAME → /etc/passwd)
    try:
        user = getpass.getuser()
        if user:
            return user
    except Exception as e:
        logger.warning("getpass.getuser() failed: %s", e)

    logger.warning(
        "Cannot determine current user; falling back to 'user'. "
        "Set CALDAV_USER env var to override. Collections will be created "
        "under 'user/' prefix and will NOT be shared with a GUI instance "
        "running under a different user."
    )
    return "user"


CURRENT_USER = _get_current_user()

# ---------- CONFIGURATION ----------
CALDAV_BASE = os.environ.get("CALDAV_BASE", "http://127.0.0.1:5232")
TZID = os.environ.get("CALDAV_TZID", "Europe/Zagreb")

# Collection paths
_EVENTS_PATH = os.environ.get("CALDAV_EVENTS_COLLECTION", f"{CURRENT_USER}/calendar")
_TASKS_PATH = os.environ.get("CALDAV_TASKS_COLLECTION", f"{CURRENT_USER}/tasks")
_MEMOS_PATH = os.environ.get("CALDAV_MEMOS_COLLECTION", f"{CURRENT_USER}/notes")

# Map between internal keys and (path, label)
COLLECTIONS = {
    "events": (_EVENTS_PATH, "Calendar"),
    "tasks": (_TASKS_PATH, "Tasks"),
    "memos": (_MEMOS_PATH, "Notes"),
}

SETUP_INSTRUCTIONS = """
Radicale server is not reachable at {base}.

To install and run it:

1. Installation:
   - Debian GNU/Linux: sudo apt update && sudo apt install radicale
   - macOS / Windows / others: pip install radicale

2. Linux permission config (/etc/radicale/rights):
   [allow-all]
   user: .*
   collection: .*
   permissions: RrWw

3. Start the server:
   - Linux (systemd): sudo systemctl enable --now radicale
   - macOS / Windows / manual: python -m radicale --auth-type none

4. Once the server is running, call this tool with action 'setup'
   to create the collections.
   Example: "Set up CalDAV"
   Expected reply: "CalDAV is set up. Created collections: ..."

Wiki: https://gitlab.com/CtrlAsistent/ctrlasistent/-/wikis/HR/CalDAV
Upstream: https://radicale.org
""".format(
    base=CALDAV_BASE
)


# ---------- HELPERS ----------
def get_caldav_url(collection: str, filename: Optional[str] = None) -> str:
    url = f"{CALDAV_BASE}/{collection}"
    if filename:
        url += f"/{filename}"
    return url


def _check_server() -> Tuple[bool, str]:
    try:
        resp = requests.get(CALDAV_BASE, auth=(CURRENT_USER, ""), timeout=5)
        return True, f"Server reachable (status {resp.status_code})"
    except requests.exceptions.ConnectionError:
        return False, "Server unreachable (ConnectionError)"
    except Exception as e:
        return False, f"Error checking server: {e}"


def collection_exists(collection_name: str) -> bool:
    """Check whether the collection exists (PROPFIND depth 0)."""
    url = get_caldav_url(collection_name)
    headers = {"Depth": "0", "Content-Type": "application/xml; charset=utf-8"}
    body = """<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
  <prop>
    <resourcetype/>
  </prop>
</propfind>"""
    try:
        resp = requests.request("PROPFIND", url, headers=headers, data=body, timeout=5)
        return resp.status_code in (200, 207)
    except Exception:
        return False


def create_collection(collection_name: str) -> Tuple[bool, str]:
    """Create a collection via MKCALENDAR if it does not already exist."""
    if collection_exists(collection_name):
        return True, f"Collection '{collection_name}' already exists."
    url = get_caldav_url(collection_name)
    try:
        resp = requests.request("MKCALENDAR", url, timeout=10)
        if resp.status_code in (200, 201, 204):
            return True, f"Collection '{collection_name}' created."
        else:
            return (
                False,
                f"Creation failed: {resp.status_code} {resp.text}",
            )
    except Exception as e:
        return False, f"Exception: {e}"


def get_item_list(collection_name: str) -> Tuple[str, Optional[List[Dict[str, str]]]]:
    """
    Fetch the list of .ics files in a collection.
    Returns (status, items_or_error):
      - 'ok'        -> items is a list
      - 'not_found' -> collection does not exist
      - 'error'     -> items_or_error is the error message
    """
    url = get_caldav_url(collection_name)
    headers = {
        "Depth": "1",
        "Content-Type": "application/xml; charset=utf-8",
    }
    body = """<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
  <prop>
    <resourcetype/>
    <getetag/>
  </prop>
</propfind>"""
    try:
        resp = requests.request("PROPFIND", url, headers=headers, data=body, timeout=10)
        if resp.status_code == 404:
            return "not_found", None
        if resp.status_code not in (200, 207):
            return "error", f"PROPFIND failed: {resp.status_code} {resp.text}"

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            return "error", f"XML parse error: {e}"

        ns = {"d": "DAV:"}
        items = []
        for response in root.findall("d:response", ns):
            href_elem = response.find("d:href", ns)
            if href_elem is None:
                continue
            href = href_elem.text
            if not href.endswith(".ics"):
                continue
            filename = href.split("/")[-1]
            propstat = response.find("d:propstat", ns)
            etag = None
            if propstat is not None:
                prop = propstat.find("d:prop", ns)
                if prop is not None:
                    etag_elem = prop.find("d:getetag", ns)
                    if etag_elem is not None:
                        etag = etag_elem.text
            items.append({"href": href, "filename": filename, "etag": etag})
        return "ok", items
    except requests.exceptions.RequestException as e:
        return "error", f"Request exception: {e}"
    except Exception as e:
        return "error", f"Unexpected error: {e}"


def get_ical_content(collection_name: str, filename: str) -> Optional[str]:
    url = get_caldav_url(collection_name, filename)
    try:
        resp = requests.get(url, auth=(CURRENT_USER, ""), timeout=5)
        if resp.status_code == 200:
            return resp.text
        else:
            logger.error(f"GET failed for {filename}: {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"GET exception: {e}")
        return None


def send_ical(collection: str, ical_data: str) -> str:
    uid = str(uuid.uuid4())
    filename = f"{uid}.ics"
    url = get_caldav_url(collection, filename)
    headers = {"Content-Type": "text/calendar; charset=utf-8"}
    try:
        resp = requests.put(url, data=ical_data, headers=headers, timeout=10)
        if resp.status_code in (200, 201, 204):
            return f"✅ Success! UID: {uid}"
        else:
            return f"❌ Error {resp.status_code}: {resp.text}"
    except Exception as e:
        return f"❌ Exception while sending: {e}"


def delete_item(collection_name: str, filename: str) -> Tuple[bool, str]:
    url = get_caldav_url(collection_name, filename)
    try:
        resp = requests.delete(url, timeout=10)
        if resp.status_code in (200, 201, 204):
            return True, f"Deleted: {filename}"
        else:
            return (
                False,
                f"Delete failed {filename}: {resp.status_code} {resp.text}",
            )
    except Exception as e:
        return False, f"Exception while deleting: {e}"


# ---------- DISPLAY HELPERS ----------
def format_datetime(dt) -> str:
    try:
        if hasattr(dt, "strftime"):
            return dt.strftime("%Y-%m-%d %H:%M")
        else:
            return str(dt)
    except Exception:
        return str(dt)


def pretty_print_item(ical_content: str, item_type: str = "event") -> str:
    try:
        cal = vobject.readOne(ical_content)
        component = None
        for comp in cal.components():
            if comp.name in ["VEVENT", "VTODO", "VJOURNAL"]:
                component = comp
                break
        if not component:
            return "[Unknown component]"

        uid = component.uid.value if hasattr(component, "uid") else "N/A"
        summary = (
            component.summary.value if hasattr(component, "summary") else "(no title)"
        )

        lines = []
        if item_type == "event":
            dtstart = component.dtstart.value if hasattr(component, "dtstart") else "?"
            dtend = component.dtend.value if hasattr(component, "dtend") else "?"
            start_str = format_datetime(dtstart)
            end_str = format_datetime(dtend)
            lines.append(f"📅 {summary}")
            lines.append(f"   From: {start_str}  To: {end_str}")
        elif item_type == "task":
            due = component.due.value if hasattr(component, "due") else "No due date"
            status = (
                component.status.value if hasattr(component, "status") else "UNKNOWN"
            )
            due_str = format_datetime(due) if hasattr(due, "strftime") else str(due)
            lines.append(f"📋 {summary}")
            lines.append(f"   Due: {due_str}")
            lines.append(f"   Status: {status}")
        elif item_type == "memo":
            desc = (
                component.description.value if hasattr(component, "description") else ""
            )
            if len(desc) > 60:
                desc = desc[:60] + "..."
            lines.append(f"📝 {summary}")
            lines.append(f"   Description: {desc}")
        else:
            lines.append(f"❓ {summary}")

        lines.append(f"   🆔 {uid}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Parse error: {e}"


def list_items(collection_name: str, label: str, item_type: str) -> str:
    """Return a formatted listing of items in a collection."""
    status, result = get_item_list(collection_name)

    if status == "not_found":
        return (
            f"❌ Collection '{collection_name}' does not exist on the server.\n"
            f"   Create it via Evolution, or call action 'setup' on this tool."
        )

    if status == "error":
        return f"❌ Error: {result}"

    items = result
    if not items:
        return f"📭 Nothing stored under: {label}\n"

    output_lines = [f"\n📂 {label} ({len(items)} item(s)):\n"]
    for idx, item in enumerate(items, 1):
        filename = item["filename"]
        ical_content = get_ical_content(collection_name, filename)
        if ical_content is None:
            output_lines.append(f"{idx}. ⚠️ Cannot fetch content: {filename}")
        else:
            output_lines.append(f"{idx}. {pretty_print_item(ical_content, item_type)}")
        output_lines.append("-" * 50)
    output_lines.append("")
    return "\n".join(output_lines)


# ---------- MAIN EXECUTION HELPERS ----------
def add_event(title: str, start_text: str, end_text: Optional[str] = None) -> str:
    try:
        start = datetime.strptime(start_text, "%Y-%m-%d %H:%M")
        end = (
            start + timedelta(hours=1)
            if end_text is None
            else datetime.strptime(end_text, "%Y-%m-%d %H:%M")
        )
        start_str = start.strftime("%Y%m%dT%H%M%S")
        end_str = end.strftime("%Y%m%dT%H%M%S")
        ical = f"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:{uuid.uuid4()}
DTSTART;TZID={TZID}:{start_str}
DTEND;TZID={TZID}:{end_str}
SUMMARY:{title}
END:VEVENT
END:VCALENDAR"""
        create_collection(COLLECTIONS["events"][0])
        return send_ical(COLLECTIONS["events"][0], ical)
    except ValueError as e:
        return f"❌ Invalid date/time format: {e}"


def add_task(title: str, due_text: str) -> str:
    try:
        due = datetime.strptime(due_text, "%Y-%m-%d %H:%M")
        due_str = due.strftime("%Y%m%dT%H%M%S")
        ical = f"""BEGIN:VCALENDAR
BEGIN:VTODO
UID:{uuid.uuid4()}
SUMMARY:{title}
DUE;TZID={TZID}:{due_str}
STATUS:NEEDS-ACTION
END:VTODO
END:VCALENDAR"""
        create_collection(COLLECTIONS["tasks"][0])
        return send_ical(COLLECTIONS["tasks"][0], ical)
    except ValueError as e:
        return f"❌ Invalid date/time format: {e}"


def add_memo(title: str, description: str) -> str:
    now = datetime.now()
    now_str = now.strftime("%Y%m%dT%H%M%S")
    safe_desc = description.replace("\n", "\\n")
    ical = f"""BEGIN:VCALENDAR
BEGIN:VJOURNAL
UID:{uuid.uuid4()}
DTSTART;TZID={TZID}:{now_str}
SUMMARY:{title}
DESCRIPTION:{safe_desc}
END:VJOURNAL
END:VCALENDAR"""
    create_collection(COLLECTIONS["memos"][0])
    return send_ical(COLLECTIONS["memos"][0], ical)


def delete_item_by_uid(collection_key: str, uid: str) -> str:
    """Delete the item with the given UID from a collection."""
    collection_name = COLLECTIONS[collection_key][0]
    status, items = get_item_list(collection_name)
    if status != "ok":
        return f"❌ Cannot fetch list for deletion: {status}"
    for item in items:
        filename = item["filename"]
        ical_content = get_ical_content(collection_name, filename)
        if ical_content is None:
            continue
        try:
            cal = vobject.readOne(ical_content)
            component = None
            for comp in cal.components():
                if comp.name in ["VEVENT", "VTODO", "VJOURNAL"]:
                    component = comp
                    break
            if component and hasattr(component, "uid") and component.uid.value == uid:
                success, msg = delete_item(collection_name, filename)
                return msg
        except Exception:
            continue
    return f"❌ No item with UID {uid} was found."


# ---------- CtrlAsistent TOOL ----------
@register_tool
class CalDAVTool(Tool):
    @property
    def name(self) -> str:
        return "caldav"

    @property
    def description(self) -> str:
        return (
            "Manages calendar, tasks and notes via a local CalDAV server "
            "(Radicale). Actions: list (events), list-tasks, list-memos, "
            "add (event), add-task, add-memo, delete, setup.\n"
            "IMPORTANT: When the user specifies a date and time, convert it "
            "to the format 'YYYY-MM-DD HH:MM' before sending it to the tool.\n"
            "Examples:\n"
            '{"tool": "caldav", "arguments": {"action": "list"}}\n'
            '{"tool": "caldav", "arguments": {"action": "add", "title": "Meeting", "start": "2026-08-25 10:00", "end": "2026-08-25 11:00"}}\n'
            '{"tool": "caldav", "arguments": {"action": "add-task", "title": "Buy milk", "due": "2026-08-24 18:00"}}\n'
            '{"tool": "caldav", "arguments": {"action": "add-memo", "title": "Idea", "description": "Prepare slides"}}\n'
            '{"tool": "caldav", "arguments": {"action": "delete", "collection": "events", "uid": "xxxx-xxxx-xxxx"}}\n'
            '{"tool": "caldav", "arguments": {"action": "setup"}}'
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "action": {
                "type": "string",
                "description": (
                    "Action: list, list-tasks, list-memos, add, add-task, "
                    "add-memo, delete, setup"
                ),
                "enum": [
                    "list",
                    "list-tasks",
                    "list-memos",
                    "add",
                    "add-task",
                    "add-memo",
                    "delete",
                    "setup",
                ],
            },
            "title": {
                "type": "string",
                "description": (
                    "Event, task or note title "
                    "(required for add, add-task, add-memo)"
                ),
                "default": "",
            },
            "start": {
                "type": "string",
                "description": ("Event start in format 'YYYY-MM-DD HH:MM' (for add)"),
                "default": "",
            },
            "end": {
                "type": "string",
                "description": (
                    "Event end in format 'YYYY-MM-DD HH:MM' "
                    "(for add, optional — defaults to start + 1 hour)"
                ),
                "default": "",
            },
            "due": {
                "type": "string",
                "description": (
                    "Task due date in format 'YYYY-MM-DD HH:MM' (for add-task)"
                ),
                "default": "",
            },
            "description": {
                "type": "string",
                "description": "Note body text (for add-memo)",
                "default": "",
            },
            "collection": {
                "type": "string",
                "description": ("Collection type: events, tasks, memos (for delete)"),
                "enum": ["events", "tasks", "memos"],
                "default": "",
            },
            "uid": {
                "type": "string",
                "description": "UID of the item to delete (for delete)",
                "default": "",
            },
        }

    def execute(
        self,
        action: str,
        title: str = "",
        start: str = "",
        end: str = "",
        due: str = "",
        description: str = "",
        collection: str = "",
        uid: str = "",
    ) -> str:
        try:
            if action == "setup":
                return self._setup()

            if action == "delete":
                if not collection or not uid:
                    return "Error: 'collection' and 'uid' are required for delete."
                if collection not in COLLECTIONS:
                    return f"Error: unknown collection '{collection}'."
                return delete_item_by_uid(collection, uid)

            collection_map = {
                "list": "events",
                "list-tasks": "tasks",
                "list-memos": "memos",
                "add": "events",
                "add-task": "tasks",
                "add-memo": "memos",
            }
            if action not in collection_map:
                return f"Unknown action: {action}"

            collection_key = collection_map[action]
            collection_name, label = COLLECTIONS[collection_key]

            success, msg = create_collection(collection_name)
            if not success:
                return f"❌ Failed to create collection: {msg}"

            if action == "list":
                return list_items(collection_name, label, "event")
            elif action == "list-tasks":
                return list_items(collection_name, label, "task")
            elif action == "list-memos":
                return list_items(collection_name, label, "memo")
            elif action == "add":
                if not title or not start:
                    return "Error: 'title' and 'start' are required for add."
                return add_event(title, start, end if end else None)
            elif action == "add-task":
                if not title or not due:
                    return "Error: 'title' and 'due' are required for add-task."
                return add_task(title, due)
            elif action == "add-memo":
                if not title or not description:
                    return "Error: 'title' and 'description' are required for add-memo."
                return add_memo(title, description)

            return f"Unknown action: {action}"
        except Exception as e:
            logger.error(f"Error in CalDAV tool: {e}")
            return f"Error executing CalDAV command: {e}"

    def _setup(self) -> str:
        output = []
        ok, msg = _check_server()
        if not ok:
            output.append(f"❌ Server not reachable: {msg}")
            output.append(SETUP_INSTRUCTIONS)
            return "\n".join(output)
        output.append(f"✅ {msg}")

        for key, (col_name, label) in COLLECTIONS.items():
            success, msg = create_collection(col_name)
            if success:
                output.append(f"✅ {msg}")
            else:
                output.append(f"❌ {msg}")

        output.append("\n✅ Setup complete. Collections are ready to use.")
        return "\n".join(output)
