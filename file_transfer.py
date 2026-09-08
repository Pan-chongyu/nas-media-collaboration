"""Native Windows file drag sources; only advertise the copy action."""
from __future__ import annotations

from pathlib import PureWindowsPath
import tkinter as tk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES, COPY
except ImportError:
    TkinterDnD, DND_FILES, COPY = None, "DND_Files", "copy"


class DragDropRoot(tk.Tk):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dnd_version = ""
        if TkinterDnD is not None:
            try:
                self.dnd_version = str(TkinterDnD._require(self))
            except (RuntimeError, tk.TclError):
                # Indexing and playback remain available if the native extension fails.
                pass


def file_drag_payload(paths):
    """Return a Tcl-safe tuple; no synchronous SMB stat or string interpolation."""
    paths = tuple(str(path) for path in paths)
    if not paths or any(not PureWindowsPath(path).is_absolute() or "\0" in path for path in paths):
        raise ValueError("拖拽素材需要完整文件路径")
    return (COPY, DND_FILES, paths)


def register_file_drag(widget, paths, *, started=None, finished=None):
    if not getattr(widget._root(), "dnd_version", ""):
        return False
    widget.drag_source_register(1, DND_FILES)

    def begin(event):
        values = paths(event)
        if not values:
            return ("refuse_drop", DND_FILES, ())
        payload = file_drag_payload(values)
        if started:
            started(values)
        return payload

    widget.dnd_bind("<<DragInitCmd>>", begin)
    if finished:
        widget.dnd_bind("<<DragEndCmd>>", lambda event: finished(event.action))
    return True
