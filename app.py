"""StreamZip - paste a link, get the extracted files.

The .zip itself is never written to disk. Bytes arrive, get inflated in a 1 MiB
window, and only the extracted files are stored. So a 95 GB archive needs room
for its *contents*, not for the contents plus the archive.

Run:  python app.py    (or double-click StreamZip.bat)
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import core
import theme as T

APP_NAME = "StreamZip"

# state -> (pill text, pill colour)
STATES = {
    "idle": ("Ready", T.FAINT),
    "checking": ("Checking", T.ACCENT),
    "ready": ("Inspected", T.ACCENT),
    "running": ("Downloading", T.ACCENT),
    "paused": ("Paused", T.WARNING),
    "done": ("Complete", T.SUCCESS),
    "failed": ("Failed", T.DANGER),
    "stopped": ("Stopped", T.WARNING),
}


class App(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=T.BG, padx=22, pady=18)
        self.pack(fill="both", expand=True)

        self.events = queue.Queue()
        self.job = None
        self.worker = None
        self.manifest = None
        self.dest_touched = False
        self.state = "idle"

        self._build()
        self.after(100, self._drain)
        self.after(50, self._animate)
        self.after(200, self._offer_clipboard)

    # ==================================================================
    # layout
    # ==================================================================

    def _build(self):
        self._header()
        self._source_card()
        self._transfer_card()
        self._advanced()
        self._log_pane()
        self._refresh_free_space()

    def _header(self):
        bar = tk.Frame(self, bg=T.BG)
        bar.pack(fill="x", pady=(0, 14))

        left = tk.Frame(bar, bg=T.BG)
        left.pack(side="left")
        tk.Label(left, text="StreamZip", font=T.fonts()["title"], fg=T.TEXT,
                 bg=T.BG, anchor="w").pack(anchor="w")
        tk.Label(left, text="Downloads and unzips in one pass - the .zip never touches your drive",
                 font=T.fonts()["subtitle"], fg=T.FAINT, bg=T.BG,
                 anchor="w").pack(anchor="w", pady=(1, 0))

        self.pill = T.Pill(bar, bg=T.BG)
        self.pill.pack(side="right", pady=6)

    def _source_card(self):
        card = T.Card(self)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=T.SURFACE, padx=18, pady=16)
        inner.pack(fill="x")
        inner.columnconfigure(1, weight=1)

        T.label(inner, "Zip link", "label", T.MUTED).grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar()
        self.url_var.trace_add("write", lambda *_: self._url_changed())
        url = T.Entry(inner, self.url_var)
        url.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=7, pady=(4, 0))
        url.bind("<Return>", lambda _e: self._start())
        url.focus_set()
        T.Button(inner, "Paste", self._paste, "ghost").grid(row=1, column=2,
                                                            sticky="e", padx=(8, 0),
                                                            pady=(4, 0))

        T.label(inner, "Extract into", "label", T.MUTED).grid(row=2, column=0,
                                                              sticky="w", pady=(14, 0))
        self.free_lbl = T.label(inner, "", "label", T.FAINT)
        self.free_lbl.grid(row=2, column=1, columnspan=2, sticky="e", pady=(14, 0))

        self.dest_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.dest_var.trace_add("write", lambda *_: self._refresh_free_space())
        dest = T.Entry(inner, self.dest_var)
        dest.grid(row=3, column=0, columnspan=2, sticky="ew", ipady=7, pady=(4, 0))
        dest.bind("<Key>", lambda _e: setattr(self, "dest_touched", True))
        T.Button(inner, "Browse", self._browse, "ghost").grid(row=3, column=2,
                                                              sticky="e", padx=(8, 0),
                                                              pady=(4, 0))

        actions = tk.Frame(inner, bg=T.SURFACE)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.start_btn = T.Button(actions, "Start", self._start, "primary")
        self.start_btn.pack(side="left")
        self.check_btn = T.Button(actions, "Inspect only", self._check, "ghost")
        self.check_btn.pack(side="left", padx=8)
        self.open_btn = T.Button(actions, "Open folder", self._open_dest, "quiet")
        self.open_btn.pack(side="right")

    def _transfer_card(self):
        card = T.Card(self)
        card.pack(fill="x", pady=(14, 0))
        inner = tk.Frame(card, bg=T.SURFACE, padx=18, pady=16)
        inner.pack(fill="x")

        stats = tk.Frame(inner, bg=T.SURFACE)
        stats.pack(fill="x")
        self.stat_speed = T.Stat(stats, "Download speed", "--")
        self.stat_written = T.Stat(stats, "Extracted", "--")
        self.stat_files = T.Stat(stats, "Files", "--")
        self.stat_eta = T.Stat(stats, "Time left", "--")
        for i, s in enumerate((self.stat_speed, self.stat_written,
                               self.stat_files, self.stat_eta)):
            s.grid(row=0, column=i, sticky="w", padx=(0, 34))

        self.overall = T.ProgressBar(inner, height=12)
        self.overall.pack(fill="x", pady=(16, 0))
        self.overall_lbl = T.label(inner, "Nothing running", "label", T.MUTED)
        self.overall_lbl.pack(fill="x", pady=(6, 0))

        self.current = T.ProgressBar(inner, height=6, colour=T.BORDER_LIT)
        self.current.pack(fill="x", pady=(12, 0))
        self.current_lbl = T.label(inner, "", "label", T.FAINT)
        self.current_lbl.pack(fill="x", pady=(5, 0))

        controls = tk.Frame(inner, bg=T.SURFACE)
        controls.pack(fill="x", pady=(14, 0))
        self.pause_btn = T.Button(controls, "Pause", self._toggle_pause, "ghost")
        self.pause_btn.pack(side="left")
        self.stop_btn = T.Button(controls, "Stop", self._stop, "danger")
        self.stop_btn.pack(side="left", padx=8)
        self.pause_btn.set_enabled(False)
        self.stop_btn.set_enabled(False)

    def _advanced(self):
        self.adv = T.Collapsible(self, "Advanced options", open_=True, bg=T.BG)
        self.adv.pack(fill="x", pady=(14, 0))

        card = T.Card(self.adv.body)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=T.SURFACE, padx=18, pady=14)
        inner.pack(fill="x")
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(3, weight=1)

        T.label(inner, "Only these", "label", T.MUTED).grid(row=0, column=0, sticky="w")
        self.include_var = tk.StringVar()
        T.Entry(inner, self.include_var).grid(row=0, column=1, sticky="ew",
                                              ipady=5, padx=(8, 18))
        T.label(inner, "Skip these", "label", T.MUTED).grid(row=0, column=2, sticky="w")
        self.exclude_var = tk.StringVar()
        T.Entry(inner, self.exclude_var).grid(row=0, column=3, sticky="ew",
                                              ipady=5, padx=(8, 0))

        T.label(inner, "Comma-separated wildcards like *.mp4 or data/2024/* - blank means "
                       "everything. Use these to pull a huge archive down in batches when "
                       "space is tight.", "label", T.FAINT,
                wraplength=740, justify="left").grid(row=1, column=0, columnspan=4,
                                                     sticky="w", pady=(8, 10))

        self.skip_var = tk.BooleanVar(value=True)
        self.crc_var = tk.BooleanVar(value=True)
        self.insecure_var = tk.BooleanVar(value=False)
        T.Check(inner, "Resume - skip files already extracted",
                self.skip_var).grid(row=2, column=0, columnspan=2, sticky="w")
        T.Check(inner, "Verify checksums",
                self.crc_var).grid(row=2, column=2, columnspan=2, sticky="w")
        T.Check(inner, "Skip certificate check (insecure, only for links you trust)",
                self.insecure_var).grid(row=3, column=0, columnspan=4,
                                        sticky="w", pady=(4, 0))

        T.label(inner, "Request header", "label", T.MUTED).grid(row=4, column=0,
                                                                sticky="w", pady=(12, 0))
        self.header_var = tk.StringVar()
        T.Entry(inner, self.header_var).grid(row=4, column=1, columnspan=3, sticky="ew",
                                             ipady=5, padx=(8, 0), pady=(12, 0))
        T.label(inner, "For links behind a login, e.g. Cookie: session=abc   or   "
                       "Authorization: Bearer ...", "label", T.FAINT).grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        fix = tk.Frame(inner, bg=T.SURFACE)
        fix.grid(row=6, column=0, columnspan=4, sticky="w", pady=(12, 0))
        self.fix_btn = T.Button(fix, "Fix certificates", self._fix_certificates, "ghost")
        self.fix_btn.pack(side="left")
        T.label(fix, "  Installs the full trust store if HTTPS verification fails",
                "label", T.FAINT).pack(side="left")

    def _log_pane(self):
        self.logs = T.Collapsible(self, "Activity log", open_=True, bg=T.BG)
        self.logs.pack(fill="both", expand=True, pady=(14, 0))

        card = T.Card(self.logs.body)
        card.pack(fill="both", expand=True)
        wrap = tk.Frame(card, bg=T.SURFACE, padx=4, pady=4)
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.log = tk.Text(wrap, height=9, wrap="none", state="disabled",
                           font=T.fonts()["mono"], bg=T.SURFACE, fg=T.MUTED,
                           relief="flat", bd=0, highlightthickness=0,
                           padx=12, pady=8, spacing1=1)
        self.log.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview,
                            style="Dark.Vertical.TScrollbar")
        bar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=bar.set)
        self.log.tag_configure("warn", foreground=T.WARNING)
        self.log.tag_configure("error", foreground=T.DANGER)
        self.log.tag_configure("good", foreground=T.SUCCESS)
        self.log.tag_configure("info", foreground=T.MUTED)

        self._log("Paste a .zip link and press Start.")
        self._log("Inspect only reads the archive index first, so you can see the "
                  "extracted size before committing disk space.")

    # ==================================================================
    # small helpers
    # ==================================================================

    def _log(self, msg, level="info"):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", (level,))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_state(self, state):
        self.state = state
        text, colour = STATES[state]
        self.pill.set(text, colour)
        running = state in ("checking", "running", "paused")
        self.start_btn.set_enabled(not running)
        self.check_btn.set_enabled(not running)
        self.pause_btn.set_enabled(state in ("running", "paused"))
        self.stop_btn.set_enabled(running)
        if state != "paused":
            self.pause_btn.configure(text="Pause")

    def _paste(self):
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo(APP_NAME, "Nothing on the clipboard.")

    def _offer_clipboard(self):
        """If a zip link is already on the clipboard, save them the paste."""
        if self.url_var.get():
            return
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if text.lower().startswith(("http://", "https://")) and ".zip" in text.lower():
            self.url_var.set(text)
            self._log("Picked up a .zip link from your clipboard.", "good")

    def _browse(self):
        chosen = filedialog.askdirectory(initialdir=self.dest_var.get() or ".")
        if chosen:
            self.dest_var.set(os.path.normpath(chosen))
            self.dest_touched = True

    def _url_changed(self):
        if self.dest_touched:
            return
        url = self.url_var.get().strip()
        if not url:
            return
        stem = os.path.splitext(core.filename_from_url(url))[0] or "extracted"
        current = self.dest_var.get()
        parent = current if os.path.basename(current).lower() == "downloads" \
            else os.path.dirname(current.rstrip("\\/")) or os.path.expanduser("~")
        self.dest_var.set(os.path.normpath(os.path.join(parent, stem)))

    def _refresh_free_space(self):
        try:
            free = core.free_space(self.dest_var.get() or ".")
            drive = os.path.splitdrive(os.path.abspath(self.dest_var.get()))[0] or "disk"
            self.free_lbl.configure(text=f"{core.human(free)} free on {drive}")
        except Exception:
            self.free_lbl.configure(text="")

    def _open_dest(self):
        path = self.dest_var.get()
        if os.path.isdir(path):
            if os.name == "nt":
                os.startfile(path)
            else:
                webbrowser.open("file://" + path)
        else:
            messagebox.showinfo(APP_NAME, "That folder does not exist yet.")

    def _headers(self):
        raw = self.header_var.get().strip()
        if not raw or ":" not in raw:
            return {}
        key, value = raw.split(":", 1)
        return {key.strip(): value.strip()}

    def _patterns(self, var):
        return [p.strip() for p in var.get().split(",") if p.strip()]

    # ==================================================================
    # actions
    # ==================================================================

    def _make_job(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(APP_NAME, "Paste a link to a .zip first.")
            return None
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning(APP_NAME, "Only http:// and https:// links work here.")
            return None
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showwarning(APP_NAME, "Choose a folder to extract into.")
            return None
        return core.Job(
            url, dest,
            headers=self._headers(),
            include=self._patterns(self.include_var),
            exclude=self._patterns(self.exclude_var),
            skip_existing=self.skip_var.get(),
            verify_crc=self.crc_var.get(),
            insecure=self.insecure_var.get(),
            on_log=lambda m, level="info": self.events.put(("log", m, level)),
            on_progress=lambda s: self.events.put(("progress", s)),
        )

    def _check(self, then_start=False):
        job = self._make_job()
        if job is None:
            return
        self.job = job
        self._set_state("checking")
        self.overall.set(None)
        self.overall_lbl.configure(text="Reading the archive index...")
        self.worker = threading.Thread(target=self._check_worker,
                                       args=(job, then_start), daemon=True)
        self.worker.start()

    def _check_worker(self, job, then_start):
        try:
            manifest = job.inspect()
            self.events.put(("manifest", manifest, then_start))
        except core.Cancelled:
            self.events.put(("done", "Stopped.", "stopped"))
        except Exception as exc:
            self.events.put(("failed", core.describe_error(exc), core.is_cert_error(exc)))

    def _on_manifest(self, manifest, then_start):
        self.manifest = manifest
        job = self.job
        self.overall.set(0)
        self._log("")
        self._log(f"Archive {core.human(manifest.archive_size)}  |  byte ranges: "
                  f"{'supported' if manifest.supports_ranges else 'not supported'}")

        if not manifest.supports_ranges:
            self._log("Without ranges the file list only appears while extracting, "
                      "and a stopped run cannot resume.", "warn")
            self._set_state("ready")
            if then_start:
                self._launch(None)
            return

        wanted = [e for e in manifest.files if job.selected(e)]
        total = sum(e.usize for e in wanted)
        free = core.free_space(job.dest)
        self._log(f"{len(manifest.files):,} members, {len(wanted):,} selected")
        self._log(f"Extracted size {core.human(total)}  "
                  f"(compressed {core.human(sum(e.csize for e in wanted))})")
        self._log(f"Free space {core.human(free)}")

        for e in sorted(wanted, key=lambda e: e.usize, reverse=True)[:5]:
            self._log(f"    {core.human(e.usize):>12}   {e.name}")

        if total and manifest.archive_size:
            self._log(f"Save-then-unzip would need "
                      f"{core.human(manifest.archive_size + total)}; this needs "
                      f"{core.human(total)}.", "good")

        self._set_state("ready")
        if total > free:
            self._log(f"Not enough room: {core.human(total)} needed, "
                      f"{core.human(free)} free.", "error")
            if not messagebox.askyesno(
                    APP_NAME,
                    f"The extracted contents are {core.human(total)} but only "
                    f"{core.human(free)} is free.\n\n"
                    "Files land one at a time, so you can move them off as they "
                    "appear, or use 'Only these' to pull the archive in batches.\n\n"
                    "Start anyway?"):
                self.overall_lbl.configure(text="Not started - not enough space")
                return
        else:
            self._log("Fits comfortably.", "good")

        if then_start:
            self._launch(manifest)
        else:
            self.overall_lbl.configure(text="Inspected. Press Start when ready.")

    def _start(self):
        if self.manifest is None or self.job is None or \
                self.job.url != self.url_var.get().strip():
            self._check(then_start=True)
            return
        job = self._make_job()
        if job is None:
            return
        self.job = job
        self._launch(self.manifest)

    def _launch(self, manifest):
        self._set_state("running")
        self.overall_lbl.configure(text="Starting...")
        self.worker = threading.Thread(target=self._run_worker,
                                       args=(self.job, manifest), daemon=True)
        self.worker.start()

    def _run_worker(self, job, manifest):
        try:
            stats = job.run(manifest)
            summary = (f"Done - {stats.files_done:,} files, "
                       f"{core.human(stats.bytes_written)} extracted")
            if stats.files_skipped:
                summary += f", {stats.files_skipped:,} already present"
            self.events.put(("done", summary, "done"))
        except core.Cancelled:
            self.events.put(("done", "Stopped. Start again to resume from the "
                                     "last completed file.", "stopped"))
        except Exception as exc:
            self.events.put(("failed", core.describe_error(exc), core.is_cert_error(exc)))

    def _toggle_pause(self):
        if not self.job:
            return
        if self.job.pause.is_set():
            self.job.pause.clear()
            self.pause_btn.configure(text="Pause")
            self._set_state("running")
            self._log("Resumed.")
        else:
            self.job.pause.set()
            self.pause_btn.configure(text="Resume")
            self._set_state("paused")
            self._log("Paused.", "warn")

    def _stop(self):
        if self.job:
            self.job.stop()
            self.stop_btn.set_enabled(False)
            self.overall_lbl.configure(text="Stopping...")

    # ==================================================================
    # certificate repair
    # ==================================================================

    def _fix_certificates(self):
        """Windows hands Python a thin snapshot of its root store. Installing
        truststore lets us defer to Windows itself; certifi is the fallback."""
        if not messagebox.askyesno(
                APP_NAME,
                "Install the full certificate trust store?\n\n"
                "This runs: pip install truststore certifi\n"
                "About 200 KB, one time. Needs a working connection."):
            return
        self.adv.open()
        self.fix_btn.set_enabled(False)
        self._log("Installing truststore + certifi ...")
        threading.Thread(target=self._fix_worker, daemon=True).start()

    def _fix_worker(self):
        exe = sys.executable
        if os.path.basename(exe).lower().startswith("pythonw"):
            console = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.exists(console):
                exe = console
        cmd = [exe, "-m", "pip", "install", "--upgrade", "truststore", "certifi"]
        flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  creationflags=flags, timeout=300)
            output = (proc.stdout or "") + (proc.stderr or "")
            tail = " | ".join(output.strip().splitlines()[-2:])
            self.events.put(("fixed", proc.returncode == 0, tail))
        except Exception as exc:
            self.events.put(("fixed", False, str(exc)))

    def _on_fixed(self, ok, detail):
        self.fix_btn.set_enabled(True)
        core._SSL_CACHE.clear()
        if ok:
            self._log(f"Certificates installed. Trust source is now "
                      f"{core.ssl_backend()}. Press Start again.", "good")
        else:
            self._log("Could not install: " + detail, "error")
            self._log("Fallback: tick 'Skip certificate check' if you trust the link.",
                      "warn")

    # ==================================================================
    # worker -> UI
    # ==================================================================

    def _animate(self):
        self.overall.tick()
        self.after(50, self._animate)

    def _drain(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._log(event[1], event[2])
                elif kind == "progress":
                    self._render(event[1])
                elif kind == "manifest":
                    self._on_manifest(event[1], event[2])
                elif kind == "fixed":
                    self._on_fixed(event[1], event[2])
                elif kind == "done":
                    self._set_state(event[2])
                    self.overall_lbl.configure(text=event[1])
                    self.current.set(0)
                    self.current_lbl.configure(text="")
                    self.stat_eta.set("--")
                    self.stat_speed.set("--")
                    if event[2] == "done":
                        self.overall.set(1.0)
                        self.overall.colour(T.SUCCESS)
                    self._log(event[1], "good" if event[2] == "done" else "warn")
                    self._refresh_free_space()
                elif kind == "failed":
                    self._set_state("failed")
                    self.overall.set(0)
                    self.overall_lbl.configure(text="Failed")
                    self._log("ERROR: " + event[1], "error")
                    messagebox.showerror(APP_NAME, event[1])
                    if len(event) > 2 and event[2]:
                        self._fix_certificates()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _render(self, s):
        s.sample()
        self.overall.colour(T.ACCENT)
        if s.bytes_total:
            frac = s.bytes_written / s.bytes_total
            self.overall.set(frac)
            self.overall_lbl.configure(
                text=f"{frac * 100:.1f}%  -  {core.human(s.bytes_written)} of "
                     f"{core.human(s.bytes_total)}  -  "
                     f"{core.human(s.bytes_downloaded)} pulled over the wire")
        else:
            self.overall.set(None)
            self.overall_lbl.configure(
                text=f"{core.human(s.bytes_written)} extracted  -  "
                     f"{core.human(s.bytes_downloaded)} pulled over the wire")

        self.stat_speed.set(core.human(s.download_rate) + "/s")
        self.stat_written.set(core.human(s.bytes_written))
        self.stat_files.set(f"{s.files_done:,}" + (f" / {s.files_total:,}"
                                                   if s.files_total else ""))
        self.stat_eta.set(core.human_time(s.eta))

        if s.current:
            self.current.set(s.current_written / s.current_total
                             if s.current_total else None)
            name = s.current if len(s.current) < 70 else "..." + s.current[-67:]
            self.current_lbl.configure(
                text=f"{name}   {core.human(s.current_written)} of "
                     f"{core.human(s.current_total)}   "
                     f"(writing {core.human(s.rate)}/s)")


def main():
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("880x780")
    root.minsize(760, 620)
    T.setup(root)
    app = App(root)

    def on_close():
        if app.job and app.worker and app.worker.is_alive():
            if not messagebox.askokcancel(
                    APP_NAME, "A transfer is running. Quit anyway?\n\n"
                              "Finished files are kept - restarting resumes from "
                              "the last completed one."):
                return
            app.job.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
