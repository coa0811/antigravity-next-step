#!/usr/bin/env python3
"""Core state machine, persistence, staleness detection, and view-model builders for next-step plugin."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

MAX_HASH_FILE_BYTES = 2_000_000
WATCHDOG_TIMEOUT_SEC = 120.0
TOOL_APPROVAL_HINT_SEC = 8.0
MAX_SCANNED_SESSIONS = 60
PLAN_STAT_SETTLE_NS = 2_000_000_000
MAX_CONVERSATION_NAME_LEN = 80
TRANSCRIPT_HEAD_MAX_LINES = 25
TRANSCRIPT_HEAD_MAX_LINE_BYTES = 65536
MAX_TRANSCRIPT_NAME_CACHE = 128
MAX_PBTXT_BYTES = 131072
MAX_ANNOTATION_CACHE = 256
MAX_SUMMARIES_DB_CACHE = 16
MAX_SUMMARIES_DB_BYTES = 64 * 1024 * 1024
MAX_DB_SNAPSHOT_RETRIES = 3
DB_SNAPSHOT_RETRY_DELAY_SEC = 0.02
HOST_TITLE_KEYS = ("conversationTitle", "conversationName", "sessionTitle", "title")
SECTION_TASK_HEADING_RE = re.compile(
    r"^#{1,4}\s*(?:task|objective|goal|request|\d+\.\s*(?:明确目标|目标|任务))\b",
    re.IGNORECASE,
)
_CONV_NAME_PRIORITY = {"": 0, "derived_fallback": 1, "derived": 2, "explicit": 3, "host": 4}
_TRANSCRIPT_NAME_CACHE: dict[tuple[str, int, int, int], str | None] = {}
_ANNOTATION_CACHE: dict[tuple[str, int, int, int], tuple[str | None, dict[str, str]]] = {}
_SUMMARIES_DB_CACHE: dict[
    tuple[Any, ...],
    tuple[dict[str, str], dict[str, str]],
] = {}

MUTATING_FILE_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "delete_file",
}

APPROVAL_CANDIDATE_TOOLS = MUTATING_FILE_TOOLS | {
    "run_command",
    "ask_permission",
    "ask_question",
}

DATA_DIR = Path(
    os.environ.get("PLUGIN_DATA")
    or (Path.home() / ".gemini" / "antigravity" / "plugin_data" / "next-step")
)
SESSIONS_DIR = DATA_DIR / "sessions"
PENDING_DIR = DATA_DIR / "pending"
LATEST_PENDING_PATH = PENDING_DIR / "latest.json"
ERROR_LOG_PATH = DATA_DIR / "error.log"


def set_data_dir(new_dir: Path) -> None:
    """Redirect plugin data directory (used by tests and custom environments)."""
    global DATA_DIR, SESSIONS_DIR, PENDING_DIR, LATEST_PENDING_PATH, ERROR_LOG_PATH
    _TRANSCRIPT_NAME_CACHE.clear()
    _ANNOTATION_CACHE.clear()
    _SUMMARIES_DB_CACHE.clear()
    DATA_DIR = Path(new_dir).resolve()
    SESSIONS_DIR = DATA_DIR / "sessions"
    PENDING_DIR = DATA_DIR / "pending"
    LATEST_PENDING_PATH = PENDING_DIR / "latest.json"
    ERROR_LOG_PATH = DATA_DIR / "error.log"


def ensure_data_dirs() -> None:
    """Create plugin data, session state, and pending buffer directories if absent."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)


def append_error_log(context: str, exc: BaseException) -> None:
    """Append diagnostic error entry without raising."""
    try:
        ensure_data_dirs()
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {context}: {exc}\n{tb}\n")
    except Exception:
        pass


def iso_now(ts: float | None = None) -> str:
    val = time.time() if ts is None else ts
    return datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_arg(val: Any) -> Any:
    """Unwrap JSON-stringified argument values up to 2 levels."""
    cur = val
    for _ in range(2):
        if not isinstance(cur, str):
            break
        s = cur.strip()
        if (
            (s.startswith('"') and s.endswith('"'))
            or (s.startswith("[") and s.endswith("]"))
            or (s.startswith("{") and s.endswith("}"))
        ):
            try:
                cur = json.loads(s)
                continue
            except json.JSONDecodeError:
                break
        break
    return cur


_WRAPPER_XML_PREFIXES = ("<ADDITIONAL_METADATA>", "<USER_SETTINGS_CHANGE>", "<CONTEXT_SUMMARY>")
_WRAPPER_XML_TAGS = ("ADDITIONAL_METADATA", "USER_SETTINGS_CHANGE", "CONTEXT_SUMMARY")
_ENVELOPE_TAG_RE = re.compile(
    r"<(?P<close>/)?(?P<name>USER_REQUEST|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|CONTEXT_SUMMARY)>"
)
_WRAPPER_TAG_RES: dict[str, re.Pattern[str]] = {
    tag: re.compile(rf"<(?P<close>/)?{tag}>") for tag in _WRAPPER_XML_TAGS
}


def _strip_wrapper_xml(text: str, *, strip_truncated: bool = False) -> str:
    """Process outermost envelope blocks left-to-right with depth-balanced wrapper tag skipping."""
    out: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        match = _ENVELOPE_TAG_RE.search(text, pos)
        if not match:
            out.append(text[pos:])
            break
        if match.start() > pos:
            out.append(text[pos : match.start()])

        name = match.group("name")
        if match.group("close"):
            if name != "USER_REQUEST":
                out.append(match.group(0))
            pos = match.end()
            continue

        if name == "USER_REQUEST":
            close_idx = text.find("</USER_REQUEST>", match.end())
            if close_idx >= 0:
                out.append(text[match.end() : close_idx])
                pos = close_idx + len("</USER_REQUEST>")
            else:
                pos = match.end()
            continue

        depth = 1
        closed_end: int | None = None
        for tm in _WRAPPER_TAG_RES[name].finditer(text, match.end()):
            if tm.group("close"):
                depth -= 1
                if depth == 0:
                    closed_end = tm.end()
                    break
            else:
                depth += 1

        if closed_end is not None:
            pos = closed_end
        elif strip_truncated:
            pos = n
            break
        else:
            out.append(match.group(0))
            pos = match.end()
    return "".join(out)


def sanitize_user_input(raw: Any, *, unwrap_xml: bool = True) -> str:
    """Strip wrapper XML tags and collapse whitespace into a concise user prompt summary."""
    text = str(raw or "")
    if unwrap_xml:
        text = _strip_wrapper_xml(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def sanitize_conversation_name(raw: Any, max_len: int = MAX_CONVERSATION_NAME_LEN) -> str | None:
    """Normalize control chars and whitespace into a concise single-line conversation title."""
    if raw is None:
        return None
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(raw))
    text = re.sub(r"\s+", " ", text).strip()
    return _clip_str(text, max_len)


def _replace_at_file_mention(match: re.Match[str]) -> str:
    inner = match.group(1).strip()
    if "/" in inner or "\\" in inner:
        cleaned = inner.rstrip("/\\")
        base = cleaned.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
        if base:
            return f"@{base}"
    return match.group(0)


def _strip_line_markdown_prefix(line: str) -> str:
    cleaned = re.sub(r"^(?:#{1,6}\s+|>\s+|[-*]\s+)", "", line).strip()
    if cleaned.startswith("**") and cleaned.endswith("**") and len(cleaned) > 4:
        cleaned = cleaned[2:-2].strip()
    return cleaned


def derive_conversation_name(
    raw: Any,
    max_len: int = MAX_CONVERSATION_NAME_LEN,
    *,
    strip_truncated: bool = False,
    unwrap_xml: bool = True,
) -> str | None:
    """Derive a readable conversation title from an initial prompt or structured task block."""
    if raw is None:
        return None
    text = str(raw)
    if unwrap_xml:
        text = _strip_wrapper_xml(text, strip_truncated=strip_truncated)
    text = re.sub(r"@\[([^\]]+)\]", _replace_at_file_mention, text)

    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"[-*=~_\s]{3,}", line):
            continue
        lines.append(line)

    if not lines:
        return None

    candidate: str | None = None
    for idx, line in enumerate(lines):
        if SECTION_TASK_HEADING_RE.match(line):
            for nxt in lines[idx + 1 :]:
                if not nxt.startswith("#"):
                    candidate = nxt
                    break
            if candidate is not None:
                break

    if candidate is None:
        for line in lines:
            stripped = _strip_line_markdown_prefix(line)
            if stripped and not re.fullmatch(r"[-*=~_\s]{3,}", stripped):
                candidate = stripped
                break
    else:
        stripped = _strip_line_markdown_prefix(candidate)
        if stripped and not re.fullmatch(r"[-*=~_\s]{3,}", stripped):
            candidate = stripped
        else:
            candidate = None
            for line in lines:
                fallback_line = _strip_line_markdown_prefix(line)
                if fallback_line and not re.fullmatch(r"[-*=~_\s]{3,}", fallback_line):
                    candidate = fallback_line
                    break

    return sanitize_conversation_name(candidate, max_len=max_len)


def extract_first_user_input_from_transcript(transcript_path: str | None) -> str | None:
    """Read the first explicit USER_INPUT prompt from the head of a known transcript file."""
    raw_path = str(transcript_path or "").strip()
    if not raw_path:
        return None
    try:
        p = Path(raw_path).expanduser()
        if not p.is_file():
            return None
        fallback_content: str | None = None
        with p.open("rb") as fh:
            for _ in range(TRANSCRIPT_HEAD_MAX_LINES):
                raw_line = fh.readline(TRANSCRIPT_HEAD_MAX_LINE_BYTES + 1)
                if not raw_line or len(raw_line) > TRANSCRIPT_HEAD_MAX_LINE_BYTES:
                    break
                s = raw_line.decode("utf-8", errors="replace").strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "USER_INPUT":
                    continue
                content = obj.get("content")
                if not isinstance(content, str):
                    continue
                truncated_fields = obj.get("truncated_fields")
                is_content_truncated = (
                    isinstance(truncated_fields, list) and "content" in truncated_fields
                )
                effective_content = _strip_wrapper_xml(
                    content,
                    strip_truncated=is_content_truncated,
                )
                if not derive_conversation_name(effective_content, unwrap_xml=False):
                    continue
                if obj.get("source") == "USER_EXPLICIT":
                    return effective_content
                if fallback_content is None:
                    fallback_content = effective_content
        return fallback_content
    except (OSError, RuntimeError):
        return None


def derive_conversation_name_from_transcript(transcript_path: str | None) -> str | None:
    """Cached, stat-verified read-only derivation of conversation title from transcript head."""
    raw_path = str(transcript_path or "").strip()
    if not raw_path:
        return None
    try:
        p = Path(raw_path).expanduser()
        if not p.is_file():
            return None
        st = p.stat()
    except (OSError, RuntimeError):
        return None

    cache_key = (str(p), st.st_mtime_ns, st.st_size, st.st_ino)
    if cache_key in _TRANSCRIPT_NAME_CACHE:
        return _TRANSCRIPT_NAME_CACHE[cache_key]

    derived = derive_conversation_name(
        extract_first_user_input_from_transcript(raw_path),
        unwrap_xml=False,
    )
    if len(_TRANSCRIPT_NAME_CACHE) >= MAX_TRANSCRIPT_NAME_CACHE:
        _TRANSCRIPT_NAME_CACHE.clear()
    _TRANSCRIPT_NAME_CACHE[cache_key] = derived
    return derived


def _apply_conversation_name(state: dict[str, Any], name: Any, source: str) -> None:
    clean = sanitize_conversation_name(name)
    if not clean:
        return
    cur_source = str(state.get("conversationNameSource") or "")
    cur_prio = _CONV_NAME_PRIORITY.get(cur_source, 0)
    new_prio = _CONV_NAME_PRIORITY.get(source, 0)
    if (
        new_prio > cur_prio
        or (new_prio == cur_prio and source in ("host", "explicit"))
        or not str(state.get("conversationName") or "").strip()
    ):
        state["conversationName"] = clean
        state["conversationNameSource"] = source


def _derive_from_stored_input(raw: Any) -> str | None:
    s = str(raw or "")
    if not s.strip():
        return None
    is_likely_truncated = len(s) >= 240 or s.lstrip().startswith(_WRAPPER_XML_PREFIXES)
    return derive_conversation_name(s, strip_truncated=is_likely_truncated)


_PBTXT_SIMPLE_ESCAPES: dict[str, int] = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": 0x5C,
    "'": 0x27,
    '"': 0x22,
    "?": 0x3F,
}


def _is_safe_session_id_segment(val: str) -> bool:
    return (
        bool(val)
        and val not in (".", "..")
        and "/" not in val
        and "\\" not in val
        and ".." not in val
        and "\x00" not in val
    )


def _resolve_session_app_data_dirs(state: dict[str, Any]) -> list[Path]:
    """Resolve candidate Antigravity appDataDir roots from session paths without leaking host state into tests."""
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add_candidate(d: Path) -> None:
        try:
            resolved = d.resolve()
        except (OSError, RuntimeError):
            resolved = d
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(d)

    for key in ("artifactDirectoryPath", "transcriptPath"):
        raw = str(state.get(key) or "").strip()
        if not raw:
            continue
        try:
            p = Path(raw).expanduser()
        except (OSError, RuntimeError):
            continue
        for anc in p.parents:
            if anc.name == "brain":
                _add_candidate(anc.parent)
                break

    if DATA_DIR.parent.name == "plugin_data":
        _add_candidate(DATA_DIR.parent.parent)
    return candidates


def _unescape_pbtxt_string(raw_inner: str) -> str:
    """Decode protobuf text-format string escapes (including octal UTF-8 sequences and literal UTF-8)."""
    buf = bytearray()
    n = len(raw_inner)
    i = 0
    while i < n:
        ch = raw_inner[i]
        if ch != "\\":
            buf.extend(ch.encode("utf-8", errors="replace"))
            i += 1
            continue
        if i + 1 >= n:
            buf.append(0x5C)
            break
        esc = raw_inner[i + 1]
        if "0" <= esc <= "7":
            j = i + 1
            while j < n and j < i + 4 and "0" <= raw_inner[j] <= "7":
                j += 1
            buf.append(int(raw_inner[i + 1 : j], 8) & 0xFF)
            i = j
            continue
        if esc == "x":
            j = i + 2
            while j < n and j < i + 4 and raw_inner[j] in "0123456789abcdefABCDEF":
                j += 1
            if j > i + 2:
                buf.append(int(raw_inner[i + 2 : j], 16))
                i = j
                continue
        if esc in ("u", "U"):
            width = 4 if esc == "u" else 8
            hex_part = raw_inner[i + 2 : i + 2 + width]
            if len(hex_part) == width and all(c in "0123456789abcdefABCDEF" for c in hex_part):
                cp = int(hex_part, 16)
                if 0 <= cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF):
                    buf.extend(chr(cp).encode("utf-8"))
                else:
                    buf.extend("\ufffd".encode("utf-8"))
                i += 2 + width
                continue
        simple = _PBTXT_SIMPLE_ESCAPES.get(esc)
        if simple is not None:
            buf.append(simple)
        else:
            buf.extend(esc.encode("utf-8", errors="replace"))
        i += 2
    return buf.decode("utf-8", errors="replace")


def _skip_pbtxt_ws_and_comments(text: str, i: int, n: int) -> int:
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "#":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
        else:
            break
    return i


def _scan_pbtxt_string_literal(text: str, i: int, n: int) -> tuple[str | None, int]:
    quote_ch = text[i]
    i += 1
    start = i
    while i < n:
        ch = text[i]
        if ch == "\\":
            i = min(n, i + 2)
        elif ch == quote_ch:
            return _unescape_pbtxt_string(text[start:i]), i + 1
        elif ch in "\r\n":
            return None, i
        else:
            i += 1
    return None, n


def _read_pbtxt_string_literals(text: str, i: int, n: int) -> tuple[str | None, int]:
    pos = _skip_pbtxt_ws_and_comments(text, i, n)
    if pos >= n or text[pos] not in ('"', "'"):
        return None, i
    parts: list[str] = []
    while pos < n and text[pos] in ('"', "'"):
        chunk, next_pos = _scan_pbtxt_string_literal(text, pos, n)
        if chunk is None:
            return None, next_pos
        parts.append(chunk)
        pos = _skip_pbtxt_ws_and_comments(text, next_pos, n)
    return "".join(parts), pos


def _parse_pbtxt_annotation(text: str) -> tuple[str | None, dict[str, str]]:
    """Parse top-level `title` and `side_question_titles` map entries from Antigravity .pbtxt annotations."""
    n = len(text)
    i = 0
    depth = 0
    top_title: str | None = None
    side_titles: dict[str, str] = {}
    in_side_entry = False
    side_key: str | None = None
    side_val: str | None = None

    while i < n:
        i = _skip_pbtxt_ws_and_comments(text, i, n)
        if i >= n:
            break
        ch = text[i]
        if ch in ('"', "'"):
            _, i = _read_pbtxt_string_literals(text, i, n)
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            if depth == 1 and in_side_entry:
                if side_key and side_val is not None:
                    clean_val = sanitize_conversation_name(side_val)
                    if clean_val:
                        side_titles[side_key] = clean_val
                in_side_entry = False
                side_key = None
                side_val = None
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            ident_start = i
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            ident = text[ident_start:i]
            j = _skip_pbtxt_ws_and_comments(text, i, n)
            if depth == 0 and ident == "title":
                if j < n and text[j] == ":":
                    k = _skip_pbtxt_ws_and_comments(text, j + 1, n)
                    val, next_i = _read_pbtxt_string_literals(text, k, n)
                    if val is not None:
                        clean = sanitize_conversation_name(val)
                        if clean:
                            top_title = clean
                    i = next_i
                    continue
                i = j
                continue
            if depth == 0 and ident == "side_question_titles":
                k = j
                if k < n and text[k] == ":":
                    k = _skip_pbtxt_ws_and_comments(text, k + 1, n)
                if k < n and text[k] == "{":
                    depth = 1
                    in_side_entry = True
                    side_key = None
                    side_val = None
                    i = k + 1
                    continue
                i = j
                continue
            if depth == 1 and in_side_entry and ident in ("key", "value"):
                if j < n and text[j] == ":":
                    k = _skip_pbtxt_ws_and_comments(text, j + 1, n)
                    val, next_i = _read_pbtxt_string_literals(text, k, n)
                    if val is not None:
                        if ident == "key":
                            side_key = val.strip()
                        else:
                            side_val = val
                        i = next_i
                        continue
                i = j
                continue
            i = j
            continue
        i += 1
    return top_title, side_titles


def _read_pbtxt_annotation(pb_path: Path) -> tuple[str | None, dict[str, str]]:
    """Stat-cached, read-only reader for Antigravity annotation .pbtxt files."""
    try:
        if not pb_path.is_file():
            return None, {}
        st = pb_path.stat()
    except (OSError, RuntimeError):
        return None, {}

    cache_key = (str(pb_path), st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _ANNOTATION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        with pb_path.open("rb") as fh:
            raw = fh.read(MAX_PBTXT_BYTES)
        parsed = _parse_pbtxt_annotation(raw.decode("utf-8", errors="replace"))
    except (OSError, RuntimeError):
        return None, {}

    if len(_ANNOTATION_CACHE) >= MAX_ANNOTATION_CACHE:
        _ANNOTATION_CACHE.clear()
    _ANNOTATION_CACHE[cache_key] = parsed
    return parsed


def _snapshot_db_signature(db_path: Path) -> tuple[str, tuple[Any, ...] | None, int]:
    """Capture a strictly O_RDONLY consistency signature across DB, WAL, journal, and SHM headers."""
    try:
        if not db_path.is_file():
            return "MISSING", None, 0
    except (OSError, RuntimeError):
        return "MISSING", None, 0

    try:
        st = db_path.stat()
        db_stat = (st.st_mtime_ns, st.st_size, st.st_ino)
        with db_path.open("rb") as fh:
            db_hdr = fh.read(100)

        journal_path = Path(f"{db_path}-journal")
        journal_stat: tuple[int, int, int] | None = None
        if journal_path.is_file():
            jst = journal_path.stat()
            if jst.st_size > 0:
                return "UNSTABLE", None, 0
            journal_stat = (jst.st_mtime_ns, jst.st_size, jst.st_ino)

        wal_path = Path(f"{db_path}-wal")
        wal_stat: tuple[int, int, int] | None = None
        wal_hdr = b""
        if wal_path.is_file():
            wst = wal_path.stat()
            wal_stat = (wst.st_mtime_ns, wst.st_size, wst.st_ino)
            if wst.st_size > 0:
                with wal_path.open("rb") as fh:
                    wal_hdr = fh.read(32)

        shm_path = Path(f"{db_path}-shm")
        shm_sig: tuple[bytes, bytes] | None = None
        if shm_path.is_file():
            with shm_path.open("rb") as fh:
                shm_raw = fh.read(100)
            if len(shm_raw) >= 96 and shm_raw[0:48] != shm_raw[48:96]:
                return "UNSTABLE", None, 0
            shm_sig = (shm_raw[0:48], shm_raw[96:100])
    except FileNotFoundError:
        return "UNSTABLE", None, 0
    except (OSError, RuntimeError):
        return "MISSING", None, 0

    total_bytes = db_stat[1] + (wal_stat[1] if wal_stat else 0)
    sig = (str(db_path), db_stat, db_hdr, wal_stat, wal_hdr, journal_stat, shm_sig)
    return "OK", sig, total_bytes


def _copy_exact_cap(src: Any, dst: Any, cap_bytes: int, chunk_size: int = 1024 * 1024) -> bool:
    """Copy at most `cap_bytes` from `src` to `dst`; return False if `src` exceeds `cap_bytes`."""
    remaining = cap_bytes
    while remaining > 0:
        buf = src.read(min(chunk_size, remaining))
        if not buf:
            return True
        dst.write(buf)
        remaining -= len(buf)
    return not src.read(1)


def _load_summaries_db_maps(db_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Strictly O_RDONLY snapshot loader for Antigravity conversation_summaries.db with zero host file mutation."""
    wal_path = Path(f"{db_path}-wal")
    for attempt in range(MAX_DB_SNAPSHOT_RETRIES):
        status, before_sig, total_bytes = _snapshot_db_signature(db_path)
        if status == "MISSING":
            return {}, {}
        if status == "UNSTABLE" or before_sig is None:
            if attempt + 1 < MAX_DB_SNAPSHOT_RETRIES:
                time.sleep(DB_SNAPSHOT_RETRY_DELAY_SEC)
                continue
            return {}, {}

        cached = _SUMMARIES_DB_CACHE.get(before_sig)
        if cached is not None:
            return cached

        if total_bytes > MAX_SUMMARIES_DB_BYTES:
            return {}, {}

        db_expected = int(before_sig[1][1])
        wal_stat = before_sig[3]
        wal_expected = int(wal_stat[1]) if wal_stat is not None else 0
        try:
            with tempfile.TemporaryDirectory(prefix="next-step-db-") as snap_td:
                snap_db = Path(snap_td) / "snap.db"
                snap_wal = Path(snap_td) / "snap.db-wal"
                copied_ok = False
                with db_path.open("rb") as src, snap_db.open("wb") as dst:
                    copied_ok = _copy_exact_cap(src, dst, db_expected)
                if copied_ok and wal_expected > 0:
                    with wal_path.open("rb") as src, snap_wal.open("wb") as dst:
                        copied_ok = _copy_exact_cap(src, dst, wal_expected)
                if not copied_ok:
                    if attempt + 1 < MAX_DB_SNAPSHOT_RETRIES:
                        time.sleep(DB_SNAPSHOT_RETRY_DELAY_SEC)
                        continue
                    return {}, {}

                after_status, after_sig, _ = _snapshot_db_signature(db_path)
                if after_status != "OK" or after_sig != before_sig:
                    if attempt + 1 < MAX_DB_SNAPSHOT_RETRIES:
                        time.sleep(DB_SNAPSHOT_RETRY_DELAY_SEC)
                        continue
                    return {}, {}

                try:
                    conn = sqlite3.connect(str(snap_db), timeout=0.5)
                except sqlite3.Error:
                    return {}, {}

                titles_map: dict[str, str] = {}
                parents_map: dict[str, str] = {}
                transient_err = False
                try:
                    try:
                        conn.execute("PRAGMA query_only = ON")
                        conn.execute("PRAGMA busy_timeout = 300")
                    except sqlite3.Error:
                        pass

                    try:
                        cur = conn.execute(
                            "SELECT conversation_id, title FROM conversation_summaries WHERE title IS NOT NULL AND title != ''"
                        )
                        for cid_val, title_val in cur:
                            cid_str = str(cid_val or "").strip()
                            clean_title = sanitize_conversation_name(title_val)
                            if cid_str and clean_title:
                                titles_map[cid_str] = clean_title
                    except sqlite3.Error as exc:
                        msg = str(exc).lower()
                        if "no such table" not in msg and "no such column" not in msg:
                            transient_err = True

                    try:
                        cur = conn.execute(
                            "SELECT conversation_id, parent_id FROM conversation_parents WHERE parent_id IS NOT NULL AND parent_id != ''"
                        )
                        for cid_val, parent_val in cur:
                            cid_str = str(cid_val or "").strip()
                            parent_str = str(parent_val or "").strip()
                            if cid_str and parent_str:
                                parents_map[cid_str] = parent_str
                    except sqlite3.Error as exc:
                        msg = str(exc).lower()
                        if "no such table" not in msg and "no such column" not in msg:
                            transient_err = True
                finally:
                    conn.close()

                if not transient_err:
                    if len(_SUMMARIES_DB_CACHE) >= MAX_SUMMARIES_DB_CACHE:
                        _SUMMARIES_DB_CACHE.clear()
                    _SUMMARIES_DB_CACHE[before_sig] = (titles_map, parents_map)
                return titles_map, parents_map
        except (OSError, RuntimeError):
            if attempt + 1 < MAX_DB_SNAPSHOT_RETRIES:
                time.sleep(DB_SNAPSHOT_RETRY_DELAY_SEC)
                continue
            return {}, {}

    return {}, {}


def resolve_antigravity_native_title(state: dict[str, Any]) -> str | None:
    """Resolve the live Antigravity sidebar title read-only from local annotations/.pbtxt and conversation_summaries.db."""
    cid = str(state.get("conversationId") or "").strip()
    if not _is_safe_session_id_segment(cid):
        return None
    for app_dir in _resolve_session_app_data_dirs(state):
        title, _ = _read_pbtxt_annotation(app_dir / "annotations" / f"{cid}.pbtxt")
        if title:
            return title
        titles_map, parents_map = _load_summaries_db_maps(app_dir / "conversation_summaries.db")
        db_title = titles_map.get(cid)
        if db_title:
            return db_title
        parent_id = parents_map.get(cid)
        if parent_id and _is_safe_session_id_segment(parent_id):
            _, side_titles = _read_pbtxt_annotation(app_dir / "annotations" / f"{parent_id}.pbtxt")
            side_title = side_titles.get(cid)
            if side_title:
                return side_title
    return None


def resolve_session_conversation_name(state: dict[str, Any]) -> str | None:
    """Resolve display title cleanly with read-only Antigravity native title lookup and fallback."""
    direct = sanitize_conversation_name(state.get("conversationName"))
    source = str(state.get("conversationNameSource") or "")
    if direct and source == "host":
        return direct
    native = resolve_antigravity_native_title(state)
    if native:
        return native
    if direct and source != "derived_fallback":
        return direct
    from_first = _derive_from_stored_input(state.get("firstUserInput"))
    if from_first:
        return from_first
    from_tr = derive_conversation_name_from_transcript(state.get("transcriptPath"))
    if from_tr:
        return from_tr
    if direct:
        return direct
    return _derive_from_stored_input(state.get("lastUserInput"))


def safe_session_filename(cid: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", (cid or "default").strip())
    return clean or "default"


def session_path(cid: str) -> Path:
    return SESSIONS_DIR / f"{safe_session_filename(cid)}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON via a temporary sibling file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _stat_plan_fast(path: Path) -> os.stat_result | None:
    """Return stat metadata for an existing regular file, or None."""
    try:
        if not path.is_file():
            return None
        return path.stat()
    except OSError:
        return None


def compute_file_sha256(path: Path) -> tuple[str | None, int | None]:
    """Compute SHA-256 and mtime_ns for a local file if it exists."""
    try:
        if not path.is_file():
            return None, None
        st = path.stat()
        h = hashlib.sha256()
        with path.open("rb") as fh:
            data = fh.read(MAX_HASH_FILE_BYTES)
            h.update(data)
            if st.st_size > MAX_HASH_FILE_BYTES:
                h.update(f":{st.st_size}:{st.st_mtime_ns}".encode("utf-8"))
        return h.hexdigest(), st.st_mtime_ns
    except OSError:
        return None, None


def resolve_plan_path(
    raw_path: str | None,
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
) -> Path | None:
    """Resolve an absolute or relative artifact/workspace file path."""
    if not raw_path:
        return None
    s = str(raw_path).strip()
    if not s:
        return None
    if s.startswith("file:///"):
        s = s[len("file://") :]
    p = Path(s).expanduser()
    if p.is_absolute():
        try:
            return p.resolve()
        except OSError:
            return p

    candidates: list[Path] = []
    if artifact_dir:
        candidates.append(Path(artifact_dir).expanduser() / p)
    for ws in workspace_paths or []:
        if ws:
            candidates.append(Path(ws).expanduser() / p)

    for cand in candidates:
        if cand.exists():
            try:
                return cand.resolve()
            except OSError:
                return cand

    if candidates:
        try:
            return candidates[0].resolve()
        except OSError:
            return candidates[0]
    try:
        return p.resolve()
    except OSError:
        return p


def build_plan_artifact_entry(
    raw_path: str | None,
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
    summary: str | None = None,
    request_feedback: bool = False,
) -> dict[str, Any] | None:
    resolved = resolve_plan_path(raw_path, workspace_paths, artifact_dir)
    if resolved is None:
        return None
    sha256, mtime_ns = compute_file_sha256(resolved)
    st = _stat_plan_fast(resolved)
    return {
        "path": str(resolved),
        "name": resolved.name,
        "sha256": sha256,
        "mtimeNs": mtime_ns,
        "sizeBytes": st.st_size if st is not None else None,
        "inode": st.st_ino if st is not None else None,
        "summary": (str(summary).strip() or None) if summary else None,
        "requestFeedback": bool(request_feedback),
        "stale": False,
        "staleReason": None,
    }


def default_session_state(cid: str = "default") -> dict[str, Any]:
    now = time.time()
    clean_cid = (cid or "default").strip() or "default"
    return {
        "conversationId": clean_cid,
        "shortId": clean_cid[:8],
        "conversationName": "",
        "conversationNameSource": "",
        "firstUserInput": "",
        "workspacePaths": [],
        "artifactDirectoryPath": "",
        "transcriptPath": "",
        "modelName": "",
        "lastUserInput": "",
        "turnUserInput": "",
        "userStepIndex": -1,
        "turnActive": False,
        "lastInvocationNum": -1,
        "lastTurnEndInvocationNum": -1,
        "invocationNum": 0,
        "stepIdx": 0,
        "lifecycle": "IDLE",
        "sourceType": "NONE",
        "confidence": "DETERMINISTIC",
        "waitingOnUser": False,
        "actor": "none",
        "nextAction": None,
        "detail": None,
        "suggestedPrompt": None,
        "options": [],
        "questions": [],
        "activeTool": None,
        "planArtifact": None,
        "pendingPlanReview": None,
        "pendingDeclaration": None,
        "fullyIdle": True,
        "terminationReason": None,
        "stale": False,
        "staleReason": None,
        "updatedAt": now,
        "updatedAtIso": iso_now(now),
    }


def load_session(cid: str) -> dict[str, Any]:
    ensure_data_dirs()
    p = session_path(cid)
    base = default_session_state(cid)
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                base.update(raw)
                base["conversationId"] = (str(raw.get("conversationId") or cid).strip() or "default")
                base["shortId"] = base["conversationId"][:8]
        except Exception:
            pass
    return base


def update_common_metadata(state: dict[str, Any], payload: dict[str, Any]) -> None:
    cid = str(payload.get("conversationId") or state.get("conversationId") or "default").strip() or "default"
    state["conversationId"] = cid
    state["shortId"] = cid[:8]

    ws = payload.get("workspacePaths")
    if isinstance(ws, list) and ws:
        state["workspacePaths"] = [str(x) for x in ws if x]
    elif payload.get("cwd") and not state.get("workspacePaths"):
        state["workspacePaths"] = [str(payload["cwd"])]

    if payload.get("artifactDirectoryPath"):
        state["artifactDirectoryPath"] = str(payload["artifactDirectoryPath"])
    if payload.get("transcriptPath"):
        state["transcriptPath"] = str(payload["transcriptPath"])
    if payload.get("modelName"):
        state["modelName"] = str(payload["modelName"])

    for key in HOST_TITLE_KEYS:
        host_val = sanitize_conversation_name(payload.get(key))
        if host_val:
            _apply_conversation_name(state, host_val, "host")
            break

    pre_source = str(state.get("conversationNameSource") or "")
    has_prior_legacy_fallback = pre_source == "derived_fallback" or (
        not state.get("firstUserInput")
        and bool(_derive_from_stored_input(state.get("lastUserInput")))
    )
    tried_current_raw_empty = False
    if payload.get("lastUserInput"):
        sanitized = sanitize_user_input(payload["lastUserInput"])
        if sanitized:
            state["lastUserInput"] = sanitized
        if not state.get("firstUserInput"):
            tr_first = extract_first_user_input_from_transcript(state.get("transcriptPath"))
            if tr_first:
                tr_sanitized = sanitize_user_input(tr_first, unwrap_xml=False)
                tr_derived = derive_conversation_name(tr_first, unwrap_xml=False)
                if tr_sanitized and tr_derived:
                    state["firstUserInput"] = tr_sanitized
                _apply_conversation_name(state, tr_derived, "derived")
            elif not has_prior_legacy_fallback:
                raw_derived = derive_conversation_name(payload["lastUserInput"])
                if sanitized and raw_derived:
                    state["firstUserInput"] = sanitized
                elif raw_derived is None:
                    tried_current_raw_empty = True
                _apply_conversation_name(state, raw_derived, "derived")

    cur_source = str(state.get("conversationNameSource") or "")
    if cur_source not in ("host", "explicit") and (
        cur_source == "derived_fallback" or not str(state.get("conversationName") or "").strip()
    ):
        strong_name = _derive_from_stored_input(state.get("firstUserInput"))
        if not strong_name and not state.get("firstUserInput"):
            tr_first_fb = extract_first_user_input_from_transcript(state.get("transcriptPath"))
            if tr_first_fb:
                tr_san_fb = sanitize_user_input(tr_first_fb, unwrap_xml=False)
                tr_der_fb = derive_conversation_name(tr_first_fb, unwrap_xml=False)
                if tr_san_fb and tr_der_fb:
                    state["firstUserInput"] = tr_san_fb
                strong_name = tr_der_fb
        if not strong_name:
            strong_name = derive_conversation_name_from_transcript(state.get("transcriptPath"))
        if strong_name:
            _apply_conversation_name(state, strong_name, "derived")
        elif not tried_current_raw_empty and not str(state.get("conversationName") or "").strip():
            last_fallback = _derive_from_stored_input(state.get("lastUserInput"))
            if last_fallback:
                _apply_conversation_name(state, last_fallback, "derived_fallback")
    if payload.get("invocationNum") is not None:
        try:
            state["invocationNum"] = int(payload["invocationNum"])
        except (ValueError, TypeError):
            pass
    if payload.get("stepIdx") is not None:
        try:
            state["stepIdx"] = int(payload["stepIdx"])
        except (ValueError, TypeError):
            pass


def evaluate_staleness(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Return a copy of session state with live staleness and tool approval timeout checks applied."""
    cur_now = time.time() if now is None else now
    out = copy.deepcopy(state)

    plan = out.get("planArtifact")
    if isinstance(plan, dict) and plan.get("path") and plan.get("sha256"):
        plan_file = Path(str(plan["path"]))
        st = _stat_plan_fast(plan_file)
        if st is None:
            disk_sha, disk_mtime = None, None
        elif (
            plan.get("mtimeNs") == st.st_mtime_ns
            and plan.get("sizeBytes") == st.st_size
            and plan.get("inode") == st.st_ino
            and (time.time_ns() - st.st_mtime_ns) > PLAN_STAT_SETTLE_NS
        ):
            disk_sha, disk_mtime = plan.get("sha256"), st.st_mtime_ns
        else:
            disk_sha, disk_mtime = compute_file_sha256(plan_file)
        if disk_sha != plan.get("sha256"):
            plan["stale"] = True
            plan["staleReason"] = "关联计划文件已改写或移除"
            plan["currentSha256"] = disk_sha
            plan["currentMtimeNs"] = disk_mtime
            if (
                out.get("sourceType") == "AGENT_DECLARED"
                or out.get("lifecycle") == "WAITING_PLAN_REVIEW"
                or isinstance(out.get("pendingDeclaration"), dict)
                or isinstance(out.get("pendingPlanReview"), dict)
                or bool(out.get("suggestedPrompt"))
            ):
                out["stale"] = True
                out["staleReason"] = "关联计划文件已改写，当前下一步提示可能已过期"

    lifecycle = str(out.get("lifecycle") or "IDLE")
    updated_at = float(out.get("updatedAt") or cur_now)
    elapsed = max(0.0, cur_now - updated_at)

    if lifecycle in ("WORKING", "TOOL_RUNNING") and elapsed > WATCHDOG_TIMEOUT_SEC:
        out["stale"] = True
        out["staleReason"] = (
            f"运行态已超过 {int(elapsed)} 秒未更新，可能正在等待宿主权限弹窗确认或会话已中断"
        )

    active_tool = out.get("activeTool")
    if lifecycle == "TOOL_RUNNING" and isinstance(active_tool, dict):
        started_at = float(active_tool.get("startedAt") or updated_at)
        tool_elapsed = max(0.0, cur_now - started_at)
        active_tool["elapsedSec"] = round(tool_elapsed, 1)
        if active_tool.get("mayWaitApproval") and tool_elapsed >= TOOL_APPROVAL_HINT_SEC:
            active_tool["approvalHint"] = "工具耗时超 8 秒，若宿主弹出了权限确认框请先处理"

    return out


def save_session(cid: str, state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    ensure_data_dirs()
    cur_now = time.time() if now is None else now
    clean_cid = (cid or str(state.get("conversationId") or "default")).strip() or "default"
    state["conversationId"] = clean_cid
    state["shortId"] = clean_cid[:8]
    state["updatedAt"] = cur_now
    state["updatedAtIso"] = iso_now(cur_now)
    atomic_write_json(session_path(clean_cid), state)
    return state


def normalize_declaration(
    args: dict[str, Any],
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
    step_idx: int | None = None,
) -> dict[str, Any] | None:
    """Validate and normalize a next_step_publish payload."""
    next_action = str(clean_arg(args.get("next_action") or args.get("nextAction") or "")).strip()
    if not next_action:
        return None

    raw_actor = str(clean_arg(args.get("actor") or "user")).strip().lower()
    if raw_actor not in ("user", "agent", "agent_background", "none"):
        raw_actor = "user"

    raw_waiting = clean_arg(args.get("waiting_on_user"))
    if raw_waiting is None:
        raw_waiting = clean_arg(args.get("waitingOnUser"))
    if isinstance(raw_waiting, bool):
        waiting_on_user = raw_waiting
    elif isinstance(raw_waiting, str):
        waiting_on_user = raw_waiting.strip().lower() not in ("false", "0", "no")
    else:
        waiting_on_user = raw_actor == "user"

    suggested_prompt_raw = clean_arg(args.get("suggested_prompt") or args.get("suggestedPrompt") or "")
    suggested_prompt = str(suggested_prompt_raw).strip() or None

    detail_raw = clean_arg(args.get("detail") or "")
    detail = str(detail_raw).strip() or None

    plan_path_raw = clean_arg(args.get("plan_path") or args.get("planPath") or "")
    plan_entry = None
    if isinstance(plan_path_raw, str) and plan_path_raw.strip():
        plan_entry = build_plan_artifact_entry(
            plan_path_raw.strip(),
            workspace_paths=workspace_paths,
            artifact_dir=artifact_dir,
        )

    conv_name_raw = clean_arg(args.get("conversation_name") or args.get("conversationName") or "")
    conversation_name = sanitize_conversation_name(conv_name_raw)

    now = time.time()
    return {
        "next_action": next_action,
        "actor": raw_actor,
        "waiting_on_user": waiting_on_user,
        "suggested_prompt": suggested_prompt,
        "detail": detail,
        "planArtifact": plan_entry,
        "conversation_name": conversation_name,
        "stepIdx": int(step_idx or 0),
        "createdAt": now,
        "createdAtIso": iso_now(now),
    }


def stage_declaration(
    args: dict[str, Any],
    cid: str | None = None,
    workspace_paths: list[str] | None = None,
    artifact_dir: str | None = None,
    step_idx: int | None = None,
    write_pending_buffer: bool = True,
    state: dict[str, Any] | None = None,
    commit_conversation_name: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Stage a normalized next-step declaration into session state and/or pending buffer."""
    ensure_data_dirs()
    eff_cid = (
        str(
            cid
            or clean_arg(args.get("_sys_conversation_id"))
            or clean_arg(args.get("conversation_id"))
            or clean_arg(args.get("conversationId"))
            or ""
        ).strip()
        or None
    )
    eff_ws = workspace_paths
    if not eff_ws:
        sys_ws = clean_arg(args.get("_sys_workspace_paths"))
        if isinstance(sys_ws, list):
            eff_ws = [str(x) for x in sys_ws if x]
    eff_artifact_dir = artifact_dir or str(clean_arg(args.get("_sys_artifact_dir") or "")).strip() or None
    eff_step_idx = step_idx
    if eff_step_idx is None and args.get("_sys_step_idx") is not None:
        try:
            eff_step_idx = int(clean_arg(args.get("_sys_step_idx")))
        except (ValueError, TypeError):
            eff_step_idx = 0

    state = state if state is not None else (load_session(eff_cid) if eff_cid else None)
    if state is not None:
        if not eff_ws:
            eff_ws = state.get("workspacePaths") or []
        if not eff_artifact_dir:
            eff_artifact_dir = state.get("artifactDirectoryPath") or None

    decl = normalize_declaration(
        args,
        workspace_paths=eff_ws,
        artifact_dir=eff_artifact_dir,
        step_idx=eff_step_idx,
    )
    if decl is None:
        return None, state

    if write_pending_buffer and eff_cid:
        pending_payload = {
            "declaration": decl,
            "conversationId": eff_cid,
            "workspacePaths": eff_ws or [],
            "createdAt": decl["createdAt"],
            "claimedBy": eff_cid,
            "consumed": bool(state is not None),
        }
        atomic_write_json(LATEST_PENDING_PATH, pending_payload)

    if state is not None and eff_cid:
        if commit_conversation_name and decl.get("conversation_name"):
            _apply_conversation_name(state, decl["conversation_name"], "explicit")
        state["pendingDeclaration"] = decl
        state["sourceType"] = "AGENT_DECLARED"
        state["confidence"] = "HIGH"
        state["waitingOnUser"] = bool(decl.get("waiting_on_user", True))
        state["actor"] = str(decl.get("actor") or ("user" if state["waitingOnUser"] else "agent"))
        state["nextAction"] = str(decl.get("next_action") or "").strip() or None
        state["detail"] = decl.get("detail")
        state["suggestedPrompt"] = decl.get("suggested_prompt")
        state["options"] = []
        state["questions"] = []
        state["planArtifact"] = decl.get("planArtifact")
        state["stale"] = False
        state["staleReason"] = None
        save_session(eff_cid, state)

    return decl, state


def clear_pending_buffer(cid: str | None = None) -> None:
    """Clear the pending declaration buffer if unattributed or belonging to cid."""
    if not LATEST_PENDING_PATH.is_file():
        return
    try:
        if cid is not None:
            raw = json.loads(LATEST_PENDING_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                owner = raw.get("conversationId")
                if owner and owner != cid:
                    return
        LATEST_PENDING_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def claim_latest_pending_declaration(
    cid: str,
    max_age_sec: float = 30.0,
    min_created_at: float = 0.0,
) -> dict[str, Any] | None:
    """Claim an unconsumed pending declaration strictly matching cid within max_age_sec."""
    if not cid or not LATEST_PENDING_PATH.is_file():
        return None
    try:
        raw = json.loads(LATEST_PENDING_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        if raw.get("consumed") is True:
            return None
        target_cid = str(raw.get("conversationId") or "").strip()
        if not target_cid or target_cid != cid:
            return None
        created_at = float(raw.get("createdAt") or 0.0)
        if created_at < min_created_at:
            return None
        if time.time() - created_at > max_age_sec:
            return None
        decl = raw.get("declaration")
        if not isinstance(decl, dict):
            return None
        raw["claimedBy"] = cid
        raw["conversationId"] = cid
        raw["consumed"] = True
        atomic_write_json(LATEST_PENDING_PATH, raw)
        return decl
    except Exception:
        return None


def promote_plan_review_to_active(state: dict[str, Any], plan_review: dict[str, Any]) -> None:
    """Restore or promote a pending RequestFeedback plan review to active WAITING_PLAN_REVIEW state."""
    state["lifecycle"] = "WAITING_PLAN_REVIEW"
    state["sourceType"] = "APPROVAL_REQUIRED"
    state["confidence"] = "DETERMINISTIC"
    state["waitingOnUser"] = True
    state["actor"] = "user"
    state["nextAction"] = str(plan_review.get("nextAction") or "审阅计划工件并点击 Proceed 或提交评论")
    state["detail"] = plan_review.get("detail")
    state["options"] = []
    state["questions"] = []
    state["suggestedPrompt"] = None
    if isinstance(plan_review.get("planArtifact"), dict):
        state["planArtifact"] = plan_review["planArtifact"]
    state["stale"] = False
    state["staleReason"] = None
    evaluated = evaluate_staleness(state)
    state["stale"] = bool(evaluated.get("stale", False))
    state["staleReason"] = evaluated.get("staleReason")
    if isinstance(evaluated.get("planArtifact"), dict):
        state["planArtifact"] = evaluated["planArtifact"]


def promote_declaration_to_active(state: dict[str, Any], decl: dict[str, Any], fully_idle: bool = True) -> None:
    """Promote a staged declaration to the session's active AGENT_DECLARED next step."""
    if decl.get("conversation_name"):
        _apply_conversation_name(state, decl["conversation_name"], "explicit")
    waiting = bool(decl.get("waiting_on_user", True))
    actor = str(decl.get("actor") or ("user" if waiting else "agent"))
    state["lifecycle"] = "BACKGROUND_RUNNING" if not fully_idle else "IDLE"
    state["sourceType"] = "AGENT_DECLARED"
    state["confidence"] = "HIGH"
    state["waitingOnUser"] = waiting
    state["actor"] = actor
    state["nextAction"] = str(decl.get("next_action") or "").strip() or None
    state["detail"] = decl.get("detail")
    state["suggestedPrompt"] = decl.get("suggested_prompt")
    state["options"] = []
    state["questions"] = []
    state["planArtifact"] = (
        copy.deepcopy(decl.get("planArtifact"))
        if isinstance(decl.get("planArtifact"), dict)
        else None
    )
    state["pendingDeclaration"] = decl
    state["stale"] = False
    state["staleReason"] = None
    evaluated = evaluate_staleness(state)
    state["stale"] = bool(evaluated.get("stale", False))
    state["staleReason"] = evaluated.get("staleReason")
    if isinstance(evaluated.get("planArtifact"), dict):
        state["planArtifact"] = evaluated["planArtifact"]


def normalize_workspace_path(raw: str | None) -> str:
    """Return a canonical workspace key string or empty string if blank."""
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        return str(Path(s).expanduser().resolve())
    except OSError:
        return s.rstrip("/") or s


def _normalize_workspace_filter(raw: str | None) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s == "__none__":
        return "__none__"
    return normalize_workspace_path(s) or None


def _session_matches_workspace(raw: dict[str, Any], norm_ws: str | None) -> bool:
    if not norm_ws:
        return True
    ws_list = [str(x) for x in (raw.get("workspacePaths") or []) if x]
    if norm_ws == "__none__":
        return len(ws_list) == 0
    primary_ws = normalize_workspace_path(ws_list[0]) if ws_list else ""
    return bool(primary_ws) and primary_ws == norm_ws


def list_all_sessions(
    workspace_filter: str | None = None,
    now: float | None = None,
    max_sessions: int = MAX_SCANNED_SESSIONS,
) -> list[dict[str, Any]]:
    """Load, evaluate staleness, filter, and sort recent recorded sessions (capped at MAX_SCANNED_SESSIONS reads)."""
    ensure_data_dirs()
    cur_now = time.time() if now is None else now
    norm_ws = _normalize_workspace_filter(workspace_filter)

    candidates: list[tuple[int, str]] = []
    if SESSIONS_DIR.is_dir():
        with os.scandir(SESSIONS_DIR) as it:
            for entry in it:
                if not entry.name.endswith(".json") or entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
                    candidates.append((st.st_mtime_ns, entry.path))
                except OSError:
                    continue

    candidates.sort(key=lambda item: item[0], reverse=True)
    cap = min(MAX_SCANNED_SESSIONS, max(1, int(max_sessions)))

    results: list[dict[str, Any]] = []
    for _, path_str in candidates[:cap]:
        try:
            raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
        except Exception:
            continue

        if not _session_matches_workspace(raw, norm_ws):
            continue

        results.append(evaluate_staleness(raw, now=cur_now))

    results.sort(
        key=lambda s: (
            0 if s.get("waitingOnUser") else 1,
            -float(s.get("updatedAt") or 0.0),
            str(s.get("conversationId") or ""),
        )
    )
    return results


def format_actor_label(actor: str | None, waiting_on_user: bool = False) -> str:
    a = (actor or "").strip().lower()
    if a == "user":
        return "用户"
    if a == "agent":
        return "Agent（主会话）"
    if a == "agent_background":
        return "Agent（后台任务 / 子 Agent）"
    if waiting_on_user:
        return "用户"
    return "—"


def format_source_label(source_type: str | None, confidence: str | None = None) -> str:
    st = (source_type or "").strip().upper()
    if st == "APPROVAL_REQUIRED":
        return "系统待办 / 审批拦截（确定性事实）"
    if st == "AGENT_DECLARED":
        return "Agent 显式声明（高可信）"
    if st == "LIFECYCLE":
        return "生命周期运行态（系统事实）"
    return "Stop 终止态核实（无已声明下一步）"


def format_status_badge(state: dict[str, Any]) -> str:
    if state.get("stale"):
        return "🟡 状态可能已过期 (Stale)"
    if state.get("waitingOnUser"):
        lifecycle = str(state.get("lifecycle") or "")
        if lifecycle == "WAITING_PLAN_REVIEW":
            return "🔴 等我审阅计划 (Waiting Plan Review)"
        if lifecycle == "WAITING_USER_DECISION":
            return "🔴 等我回答/选择 (Waiting User Decision)"
        if lifecycle == "WAITING_USER_APPROVAL":
            return "🔴 等我授权 (Waiting Permission)"
        if lifecycle in ("ERROR", "CANCELED"):
            return f"🔴 已中止 / 待介入 ({lifecycle})"
        return "🔴 等我确认 / 发出指令 (Waiting on You)"
    lifecycle = str(state.get("lifecycle") or "IDLE")
    if lifecycle in ("WORKING", "TOOL_RUNNING"):
        return f"🟢 Agent 运行中 ({lifecycle})"
    if lifecycle == "BACKGROUND_RUNNING":
        return "🟢 后台任务运行中 (Background Active)"
    return "⚪ 会话空闲（无挂起待办）"


def _normalize_questions(raw_questions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_questions, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            continue
        q_title = str(item.get("question") or "").strip()
        if not q_title:
            continue
        raw_opts = item.get("options") or []
        opts = [
            str(opt).strip()
            for opt in raw_opts
            if opt is not None and str(opt).strip()
        ]
        out.append(
            {
                "question": q_title,
                "options": opts,
                "multiSelect": bool(item.get("multiSelect", False)),
            }
        )
    return out


def _render_session_markdown_block(
    *,
    cid: str,
    short_id: str,
    ws_name: str,
    conversation_name: str | None = None,
    last_input: str | None,
    badge: str,
    actor_label: str,
    next_action: str,
    source_label: str,
    updated_at_iso: str,
    detail: str | None = None,
    stale: bool = False,
    stale_reason: str | None = None,
    approval_hint: str | None = None,
    questions: list[dict[str, Any]] | None = None,
    options: list[str] | None = None,
    plan: dict[str, Any] | None = None,
    suggested_prompt: str | None = None,
    copy_blocked: bool = False,
) -> str:
    heading = (
        f"### {conversation_name} · `{ws_name}` (`#{short_id}`)"
        if conversation_name
        else f"### 会话 `#{short_id}` · `{ws_name}`"
    )
    lines = [
        heading,
        f"- **完整会话 ID**：`{cid}`",
        f"- **最近用户输入**：{last_input or '（尚无用户输入记录）'}",
        f"- **是否在等我**：`{badge}`",
        f"- **由谁做**：`{actor_label}`",
        f"- **接下来要做什么**：**{next_action}**",
        f"- **来源与可信度**：`{source_label}`",
        f"- **最后更新时间**：`{updated_at_iso}`",
    ]
    if detail:
        lines.append(f"- **补充说明**：{detail}")
    if stale and stale_reason:
        lines.append(f"- **过期提示**：`🟡 {stale_reason}`")
    if approval_hint:
        lines.append(f"- **权限弹窗提示**：`🟡 {approval_hint}`")
    q_list = questions or []
    if len(q_list) > 1:
        lines.append("- **待选条目（多问题）**：")
        for q_idx, q_item in enumerate(q_list, 1):
            lines.append(f"  {q_idx}. **{q_item['question']}**")
            for opt in q_item.get("options") or []:
                lines.append(f"     - `{opt}`")
    elif options:
        lines.append("- **待选条目**：")
        for idx, opt in enumerate(options, 1):
            lines.append(f"  {idx}. `{opt}`")
    if isinstance(plan, dict) and plan.get("path"):
        plan_status = "🟡 已过期（文件已改写）" if plan.get("stale") else "未过期"
        lines.append(f"- **关联计划工件**：`{plan.get('name') or plan['path']}`（{plan_status}）")
    if suggested_prompt and not copy_blocked:
        lines.append(
            f"- **建议下一条指令（只读复制，由你决定是否提交）**：\n  ```text\n  {suggested_prompt}\n  ```"
        )
    elif suggested_prompt and copy_blocked:
        lines.append("- **建议下一条指令**：`🟡 已过期，已阻止直接复制`")
    return "\n".join(lines)


def format_session_markdown(state: dict[str, Any]) -> str:
    """Format a single evaluated session state into concise Markdown."""
    ev = evaluate_staleness(state)
    cid = str(ev.get("conversationId") or "default")
    short_id = str(ev.get("shortId") or cid[:8])
    ws_list = ev.get("workspacePaths") or []
    ws_name = Path(ws_list[0]).name if ws_list else "default-workspace"

    next_action = ev.get("nextAction")
    if not next_action:
        if ev.get("pendingDeclaration"):
            next_action = f"[本轮已暂存声明] {ev['pendingDeclaration'].get('next_action')}"
        else:
            next_action = "当前无已声明的下一步（上一轮已正常结束，等待你输入新指令）"

    plan = ev.get("planArtifact") if isinstance(ev.get("planArtifact"), dict) else None
    active_tool = ev.get("activeTool") if isinstance(ev.get("activeTool"), dict) else None
    copy_blocked = bool(ev.get("stale") or (isinstance(plan, dict) and plan.get("stale")))

    return _render_session_markdown_block(
        cid=cid,
        short_id=short_id,
        ws_name=ws_name,
        conversation_name=resolve_session_conversation_name(ev),
        last_input=ev.get("lastUserInput"),
        badge=format_status_badge(ev),
        actor_label=format_actor_label(ev.get("actor"), bool(ev.get("waitingOnUser"))),
        next_action=next_action,
        source_label=format_source_label(ev.get("sourceType"), ev.get("confidence")),
        updated_at_iso=str(ev.get("updatedAtIso") or ""),
        detail=ev.get("detail"),
        stale=bool(ev.get("stale")),
        stale_reason=ev.get("staleReason"),
        approval_hint=active_tool.get("approvalHint") if active_tool else None,
        questions=ev.get("questions") or [],
        options=ev.get("options") or [],
        plan=plan,
        suggested_prompt=ev.get("suggestedPrompt"),
        copy_blocked=copy_blocked,
    )


def _clip_str(val: Any, max_len: int) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)] + "…"


def _build_session_common(state: dict[str, Any], now: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate staleness once and assemble non-sensitive overview fields for summary/detail builders."""
    ev = evaluate_staleness(state, now=now)
    cid = str(ev.get("conversationId") or "default")
    short_id = str(ev.get("shortId") or cid[:8])
    ws_list = [str(w) for w in (ev.get("workspacePaths") or []) if w]
    ws_key = normalize_workspace_path(ws_list[0]) if ws_list else "__none__"
    if ws_list:
        ws_name = Path(ws_key).name or ws_key
    else:
        ws_name = "未关联工作区"

    plan = ev.get("planArtifact")
    is_stale = bool(ev.get("stale", False))
    copy_blocked = bool(is_stale or (isinstance(plan, dict) and plan.get("stale")))
    waiting_on_user = bool(ev.get("waitingOnUser", False))
    actor = str(ev.get("actor") or "none")
    conv_name = resolve_session_conversation_name(ev)

    summary = {
        "conversationId": cid,
        "shortId": short_id,
        "conversationName": conv_name,
        "workspaceName": ws_name,
        "workspaceKey": ws_key,
        "workspacePaths": ws_list[:4],
        "lastUserInput": _clip_str(ev.get("lastUserInput"), 240),
        "lifecycle": str(ev.get("lifecycle") or "IDLE"),
        "sourceType": str(ev.get("sourceType") or "NONE"),
        "confidence": str(ev.get("confidence") or "DETERMINISTIC"),
        "waitingOnUser": waiting_on_user,
        "actor": actor,
        "actorLabel": format_actor_label(actor, waiting_on_user),
        "statusBadge": format_status_badge(ev),
        "sourceLabel": format_source_label(ev.get("sourceType"), ev.get("confidence")),
        "nextAction": _clip_str(ev.get("nextAction"), 500),
        "copyBlocked": copy_blocked,
        "stale": is_stale,
        "updatedAt": ev.get("updatedAt"),
        "updatedAtIso": ev.get("updatedAtIso") or "",
    }
    return ev, summary


def build_session_summary_item(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Build overview-only session item for list/filter rendering without leaking another session's prompts or options."""
    _, summary = _build_session_common(state, now=now)
    return summary


def build_session_panel_item(state: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Build a strictly read-only detailed view model for the bound session."""
    ev, base = _build_session_common(state, now=now)

    plan = ev.get("planArtifact")
    plan_item: dict[str, Any] | None = None
    if isinstance(plan, dict) and plan.get("path"):
        plan_item = {
            "path": _clip_str(plan.get("path"), 500),
            "name": _clip_str(plan.get("name") or Path(str(plan["path"])).name, 160),
            "sha256": plan.get("sha256"),
            "stale": bool(plan.get("stale", False)),
            "staleReason": _clip_str(plan.get("staleReason"), 200),
        }

    active_tool = ev.get("activeTool")
    tool_item: dict[str, Any] | None = None
    if isinstance(active_tool, dict) and active_tool.get("name"):
        tool_item = {
            "name": _clip_str(active_tool.get("name"), 120),
            "summary": _clip_str(active_tool.get("summary"), 240),
            "approvalHint": _clip_str(active_tool.get("approvalHint"), 240),
            "elapsedSec": active_tool.get("elapsedSec"),
        }

    raw_opts = ev.get("options") or []
    options = [
        str(opt).strip()
        for opt in raw_opts
        if opt is not None and str(opt).strip()
    ]
    questions = _normalize_questions(ev.get("questions"))

    raw_prompt = ev.get("suggestedPrompt")
    suggested_prompt = (
        str(raw_prompt).strip() or None
        if raw_prompt is not None
        else None
    )
    base.update(
        {
            "modelName": _clip_str(ev.get("modelName"), 120),
            "detail": _clip_str(ev.get("detail"), 1500),
            "options": options,
            "questions": questions,
            "suggestedPrompt": suggested_prompt,
            "staleReason": _clip_str(ev.get("staleReason"), 240),
            "planArtifact": plan_item,
            "activeTool": tool_item,
        }
    )
    return base


def resolve_target_session(
    clean_cid: str,
    *,
    allow_prefix: bool = True,
    workspace_filter: str | None = None,
) -> tuple[dict[str, Any] | None, bool, bool]:
    """Resolve exact session ID or (when allow_prefix=True) unique filename-scanned prefix. Returns (raw_state, ambiguous, not_found)."""
    norm_ws = _normalize_workspace_filter(workspace_filter)
    sp = session_path(clean_cid)
    if sp.is_file():
        try:
            raw = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            raw = None
        if isinstance(raw, dict) and str(raw.get("conversationId") or "").strip() == clean_cid:
            if _session_matches_workspace(raw, norm_ws):
                return raw, False, False
            return None, False, True

    if not allow_prefix:
        return None, False, True

    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", clean_cid)
    matched_paths: list[Path] = []
    if SESSIONS_DIR.is_dir():
        with os.scandir(SESSIONS_DIR) as it:
            for entry in it:
                if not entry.name.endswith(".json") or entry.name.startswith("."):
                    continue
                if entry.name[:-5].startswith(safe_prefix):
                    matched_paths.append(Path(entry.path))

    if not matched_paths:
        return None, False, True

    confirmed: list[dict[str, Any]] = []
    unverified = False
    reads = 0
    for mp in matched_paths:
        if reads >= 2:
            unverified = True
            break
        reads += 1
        try:
            raw = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            unverified = True
            continue
        if not isinstance(raw, dict):
            unverified = True
            continue
        if str(raw.get("conversationId") or "").startswith(clean_cid) and _session_matches_workspace(raw, norm_ws):
            confirmed.append(raw)
            if len(confirmed) > 1:
                break

    if len(confirmed) > 1 or unverified:
        return None, True, False
    if len(confirmed) == 1:
        return confirmed[0], False, False
    return None, False, True


def _build_workspace_options(
    session_items: list[dict[str, Any]],
    active_filter: str | None = None,
) -> list[dict[str, str]]:
    """Build canonical path-keyed workspace filter options with basename disambiguation."""
    key_to_name: dict[str, str] = {}
    for item in session_items:
        wk = str(item.get("workspaceKey") or "__none__")
        if wk not in key_to_name:
            key_to_name[wk] = str(item.get("workspaceName") or ("未关联工作区" if wk == "__none__" else Path(wk).name or wk))

    if active_filter and active_filter not in key_to_name:
        key_to_name[active_filter] = "未关联工作区" if active_filter == "__none__" else (Path(active_filter).name or active_filter)

    real_keys = [k for k in key_to_name if k != "__none__"]
    name_counts = Counter(key_to_name[k] for k in real_keys)

    options: list[dict[str, str]] = []
    for wk in sorted(real_keys, key=lambda k: (key_to_name[k].lower(), k)):
        base_name = key_to_name[wk]
        label = f"{base_name} ({wk})" if name_counts[base_name] > 1 else base_name
        options.append({"key": wk, "label": label})

    if "__none__" in key_to_name:
        options.append({"key": "__none__", "label": "未关联工作区"})
    return options


def build_side_panel_payload(
    bound_cid: str | None = None,
    binding_mode: str = "unbound",
    workspace_filter: str | None = None,
    limit: int = 30,
    now: float | None = None,
    allow_prefix: bool | None = None,
) -> dict[str, Any]:
    """Build strictly read-only, session-isolated payload for the MCP Apps Side Panel."""
    cur_now = time.time() if now is None else now
    clean_cid = str(bound_cid or "").strip() or None
    norm_ws = _normalize_workspace_filter(workspace_filter)
    eff_limit = min(MAX_SCANNED_SESSIONS, max(1, int(limit)))
    all_sessions = list_all_sessions(
        workspace_filter=norm_ws,
        now=cur_now,
        max_sessions=MAX_SCANNED_SESSIONS,
    )

    eff_mode = "unbound"
    resolved_cid: str | None = None
    selected_item: dict[str, Any] | None = None
    not_found = False
    ambiguous = False

    if clean_cid:
        eff_allow_prefix = (binding_mode != "hook_bound") if allow_prefix is None else bool(allow_prefix)
        raw_match, ambiguous, not_found = resolve_target_session(
            clean_cid,
            allow_prefix=eff_allow_prefix,
        )
        if raw_match is not None:
            selected_item = build_session_panel_item(raw_match, now=cur_now)
            resolved_cid = selected_item["conversationId"]
            eff_mode = binding_mode if binding_mode in ("hook_bound", "explicit") else "explicit"
        elif ambiguous:
            eff_mode = "unbound"
        else:
            resolved_cid = clean_cid
            eff_mode = binding_mode if binding_mode in ("hook_bound", "explicit") else "explicit"

    session_items = [build_session_summary_item(s, now=cur_now) for s in all_sessions[:eff_limit]]
    workspace_options = _build_workspace_options(session_items, active_filter=norm_ws)
    return {
        "version": "0.4.0",
        "generatedAtIso": iso_now(cur_now),
        "bindingMode": eff_mode,
        "boundConversationId": resolved_cid,
        "sessionNotFound": not_found,
        "ambiguousPrefix": ambiguous,
        "selectedSession": selected_item,
        "sessions": session_items,
        "workspaceFilter": norm_ws,
        "workspaceOptions": workspace_options,
        "workspaces": [opt["label"] for opt in workspace_options],
        "totalSessions": len(all_sessions),
        "waitingCount": sum(1 for s in all_sessions if s.get("waitingOnUser")),
    }


def format_side_panel_text(payload: dict[str, Any]) -> str:
    """Format concise plain-text Markdown fallback for non-UI MCP clients without re-reading disk."""
    mode = payload.get("bindingMode")
    cid = payload.get("boundConversationId")
    sessions = payload.get("sessions") or []

    if mode == "unbound":
        header = (
            "**Next Step HUD（未绑定当前会话）**："
            + (
                "传入的会话前缀匹配多个会话，已回退为未绑定状态。\n\n"
                if payload.get("ambiguousPrefix")
                else "未传入 `conversation_id` 且无宿主 Hook 绑定，以下列出已记录会话（不自动猜测当前会话）：\n\n"
            )
        )
        if not sessions:
            return header + "当前暂无已记录的会话状态。"
        lines = [header.rstrip()]
        for item in sessions[:10]:
            action = item.get("nextAction") or "当前无已声明的下一步"
            conv_name = str(item.get("conversationName") or "").strip()
            if conv_name:
                lines.append(
                    f"- **{conv_name}** (`#{item['shortId']}` · `{item['workspaceName']}`) · `{item['statusBadge']}` · {action}"
                )
            else:
                lines.append(
                    f"- `#{item['shortId']}` · `{item['workspaceName']}` · `{item['statusBadge']}` · {action}"
                )
        return "\n".join(lines)

    if payload.get("sessionNotFound"):
        return f"**Next Step HUD（已绑定会话 `{str(cid)[:8]}`）**：尚无该会话的状态记录（尚未触发 Hook 或发布下一步）。"

    sel = payload.get("selectedSession")
    if isinstance(sel, dict):
        active_tool = sel.get("activeTool") if isinstance(sel.get("activeTool"), dict) else None
        plan = sel.get("planArtifact") if isinstance(sel.get("planArtifact"), dict) else None
        return _render_session_markdown_block(
            cid=str(sel.get("conversationId") or cid or "default"),
            short_id=str(sel.get("shortId") or str(cid or "default")[:8]),
            ws_name=str(sel.get("workspaceName") or "default-workspace"),
            conversation_name=sel.get("conversationName"),
            last_input=sel.get("lastUserInput"),
            badge=str(sel.get("statusBadge") or ""),
            actor_label=str(sel.get("actorLabel") or "—"),
            next_action=str(sel.get("nextAction") or "当前无已声明的下一步（上一轮已正常结束，等待你输入新指令）"),
            source_label=str(sel.get("sourceLabel") or ""),
            updated_at_iso=str(sel.get("updatedAtIso") or ""),
            detail=sel.get("detail"),
            stale=bool(sel.get("stale")),
            stale_reason=sel.get("staleReason"),
            approval_hint=active_tool.get("approvalHint") if active_tool else None,
            questions=sel.get("questions") or [],
            options=sel.get("options") or [],
            plan=plan,
            suggested_prompt=sel.get("suggestedPrompt"),
            copy_blocked=bool(sel.get("copyBlocked")),
        )

    return "当前暂无已记录的会话状态。"
