"""Local clipboard helpers for the muse-relay scripts.

send.py --clip reads the clipboard; paste.py writes to it. Both go
through the same tool fallbacks, so the pair works across macOS, X11,
Wayland, Termux and Windows (PowerShell as last resort) with no
dependencies beyond the tools themselves.

Failure is LOUD, never a silent no-op: with no clipboard tool on PATH
you get a ClipboardError naming what to install; when tools exist but
fail (usually "no display"), the error lists every attempt.
"""
import shutil
import subprocess

# Preference order per the spec: xclip, xsel, pbcopy/pbpaste, wl-*,
# then Termux and PowerShell as fallbacks. Each entry is one command;
# the first binary found on PATH wins, and a runtime failure falls
# through to the next candidate.
READ_TOOLS = [
    ["xclip", "-selection", "clipboard", "-out"],
    ["xsel", "--clipboard", "--output"],
    ["pbpaste"],
    ["wl-paste", "--no-newline"],
    ["termux-clipboard-get"],
    ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
]
WRITE_TOOLS = [
    ["xclip", "-selection", "clipboard", "-in"],
    ["xsel", "--clipboard", "--input"],
    ["pbcopy"],
    ["wl-copy"],
    ["termux-clipboard-set"],
    ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
]

_TIMEOUT = 10  # seconds per tool attempt


class ClipboardError(Exception):
    """No clipboard tool, or every tool failed."""


def _installed(cmd):
    return shutil.which(cmd[0]) is not None


def _attempt(cmd, data=None):
    """Run one clipboard tool; returns (ok, stdout_bytes, note)."""
    try:
        p = subprocess.run(cmd, input=data, capture_output=True,
                           timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return False, b"", f"{cmd[0]}: {e}"
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()[:120]
        return False, b"", f"{cmd[0]}: rc={p.returncode}" + (f" {err}" if err else "")
    return True, p.stdout, cmd[0]


def clipboard_read(tools=None):
    """Return clipboard bytes via the first working tool.

    Raises ClipboardError when no tool is installed (the message names
    the whole fallback list and how to install one) or when every
    installed tool fails (the message lists each failure).
    """
    tools = READ_TOOLS if tools is None else tools
    attempts = []
    for cmd in tools:
        if not _installed(cmd):
            continue
        ok, out, note = _attempt(cmd)
        if ok:
            return out
        attempts.append(note)
    if not attempts:
        names = ", ".join(c[0] for c in tools)
        raise ClipboardError(
            f"no clipboard tool found (looked for: {names}) — install "
            "one, e.g. 'apt install xclip' (or wl-clipboard, xsel)")
    raise ClipboardError("clipboard read failed — " + "; ".join(attempts))


def clipboard_write(data, tools=None):
    """Put bytes on the local clipboard via the first working tool.

    Returns the name of the tool used. Raises ClipboardError under the
    same loud-failure rules as clipboard_read.
    """
    tools = WRITE_TOOLS if tools is None else tools
    attempts = []
    for cmd in tools:
        if not _installed(cmd):
            continue
        ok, _, note = _attempt(cmd, data=data)
        if ok:
            return note
        attempts.append(note)
    if not attempts:
        names = ", ".join(c[0] for c in tools)
        raise ClipboardError(
            f"no clipboard tool found (looked for: {names}) — install "
            "one, e.g. 'apt install xclip' (or wl-clipboard, xsel)")
    raise ClipboardError("clipboard write failed — " + "; ".join(attempts))
