"""Visual layer for StreamZip.

Tkinter's stock widgets look like Windows 95 because ttk on Windows refuses to
colour most native controls. So the chrome here is built from plain tk widgets
and Canvas drawing, which we *can* style completely.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --------------------------------------------------------------------------
# palette
# --------------------------------------------------------------------------

BG = "#0b0d12"          # window
SURFACE = "#141821"     # cards
SURFACE_2 = "#1b2029"   # inputs, wells
SURFACE_3 = "#232a36"   # hover
BORDER = "#252d3a"
BORDER_LIT = "#33405a"

TEXT = "#e7eaf0"
MUTED = "#98a2b6"
FAINT = "#616c81"

ACCENT = "#4f8cff"
ACCENT_HOVER = "#6ba0ff"
ACCENT_DIM = "#1e3966"

SUCCESS = "#3fb950"
WARNING = "#d9a123"
DANGER = "#f06a5f"

FAMILY = "Segoe UI"
MONO = "Consolas"


def fonts():
    return {
        "title": (FAMILY, 17, "bold"),
        "subtitle": (FAMILY, 9),
        "label": (FAMILY, 9),
        "body": (FAMILY, 10),
        "input": (FAMILY, 10),
        "button": (FAMILY, 10),
        "button_bold": (FAMILY, 10, "bold"),
        "stat": (FAMILY, 16, "bold"),
        "stat_label": (FAMILY, 8),
        "section": (FAMILY, 10, "bold"),
        "mono": (MONO, 9),
    }


def setup(root):
    """Dark-mode the few ttk widgets we still use."""
    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")   # the only built-in theme that honours colours
    except tk.TclError:
        pass
    style.configure("Dark.Vertical.TScrollbar", background=SURFACE_3,
                    troughcolor=SURFACE, bordercolor=SURFACE,
                    arrowcolor=MUTED, relief="flat", borderwidth=0)
    style.map("Dark.Vertical.TScrollbar",
              background=[("active", BORDER_LIT)])
    # make sure the default font on message boxes etc. is sane
    try:
        default = tkfont.nametofont("TkDefaultFont")
        default.configure(family=FAMILY, size=9)
    except tk.TclError:
        pass
    return style


# --------------------------------------------------------------------------
# building blocks
# --------------------------------------------------------------------------

def round_rect(canvas, x0, y0, x1, y1, r, **kwargs):
    """A rounded rectangle as a smoothed polygon."""
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class Card(tk.Frame):
    """A padded surface with a hairline border."""

    def __init__(self, master, **kw):
        super().__init__(master, bg=SURFACE, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BORDER,
                         bd=0, **kw)


def label(master, text, style="body", fg=TEXT, bg=SURFACE, **kw):
    return tk.Label(master, text=text, font=fonts()[style], fg=fg, bg=bg,
                    anchor="w", **kw)


class Entry(tk.Entry):
    """Flat dark input that lights its border on focus."""

    def __init__(self, master, textvariable=None, **kw):
        super().__init__(master, textvariable=textvariable,
                         font=fonts()["input"], bg=SURFACE_2, fg=TEXT,
                         insertbackground=ACCENT, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, disabledbackground=SURFACE_2,
                         disabledforeground=FAINT, **kw)
        self.configure(insertwidth=2)


class Button(tk.Button):
    """Flat button with a hover state. variant: primary | ghost | quiet."""

    VARIANTS = {
        "primary": (ACCENT, "#0a1220", ACCENT_HOVER),
        "ghost": (SURFACE_2, TEXT, SURFACE_3),
        "quiet": (SURFACE, MUTED, SURFACE_2),
        "danger": (SURFACE_2, DANGER, "#2a1d1f"),
    }

    def __init__(self, master, text, command=None, variant="ghost", **kw):
        bg, fg, hover = self.VARIANTS[variant]
        self._bg, self._hover = bg, hover
        font = fonts()["button_bold"] if variant == "primary" else fonts()["button"]
        super().__init__(master, text=text, command=command, font=font,
                         bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                         relief="flat", bd=0, highlightthickness=0,
                         cursor="hand2", padx=16, pady=7,
                         disabledforeground=FAINT, **kw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _enter(self, _e):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._hover)

    def _leave(self, _e):
        self.configure(bg=self._bg)

    def set_enabled(self, on):
        self.configure(state="normal" if on else "disabled",
                       cursor="hand2" if on else "arrow",
                       bg=self._bg if on else SURFACE_2)


class Check(tk.Checkbutton):
    def __init__(self, master, text, variable, bg=SURFACE, **kw):
        super().__init__(master, text=text, variable=variable, font=fonts()["label"],
                         bg=bg, fg=MUTED, activebackground=bg, activeforeground=TEXT,
                         selectcolor=SURFACE_2, relief="flat", bd=0,
                         highlightthickness=0, anchor="w", cursor="hand2",
                         padx=0, **kw)


class Pill(tk.Canvas):
    """Status chip: a coloured dot plus a word."""

    def __init__(self, master, bg=BG):
        super().__init__(master, height=26, width=132, bg=bg,
                         highlightthickness=0, bd=0)
        self._bg = bg
        self.set("Idle", FAINT)

    def set(self, text, colour):
        self.delete("all")
        w = max(96, len(text) * 8 + 40)
        self.configure(width=w)
        round_rect(self, 1, 2, w - 1, 24, 11, fill=SURFACE, outline=BORDER)
        self.create_oval(13, 10, 21, 18, fill=colour, outline="")
        self.create_text(29, 13, text=text, anchor="w", fill=TEXT,
                         font=fonts()["label"])


class ProgressBar(tk.Canvas):
    """Rounded progress track. value is 0..1, or None for indeterminate."""

    def __init__(self, master, height=10, bg=SURFACE, colour=ACCENT):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self._h = height
        self._bg = bg
        self._colour = colour
        self._value = 0.0
        self._pulse = 0.0
        self._pulsing = False
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, value):
        self._pulsing = value is None
        self._value = 0.0 if value is None else max(0.0, min(1.0, value))
        self._draw()

    def colour(self, colour):
        self._colour = colour
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self._h
        if w <= 2:
            return
        r = h / 2
        round_rect(self, 0, 0, w, h, r, fill=SURFACE_2, outline="")
        if self._pulsing:
            span = w * 0.28
            x = (w + span) * self._pulse - span
            round_rect(self, max(0, x), 0, min(w, x + span), h, r,
                       fill=ACCENT_DIM, outline="")
        elif self._value > 0:
            fill_w = max(h, w * self._value)
            round_rect(self, 0, 0, fill_w, h, r, fill=self._colour, outline="")

    def tick(self):
        """Advance the indeterminate shimmer. Call ~20x a second."""
        if self._pulsing:
            self._pulse = (self._pulse + 0.02) % 1.0
            self._draw()


class Stat(tk.Frame):
    """One metric: small grey caption above a big bright number."""

    def __init__(self, master, caption, value="--", bg=SURFACE):
        super().__init__(master, bg=bg)
        self.caption = tk.Label(self, text=caption.upper(), font=fonts()["stat_label"],
                                fg=FAINT, bg=bg, anchor="w")
        self.caption.pack(anchor="w")
        self.value = tk.Label(self, text=value, font=fonts()["stat"], fg=TEXT,
                              bg=bg, anchor="w")
        self.value.pack(anchor="w", pady=(1, 0))

    def set(self, value, colour=TEXT):
        self.value.configure(text=value, fg=colour)


class Collapsible(tk.Frame):
    """A section that folds away, so the default view stays simple."""

    def __init__(self, master, title, open_=False, bg=BG):
        super().__init__(master, bg=bg)
        self._open = open_
        self._title = title
        self.header = tk.Label(self, font=fonts()["label"], fg=MUTED, bg=bg,
                               anchor="w", cursor="hand2", padx=2, pady=4)
        self.header.pack(fill="x")
        self.header.bind("<Button-1>", lambda _e: self.toggle())
        self.header.bind("<Enter>", lambda _e: self.header.configure(fg=TEXT))
        self.header.bind("<Leave>", lambda _e: self.header.configure(fg=MUTED))
        self.body = tk.Frame(self, bg=bg)
        self._sync()

    def toggle(self):
        self._open = not self._open
        self._sync()

    def open(self):
        if not self._open:
            self.toggle()

    def _sync(self):
        arrow = "▾" if self._open else "▸"
        self.header.configure(text=f"{arrow}  {self._title}")
        if self._open:
            self.body.pack(fill="both", expand=True, pady=(2, 0))
        else:
            self.body.pack_forget()
