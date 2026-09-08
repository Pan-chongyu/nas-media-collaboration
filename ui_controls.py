"""Small canvas controls shared by the desktop media preview."""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable


class TimelineScale(tk.Canvas):
    """A horizontal scale with a slim track and a circular playhead.

    ``command`` follows ttk.Scale's convention: it receives the current value
    as a string, and is called only for user input. Updating ``variable`` or
    calling ``set`` redraws the scale without seeking the player.

    Input handlers use a private bind tag, so callers can bind their own mouse
    press/release handlers without ``add='+'``. Widget handlers run before the
    scale's handlers; this lets a caller mark a seek gesture as active before
    the first command is emitted. The release handler only ends the gesture;
    clicking and dragging already update the value on press and motion.
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        variable: tk.DoubleVar | None = None,
        from_: float = 0,
        to: float = 100,
        command: Callable[[str], object] | None = None,
        width: int = 240,
        height: int = 26,
        bg: str = "#17212b",
        track: str = "#364450",
        accent: str = "#2dd4bf",
        **kwargs,
    ):
        self._minimum = float(from_)
        self._maximum = float(to)
        self._command = command
        self._track_color = track
        self._accent_color = accent
        self._states: set[str] = set()
        self._dragging = False
        self._trace_id: str | None = None
        self._variable = variable if variable is not None else tk.DoubleVar(master, self._minimum)
        initial_state = kwargs.pop("state", "normal")
        orient = kwargs.pop("orient", "horizontal")
        if orient != "horizontal":
            raise ValueError("TimelineScale supports horizontal orientation only")
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("borderwidth", 0)
        kwargs.setdefault("takefocus", True)
        kwargs.setdefault("cursor", "hand2")
        super().__init__(master, width=width, height=height, bg=bg, **kwargs)
        self._tag = f"TimelineScale:{self._w}"
        tags = self.bindtags()
        self.bindtags((tags[0], self._tag, *tags[1:]))
        self._bindings: list[tuple[str, str]] = []
        bindings = {
            "<Configure>": self._redraw,
            "<ButtonPress-1>": self._press,
            "<B1-Motion>": self._motion,
            "<ButtonRelease-1>": self._release,
            "<FocusIn>": self._focus_in,
            "<FocusOut>": self._focus_out,
            "<KeyPress-Left>": lambda event: self._step(-1),
            "<KeyPress-Right>": lambda event: self._step(1),
            "<KeyPress-Down>": lambda event: self._step(-1),
            "<KeyPress-Up>": lambda event: self._step(1),
            "<KeyPress-Prior>": lambda event: self._step(-10),
            "<KeyPress-Next>": lambda event: self._step(10),
            "<KeyPress-Home>": lambda event: self._keyboard_value(self._minimum),
            "<KeyPress-End>": lambda event: self._keyboard_value(self._maximum),
            "<Destroy>": self._destroyed,
        }
        for sequence, callback in bindings.items():
            binding_id = self.bind_class(self._tag, sequence, callback)
            self._bindings.append((sequence, binding_id))
        self._trace_id = self._variable.trace_add("write", self._redraw)
        self._track = self.create_line(0, 0, 0, 0, width=4, capstyle=tk.ROUND, fill=track)
        self._fill = self.create_line(0, 0, 0, 0, width=4, capstyle=tk.ROUND, fill=accent)
        self._focus_ring = self.create_oval(0, 0, 0, 0, width=1, outline=accent, state="hidden")
        self._thumb = self.create_oval(0, 0, 0, 0, width=0, fill=accent)
        self.state(["disabled"] if initial_state == "disabled" else [])
        self._redraw()

    def _clamp(self, value: float) -> float:
        value = float(value)
        if not math.isfinite(value):
            return self._minimum
        low, high = sorted((self._minimum, self._maximum))
        return max(low, min(high, value))

    def get(self) -> float:
        try:
            return self._clamp(self._variable.get())
        except (tk.TclError, TypeError, ValueError):
            return self._minimum

    def set(self, value: float) -> None:
        """Update a value programmatically, without invoking the command."""
        self._variable.set(self._clamp(value))

    def state(self, statespec=None):
        if statespec is None:
            return tuple(sorted(self._states))
        if isinstance(statespec, str):
            statespec = self.tk.splitlist(statespec)
        changed = []
        for spec in statespec:
            remove = spec.startswith("!")
            name = spec[1:] if remove else spec
            if remove and name in self._states:
                self._states.remove(name)
                changed.append(name)
            elif not remove and name not in self._states:
                self._states.add(name)
                changed.append("!" + name)
        if "disabled" in self._states:
            self._dragging = False
        super().configure(cursor="arrow" if "disabled" in self._states else "hand2",
                          takefocus="disabled" not in self._states)
        self._redraw()
        return tuple(changed)

    def instate(self, statespec, callback=None, *args):
        if isinstance(statespec, str):
            statespec = self.tk.splitlist(statespec)
        matches = all(
            spec[1:] not in self._states if spec.startswith("!") else spec in self._states
            for spec in statespec
        )
        if matches and callback is not None:
            return callback(*args)
        return matches

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str):
            return super().configure(cnf)
        if cnf:
            kwargs = dict(cnf, **kwargs)
        if not kwargs:
            return super().configure()
        for key, attr in (("from_", "_minimum"), ("from", "_minimum"), ("to", "_maximum")):
            if key in kwargs:
                setattr(self, attr, float(kwargs.pop(key)))
        for key, attr in (("command", "_command"), ("track", "_track_color"), ("accent", "_accent_color")):
            if key in kwargs:
                setattr(self, attr, kwargs.pop(key))
        if "variable" in kwargs:
            self._variable.trace_remove("write", self._trace_id)
            self._variable = kwargs.pop("variable")
            self._trace_id = self._variable.trace_add("write", self._redraw)
        if "state" in kwargs:
            self.state(["disabled"] if kwargs.pop("state") == "disabled" else ["!disabled"])
        result = super().configure(**kwargs) if kwargs else None
        self._redraw()
        return result

    config = configure

    def cget(self, key):
        custom = {
            "from": self._minimum,
            "from_": self._minimum,
            "to": self._maximum,
            "variable": str(self._variable),
            "command": self._command,
            "track": self._track_color,
            "accent": self._accent_color,
            "orient": "horizontal",
            "state": "disabled" if "disabled" in self._states else "normal",
        }
        return custom[key] if key in custom else super().cget(key)

    def _geometry(self):
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            width = int(float(super().cget("width")))
        if height <= 1:
            height = int(float(super().cget("height")))
        padding = min(10, width / 2)
        return padding, max(padding, width - padding), height / 2

    def _redraw(self, *_):
        if not hasattr(self, "_track"):
            return
        start, end, middle = self._geometry()
        distance = self._maximum - self._minimum
        fraction = (self.get() - self._minimum) / distance if distance else 0
        position = start + fraction * (end - start)
        disabled = "disabled" in self._states
        self.coords(self._track, start, middle, end, middle)
        self.itemconfigure(self._track, fill=self._track_color)
        self.coords(self._fill, start, middle, position, middle)
        self.itemconfigure(self._fill, fill=self._accent_color,
                           state="hidden" if disabled or fraction <= 0 else "normal")
        self.coords(self._thumb, position - 5, middle - 5, position + 5, middle + 5)
        self.itemconfigure(self._thumb, fill=self._accent_color,
                           state="hidden" if disabled else "normal")
        self.coords(self._focus_ring, position - 8, middle - 8, position + 8, middle + 8)
        self.itemconfigure(self._focus_ring, outline=self._accent_color,
                           state="normal" if "focus" in self._states and not disabled else "hidden")

    def _user_value(self, value):
        if "disabled" in self._states:
            return
        self.set(value)
        if self._command is not None:
            self._command(str(self.get()))

    def _pointer_value(self, event):
        start, end, _ = self._geometry()
        fraction = max(0, min(1, (event.x - start) / max(1, end - start)))
        return self._minimum + fraction * (self._maximum - self._minimum)

    def _press(self, event):
        if "disabled" not in self._states:
            self.focus_set()
            self._dragging = True
            self._user_value(self._pointer_value(event))

    def _motion(self, event):
        if self._dragging:
            self._user_value(self._pointer_value(event))

    def _release(self, event):
        self._dragging = False

    def _keyboard_value(self, value):
        self._user_value(value)
        return "break"

    def _step(self, amount):
        return self._keyboard_value(self.get() + amount * (self._maximum - self._minimum) / 100)

    def _focus_in(self, event):
        self._states.add("focus")
        self._redraw()

    def _focus_out(self, event):
        self._states.discard("focus")
        self._redraw()

    def _destroyed(self, event):
        if event.widget is not self:
            return
        if self._trace_id is not None:
            self._variable.trace_remove("write", self._trace_id)
            self._trace_id = None
        for sequence, _ in self._bindings:
            self.unbind_class(self._tag, sequence)
