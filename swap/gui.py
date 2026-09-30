"""Interface graphique (tkinter, fournie avec Python) : analyser, choisir, envoyer, recevoir, restaurer."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import time
import traceback
import webbrowser
from types import SimpleNamespace
from typing import Optional

import threading

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import __version__, net
from .session import Job, Session, find_backups
from .sink import SinkError
from .util import human_size, is_windows

PAD = 8
CHECKED, UNCHECKED = "☑", "☐"


CATEGORY_ORDER = ["Dossiers personnels", "PC SOFT", "Installateurs", "Dossiers hors profil", "Autres disques", "Configuration"]


def category_rank(category: str) -> int:
    """Ordre d'affichage : ce qui compte le plus pour l'utilisateur d'abord, la configuration des applis en dernier."""
    for rank, prefix in enumerate(CATEGORY_ORDER):
        if category.startswith(prefix):
            return rank
    return len(CATEGORY_ORDER)


def open_path(path: str) -> None:
    try:
        if is_windows():
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            webbrowser.open("file://" + os.path.abspath(path))
    except OSError as exc:
        messagebox.showerror("swap", f"Impossible d'ouvrir {path} : {exc}")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


class App(tk.Tk):
    def __init__(self, session: Optional[Session] = None, backup: Optional[str] = None):
        super().__init__()
        self.session = session or Session(os.path.abspath("swap-sortie"))
        self.title(f"swap {__version__} — migration de poste")
        self.geometry(f"1120x{min(800, max(600, self.winfo_screenheight() - 90))}+20+20")
        self.minsize(900, 560)
        style = ttk.Style(self)
        style.theme_use("vista" if is_windows() and "vista" in style.theme_names() else "clam")
        self.f_bold = tkfont.nametofont("TkDefaultFont").copy()
        self.f_bold.configure(weight="bold")
        self.f_mono = tkfont.nametofont("TkFixedFont").copy()
        self.f_mono.configure(size=11)
        self.f_code = tkfont.nametofont("TkFixedFont").copy()
        self.f_code.configure(size=26, weight="bold")
        self.f_arrow = tkfont.nametofont("TkDefaultFont").copy()
        self.f_arrow.configure(size=16)
        style.configure("Big.TButton", padding=(14, 8), font=self.f_bold)
        style.configure("Code.TLabel", font=self.f_code)
        style.configure("Head.TLabel", font=self.f_bold)
        self.job: Optional[Job] = None
        self.rx: Optional[net.Receiver] = None
        self.rx_events: "queue.Queue[dict]" = queue.Queue()
        self.rx_firewall = False

        self.v_host = tk.StringVar()
        self.v_mode = tk.StringVar(value="direct")
        self.v_code = tk.StringVar()
        self.v_port = tk.StringVar(value=str(net.DEFAULT_PORT))
        self.v_share = tk.StringVar(value="C$")
        self.v_subfolder = tk.StringVar(value="SWAP")
        self.v_folder = tk.StringVar()
        self.v_hash = tk.BooleanVar(value=False)
        self.v_dry = tk.BooleanVar(value=False)
        self.v_rx_dest = tk.StringVar(value="C:\\SWAP" if is_windows() else os.path.expanduser("~/SWAP"))
        self.v_rx_port = tk.StringVar(value=str(net.DEFAULT_PORT))
        self.v_rx_fw = tk.BooleanVar(value=True)
        self.v_backup = tk.StringVar()
        self.v_overwrite = tk.BooleanVar(value=False)
        self.v_restore_dry = tk.BooleanVar(value=False)

        self._build_header()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
        self._build_scan_tab()
        self._build_choose_tab()
        self._build_send_tab()
        self._build_receive_tab()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._rx_poll_id = self.after(150, self._poll_receiver)
        if backup:  # lancé depuis RESTAURER.bat : on va directement à la restauration de ce dossier
            self.v_backup.set(os.path.normpath(backup))
            self.nb.select(3)

    def destroy(self) -> None:
        try:
            self.after_cancel(self._rx_poll_id)
        except (tk.TclError, AttributeError):
            pass
        super().destroy()

    # ------------------------------------------------------------------------------------------------------------
    # en-tête
    def _build_header(self) -> None:
        bar = ttk.Frame(self, padding=(PAD, PAD, PAD, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="PC source", style="Head.TLabel").pack(side="left")
        self.lbl_source = ttk.Label(bar, text=f"{self.session.machine}  (ce poste)", font=self.f_mono)
        self.lbl_source.pack(side="left", padx=(6, 18))
        ttk.Label(bar, text="→", font=self.f_arrow).pack(side="left")
        ttk.Label(bar, text="PC cible", style="Head.TLabel").pack(side="left", padx=(18, 6))
        self.ent_host = ttk.Entry(bar, textvariable=self.v_host, width=26, font=self.f_mono)
        self.ent_host.pack(side="left")
        ttk.Label(bar, text="nom (ex. TPSEL045) ou adresse IP", foreground="#666").pack(side="left", padx=8)

    # ------------------------------------------------------------------------------------------------------------
    # 1. Analyser
    def _build_scan_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(tab, text="1. Analyser")
        ttk.Label(tab, wraplength=980, justify="left", text=(
            "swap parcourt ce poste (dossiers, applications, imprimantes, lecteurs réseau, certificats…) et prépare la liste de "
            "tout ce qui doit être migré. Cette étape ne modifie rien : elle lit seulement.")).pack(anchor="w")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=PAD)
        self.btn_scan = ttk.Button(row, text="Analyser ce poste", style="Big.TButton", command=self.on_scan)
        self.btn_scan.pack(side="left")
        self.btn_scan_cancel = ttk.Button(row, text="Annuler", command=self.on_cancel, state="disabled")
        self.btn_scan_cancel.pack(side="left", padx=6)
        ttk.Button(row, text="Charger une analyse existante…", command=self.on_load_inventory).pack(side="left", padx=(24, 0))
        self.btn_report = ttk.Button(row, text="Ouvrir le rapport détaillé", command=lambda: open_path(self.session.report_path()),
                                     state="disabled")
        self.btn_report.pack(side="left", padx=6)
        self.scan_bar = ttk.Progressbar(tab, mode="indeterminate")
        self.scan_bar.pack(fill="x")
        self.v_scan_status = tk.StringVar(value="Prêt.")
        ttk.Label(tab, textvariable=self.v_scan_status).pack(anchor="w", pady=(2, PAD))
        self.txt_summary = self._text(tab, height=20)
        self.ui_scan = SimpleNamespace(bar=self.scan_bar, status=self.v_scan_status, log=None, cancel=self.btn_scan_cancel,
                                       actions=[self.btn_scan], done=self._scan_done)

    def on_scan(self) -> None:
        self._set_text(self.txt_summary, "")
        self.start_job(self.session.scan, self.ui_scan)

    def _scan_done(self, job: Job) -> None:
        if job.cancelled:
            self.v_scan_status.set("Analyse annulée.")
            return
        if job.error:
            self.v_scan_status.set("L'analyse a échoué.")
            return self._show_error("L'analyse a échoué", job)
        self.v_scan_status.set(f"Analyse terminée. Rapport : {self.session.report_path()}")
        self._after_inventory()
        self.nb.select(1)

    def on_load_inventory(self) -> None:
        path = filedialog.askopenfilename(title="Choisir inventaire.json", filetypes=[("Inventaire", "inventaire.json"), ("JSON", "*.json")])
        if path:
            try:
                self.session.load_inventory(path)
            except (OSError, ValueError, KeyError) as exc:
                return messagebox.showerror("swap", f"Fichier illisible : {exc}")
            self._after_inventory()
            self.v_scan_status.set(f"Analyse chargée : {path}")

    def _after_inventory(self) -> None:
        self.lbl_source.config(text=f"{self.session.machine}  (ce poste)")
        self._set_text(self.txt_summary, "\n".join(self.session.summary_lines()))
        self.btn_report.config(state="normal" if os.path.exists(self.session.report_path()) else "disabled")
        self._refresh_tree()
        self.txt_rules.delete("1.0", "end")
        self.txt_rules.insert("1.0", self.session.rules_text)

    # ------------------------------------------------------------------------------------------------------------
    # 2. Choisir
    def _build_choose_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(tab, text="2. Choisir")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Filtrer :").pack(side="left")
        self.v_filter = tk.StringVar()
        self.v_filter.trace_add("write", lambda *_: self._refresh_tree())
        ttk.Entry(top, textvariable=self.v_filter, width=28).pack(side="left", padx=6)
        for text, cmd in (("Tout cocher", lambda: self._select(True)), ("Tout décocher", lambda: self._select(False)),
                          ("Sélection par défaut", self._select_defaults)):
            ttk.Button(top, text=text, command=cmd).pack(side="left", padx=3)
        self.v_totals = tk.StringVar()
        ttk.Label(top, textvariable=self.v_totals, style="Head.TLabel").pack(side="right")

        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True, pady=PAD)
        cols = ("check", "label", "category", "size", "files", "path")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (("check", "", 34, "center"), ("label", "Élément", 300, "w"), ("category", "Catégorie", 190, "w"),
                                         ("size", "Taille", 80, "e"), ("files", "Fichiers", 70, "e"), ("path", "Emplacement", 380, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col in ("label", "path")))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("off", foreground="#888")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<space>", lambda e: self._toggle_selected())

        self.v_detail = tk.StringVar(value="Cliquez sur ☐/☑ (ou double-clic) pour cocher. Sélectionnez une ligne pour voir le détail.")
        ttk.Label(tab, textvariable=self.v_detail, wraplength=1040, justify="left").pack(anchor="w")

        opts = ttk.LabelFrame(tab, text="Options", padding=PAD)
        opts.pack(fill="x", pady=(PAD, 0))
        left = ttk.Frame(opts)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Règles de destination sur le PC cible (une par ligne : motif = dossier) — ex. pcsoft-projet = C:\\Mes Projets",
                  wraplength=620).pack(anchor="w")
        self.txt_rules = tk.Text(left, height=3, width=70, font="TkFixedFont")
        self.txt_rules.pack(fill="x", pady=(2, 0))
        self.txt_rules.bind("<KeyRelease>", lambda e: self._sync_options())
        self.txt_rules.bind("<FocusOut>", lambda e: self._sync_options())
        right = ttk.Frame(opts)
        right.pack(side="left", padx=(PAD * 2, 0))
        ttk.Label(right, text="Ne pas copier ces fichiers (ex. *.iso *.msu)").pack(anchor="w")
        self.v_excl = tk.StringVar()
        self.v_excl.trace_add("write", lambda *_: self._sync_options())
        ttk.Entry(right, textvariable=self.v_excl, width=34).pack(anchor="w", pady=(2, 0))
        ttk.Button(tab, text="Suivant : envoyer →", style="Big.TButton", command=lambda: self.nb.select(2)).pack(anchor="e", pady=(PAD, 0))

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for item in sorted(self.session.filtered(self.v_filter.get()), key=lambda i: (category_rank(i.category), i.label.lower())):
            on = item.id in self.session.selected
            size = "registre" if item.kind == "registry" else human_size(item.size)
            self.tree.insert("", "end", iid=item.id, tags=() if on else ("off",), values=(
                CHECKED if on else UNCHECKED, ("🔒 " if item.sensitive else "") + item.label, item.category, size, item.files or "", item.src))
        self._update_totals()

    def _update_totals(self) -> None:
        self.v_totals.set(f"{len(self.session.selected)} coché(s) sur {len(self.session.items)} — {human_size(self.session.total_size())}")

    def _toggle(self, item_id: str) -> None:
        self.session.toggle(item_id)
        on = item_id in self.session.selected
        self.tree.set(item_id, "check", CHECKED if on else UNCHECKED)
        self.tree.item(item_id, tags=() if on else ("off",))
        self._update_totals()

    def _toggle_selected(self) -> None:
        for iid in self.tree.selection():
            self._toggle(iid)

    def _on_tree_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            row = self.tree.identify_row(event.y)
            if row:
                self._toggle(row)

    def _on_tree_double(self, event) -> str:
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_column(event.x) != "#1":
            self._toggle(row)
        return "break"

    def _on_tree_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        item = next((i for i in self.session.items if i.id == sel[0]), None)
        if item:
            extra = f"\n{item.note}" if item.note else ""
            self.v_detail.set(f"{item.label}  —  {item.src}\nIdentifiant de règle : {item.id}{extra}")

    def _select(self, value: bool) -> None:
        self.session.select_all(value)
        self._refresh_tree()

    def _select_defaults(self) -> None:
        self.session.select_defaults()
        self._refresh_tree()

    # ------------------------------------------------------------------------------------------------------------
    # 3. Envoyer
    def _build_send_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(tab, text="3. Envoyer")
        box = ttk.LabelFrame(tab, text="Où envoyer ?", padding=PAD)
        box.pack(fill="x")

        r1 = ttk.Radiobutton(box, text="Vers le PC cible, connexion directe (le PC cible lance swap → « Recevoir » et affiche un code)",
                             variable=self.v_mode, value="direct", command=self._sync_mode)
        r1.grid(row=0, column=0, columnspan=6, sticky="w")
        ttk.Label(box, text="Code affiché sur le PC cible :").grid(row=1, column=0, sticky="e", padx=(24, 4), pady=2)
        self.ent_code = ttk.Entry(box, textvariable=self.v_code, width=16, font=self.f_mono)
        self.ent_code.grid(row=1, column=1, sticky="w")
        ttk.Label(box, text="Port :").grid(row=1, column=2, sticky="e", padx=(12, 4))
        self.ent_port = ttk.Entry(box, textvariable=self.v_port, width=7)
        self.ent_port.grid(row=1, column=3, sticky="w")

        r2 = ttk.Radiobutton(box, text="Vers le PC cible, partage Windows (rien à lancer sur le PC cible ; il faut être administrateur dessus)",
                             variable=self.v_mode, value="share", command=self._sync_mode)
        r2.grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Label(box, text="Partage :").grid(row=3, column=0, sticky="e", padx=(24, 4), pady=2)
        self.ent_share = ttk.Entry(box, textvariable=self.v_share, width=8)
        self.ent_share.grid(row=3, column=1, sticky="w")
        ttk.Label(box, text="Dossier :").grid(row=3, column=2, sticky="e", padx=(12, 4))
        self.ent_sub = ttk.Entry(box, textvariable=self.v_subfolder, width=14)
        self.ent_sub.grid(row=3, column=3, sticky="w")
        self.lbl_unc = ttk.Label(box, foreground="#555")
        self.lbl_unc.grid(row=3, column=4, sticky="w", padx=12)

        r3 = ttk.Radiobutton(box, text="Vers un disque externe ou un dossier", variable=self.v_mode, value="folder", command=self._sync_mode)
        r3.grid(row=4, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.ent_folder = ttk.Entry(box, textvariable=self.v_folder, width=52)
        self.ent_folder.grid(row=5, column=0, columnspan=4, sticky="we", padx=(24, 4), pady=2)
        self.btn_browse = ttk.Button(box, text="Parcourir…", command=lambda: self._browse(self.v_folder))
        self.btn_browse.grid(row=5, column=4, sticky="w")
        for var in (self.v_host, self.v_share, self.v_subfolder):
            var.trace_add("write", lambda *_: self._sync_mode())

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=PAD)
        ttk.Checkbutton(row, text="Vérifier chaque fichier (empreinte SHA-256, plus lent)", variable=self.v_hash).pack(side="left")
        ttk.Checkbutton(row, text="Simulation (rien n'est envoyé)", variable=self.v_dry).pack(side="left", padx=16)
        self.btn_test = ttk.Button(row, text="Tester la destination", command=self.on_test_destination)
        self.btn_test.pack(side="right")

        row2 = ttk.Frame(tab)
        row2.pack(fill="x")
        self.btn_send = ttk.Button(row2, text="Envoyer", style="Big.TButton", command=self.on_send)
        self.btn_send.pack(side="left")
        self.btn_send_cancel = ttk.Button(row2, text="Annuler", command=self.on_cancel, state="disabled")
        self.btn_send_cancel.pack(side="left", padx=6)
        self.send_bar = ttk.Progressbar(tab, mode="determinate", maximum=100)
        self.send_bar.pack(fill="x", pady=(PAD, 0))
        self.v_send_status = tk.StringVar(value="Prêt.")
        ttk.Label(tab, textvariable=self.v_send_status).pack(anchor="w", pady=(2, PAD))
        self.txt_send_log = self._text(tab, height=10)
        self.ui_send = SimpleNamespace(bar=self.send_bar, status=self.v_send_status, log=self.txt_send_log, cancel=self.btn_send_cancel,
                                       actions=[self.btn_send, self.btn_test], done=self._send_done)
        self._sync_mode()

    def _sync_mode(self) -> None:
        mode = self.v_mode.get()
        for widgets, name in (([self.ent_code, self.ent_port], "direct"), ([self.ent_share, self.ent_sub], "share"),
                              ([self.ent_folder, self.btn_browse], "folder")):
            for w in widgets:
                w.config(state="normal" if mode == name else "disabled")
        self.lbl_unc.config(text=net.unc_path(self.v_host.get() or "TPSELxxx", self.v_share.get() or "C$", self.v_subfolder.get()) if mode == "share" else "")

    def _browse(self, var: tk.StringVar) -> None:
        folder = filedialog.askdirectory(title="Choisir un dossier")
        if folder:
            var.set(os.path.normpath(folder))

    def _dest_kwargs(self) -> dict:
        try:
            port = int(self.v_port.get())
        except ValueError:
            raise SinkError("Le port doit être un nombre.") from None
        mode = self.v_mode.get()
        if mode == "folder" and not self.v_folder.get().strip():
            raise SinkError("Choisissez le dossier ou le disque de destination.")
        if mode in ("direct", "share") and not self.v_host.get().strip():
            raise SinkError("Saisissez le nom (ou l'adresse) du PC cible en haut de la fenêtre.")
        if mode == "direct" and not net.normalize_code(self.v_code.get()):
            raise SinkError("Saisissez le code affiché sur le PC cible (onglet « Recevoir »).")
        return dict(host=self.v_host.get(), code=self.v_code.get(), port=port, folder=self.v_folder.get(),
                    share=self.v_share.get() or "C$", subfolder=self.v_subfolder.get())

    def on_test_destination(self) -> None:
        try:
            kw, mode = self._dest_kwargs(), self.v_mode.get()
        except SinkError as exc:
            return messagebox.showwarning("swap", str(exc))
        self._log(self.txt_send_log, "Test de la destination...")
        self.start_job(lambda ctx: self.session.test_destination(mode, **kw), SimpleNamespace(
            bar=self.send_bar, status=self.v_send_status, log=self.txt_send_log, cancel=None,
            actions=[self.btn_send, self.btn_test], done=self._test_done))

    def _test_done(self, job: Job) -> None:
        if job.error:
            self._log(self.txt_send_log, f"✘ {job.error}")
            self.v_send_status.set("Destination inaccessible.")
            messagebox.showerror("Destination inaccessible", str(job.error))
        else:
            self._log(self.txt_send_log, f"✔ {job.result}")
            self.v_send_status.set("Destination accessible.")

    def _sync_options(self) -> None:
        """Relit les règles et exclusions saisies (au cas où la dernière frappe n'a pas déclenché d'événement)."""
        self.session.rules_text = self.txt_rules.get("1.0", "end").strip()
        self.session.exclude_text = self.v_excl.get()

    def on_send(self) -> None:
        self._sync_options()
        if not self.session.inv:
            messagebox.showinfo("swap", "Analysez d'abord ce poste (onglet 1).")
            return self.nb.select(0)
        try:
            kw, mode = self._dest_kwargs(), self.v_mode.get()
        except SinkError as exc:
            return messagebox.showwarning("swap", str(exc))
        items = self.session.selected_items()
        if not items:
            return messagebox.showwarning("swap", "Rien n'est coché dans l'onglet « Choisir ».")
        where = {"direct": f"le PC {kw['host']} (connexion directe)", "share": net.unc_path(kw["host"], kw["share"], kw["subfolder"]),
                 "folder": kw["folder"]}[mode]
        warn = "\n\n🔒 La copie contient des données sensibles (clés SSH, sessions…)." if any(i.sensitive for i in items) else ""
        if not self.v_dry.get() and not messagebox.askokcancel(
                "Envoyer ?", f"{len(items)} élément(s), {human_size(self.session.total_size())}\nDe : {self.session.machine}\nVers : {where}"
                f"{warn}\n\nLes fichiers déjà présents et identiques ne sont pas renvoyés ; vous pouvez interrompre et reprendre."):
            return
        self._set_text(self.txt_send_log, "")
        dry, want_hash = self.v_dry.get(), self.v_hash.get()

        def work(ctx):
            ctx.progress("Connexion...")
            sink = self.session.open_sink(mode, **kw)
            self._log_from_thread(ctx, f"Destination : {sink.describe()}")
            return self.session.send(ctx, sink, dry_run=dry, want_hash=want_hash)

        self.start_job(work, self.ui_send)

    def _send_done(self, job: Job) -> None:
        if job.cancelled:
            self.v_send_status.set("Envoi annulé. Relancez « Envoyer » pour reprendre là où il s'est arrêté.")
            return self._log(self.txt_send_log, "Envoi annulé.")
        if job.error:
            self.v_send_status.set("Envoi interrompu.")
            self._log(self.txt_send_log, f"✘ {job.error}")
            if isinstance(job.error, SinkError):
                return messagebox.showerror("Envoi interrompu", f"{job.error}\n\nRelancez « Envoyer » : seuls les fichiers manquants seront renvoyés.")
            return self._show_error("Erreur pendant l'envoi", job)
        summary = job.result
        copied = sum(r.get("copied", 0) for r in summary["items"].values())
        same = sum(r.get("unchanged", 0) for r in summary["items"].values())
        errors = summary["errors"]
        for line in errors[:50]:
            self._log(self.txt_send_log, f"⚠ {line}")
        msg = f"{'Simulation terminée' if self.v_dry.get() else 'Envoi terminé'} : {copied} fichier(s) copié(s), {same} déjà à jour"
        msg += f", {len(errors)} non copié(s) (voir le journal)." if errors else "."
        self.v_send_status.set(msg)
        self._log(self.txt_send_log, msg)
        if not self.v_dry.get():
            self._log(self.txt_send_log, f"Sauvegarde : {summary['backup']}")
            self._log(self.txt_send_log, "Étape suivante, sur le PC cible : onglet « Recevoir / restaurer » (ou RESTAURER.bat dans le dossier reçu).")
            if self.v_mode.get() in ("folder", "share"):
                self.v_backup.set(summary["backup"])
            messagebox.showinfo("Envoi terminé", msg + "\n\nSur le PC cible : swap → « Recevoir / restaurer » → Restaurer.")

    # ------------------------------------------------------------------------------------------------------------
    # 4. Recevoir / restaurer (PC cible)
    def _build_receive_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=PAD)
        self.nb.add(tab, text="4. Recevoir / restaurer  (sur le PC cible)")
        rx = ttk.LabelFrame(tab, text="Recevoir depuis un autre PC", padding=PAD)
        rx.pack(fill="x")
        ttk.Label(rx, text="Ranger ce qui est reçu dans :").grid(row=0, column=0, sticky="e")
        ttk.Entry(rx, textvariable=self.v_rx_dest, width=44).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(rx, text="Parcourir…", command=lambda: self._browse(self.v_rx_dest)).grid(row=0, column=2)
        ttk.Label(rx, text="Port :").grid(row=0, column=3, sticky="e", padx=(12, 4))
        ttk.Entry(rx, textvariable=self.v_rx_port, width=7).grid(row=0, column=4)
        ttk.Checkbutton(rx, text="Ouvrir ce port dans le pare-feu Windows pendant la réception (droits administrateur)",
                        variable=self.v_rx_fw).grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self.btn_rx = ttk.Button(rx, text="Démarrer la réception", style="Big.TButton", command=self.on_rx_toggle)
        self.btn_rx.grid(row=2, column=0, columnspan=2, sticky="w", pady=PAD)
        self.v_rx_info = tk.StringVar(value="")
        ttk.Label(rx, textvariable=self.v_rx_info, justify="left").grid(row=3, column=0, columnspan=5, sticky="w")
        self.v_rx_code = tk.StringVar(value="")
        ttk.Label(rx, textvariable=self.v_rx_code, style="Code.TLabel").grid(row=4, column=0, columnspan=5, sticky="w")
        self.txt_rx_log = self._text(rx, height=3, pack=False)
        self.txt_rx_log._frame.grid(row=5, column=0, columnspan=5, sticky="we", pady=(4, 0))

        rs = ttk.LabelFrame(tab, text="Restaurer sur ce PC", padding=PAD)
        rs.pack(fill="both", expand=True, pady=(PAD, 0))
        ttk.Label(rs, text="Dossier de la sauvegarde (SWAP-…) :").grid(row=0, column=0, sticky="e")
        ttk.Entry(rs, textvariable=self.v_backup, width=54).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(rs, text="Parcourir…", command=self._browse_backup).grid(row=0, column=2)
        ttk.Checkbutton(rs, text="Écraser les fichiers déjà présents (sinon ils sont conservés)", variable=self.v_overwrite).grid(
            row=1, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(rs, text="Simulation (montre où irait chaque élément)", variable=self.v_restore_dry).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(rs, text="Règles de destination supplémentaires (motif = dossier) :").grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.txt_restore_rules = tk.Text(rs, height=2, width=70, font="TkFixedFont")
        self.txt_restore_rules.grid(row=4, column=0, columnspan=3, sticky="we")
        row = ttk.Frame(rs)
        row.grid(row=5, column=0, columnspan=3, sticky="w", pady=PAD)
        self.btn_restore = ttk.Button(row, text="Restaurer", style="Big.TButton", command=self.on_restore)
        self.btn_restore.pack(side="left")
        self.btn_restore_cancel = ttk.Button(row, text="Annuler", command=self.on_cancel, state="disabled")
        self.btn_restore_cancel.pack(side="left", padx=6)
        self.btn_script = ttk.Button(row, text="Lancer le script d'installation des applications…", command=self.on_run_script)
        self.btn_script.pack(side="left", padx=(24, 0))
        ttk.Button(row, text="Ouvrir le rapport", command=self.on_open_backup_report).pack(side="left", padx=6)
        self.restore_bar = ttk.Progressbar(rs, mode="determinate", maximum=100)
        self.restore_bar.grid(row=6, column=0, columnspan=3, sticky="we")
        self.v_restore_status = tk.StringVar(value="")
        ttk.Label(rs, textvariable=self.v_restore_status).grid(row=7, column=0, columnspan=3, sticky="w")
        self.txt_restore_log = self._text(rs, height=8, pack=False)
        self.txt_restore_log._frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
        rs.rowconfigure(8, weight=1, minsize=120)
        rs.columnconfigure(1, weight=1)
        self.ui_restore = SimpleNamespace(bar=self.restore_bar, status=self.v_restore_status, log=self.txt_restore_log,
                                          cancel=self.btn_restore_cancel, actions=[self.btn_restore], done=self._restore_done)

    def _browse_backup(self) -> None:
        folder = filedialog.askdirectory(title="Choisir le dossier de la sauvegarde")
        if folder:
            found = find_backups(folder)
            self.v_backup.set(os.path.normpath(found[0] if found else folder))

    def on_rx_toggle(self) -> None:
        if self.rx is not None:
            return self._rx_stop()
        try:
            port = int(self.v_rx_port.get())
            self.rx = net.Receiver(self.v_rx_dest.get(), None, port, self.rx_events.put)
        except (ValueError, OSError) as exc:
            self.rx = None
            return messagebox.showerror("Réception impossible", f"Impossible d'écouter sur ce port : {exc}")
        self.rx_firewall = False
        if self.v_rx_fw.get():
            ok, msg = net.firewall_open(self.rx.port)
            self.rx_firewall = ok
            self._log(self.txt_rx_log, "Pare-feu : port ouvert." if ok else
                      f"Pare-feu : impossible d'ouvrir le port ({msg or 'droits administrateur requis'}). Ouvrez-le à la main si la connexion échoue.")
        threading.Thread(target=self.rx.serve, kwargs={"once": True}, daemon=True).start()
        addrs = ", ".join(net.local_addresses()) or "?"
        self.v_rx_info.set(f"Ce PC : {os.environ.get('COMPUTERNAME') or self._hostname()}   —   adresses : {addrs}   —   port {self.rx.port}\n"
                           "Sur le PC source : saisissez ce nom (en haut) et ce code, onglet « Envoyer ».")
        self.v_rx_code.set(self.rx.code)
        self.btn_rx.config(text="Arrêter la réception")

    @staticmethod
    def _hostname() -> str:
        import socket

        return socket.gethostname()

    def _rx_stop(self) -> None:
        if self.rx:
            self.rx.stop()
            if self.rx_firewall:
                net.firewall_close(self.rx.port)
        self.rx, self.rx_firewall = None, False
        self.btn_rx.config(text="Démarrer la réception")
        self.v_rx_code.set("")
        self.v_rx_info.set("")

    def _poll_receiver(self) -> None:
        try:
            while True:
                e = self.rx_events.get_nowait()
                kind = e["kind"]
                if kind == "connected":
                    self._log(self.txt_rx_log, f"Connexion de {e['machine']} ({e['peer']}), utilisateur {e['user']}")
                elif kind == "file":
                    self.v_rx_info.set(f"Réception en cours : {e['files']} fichier(s), {human_size(e['bytes'])}")
                elif kind in ("refused", "aborted"):
                    self._log(self.txt_rx_log, f"{'Connexion refusée' if kind == 'refused' else 'Transfert interrompu'} ({e['peer']}) : {e['error']}")
                elif kind == "done":
                    self._log(self.txt_rx_log, f"Terminé : {e['files']} fichier(s), {human_size(e['bytes'])} dans {e['root']}")
                    self.v_backup.set(e["root"])
                    messagebox.showinfo("Réception terminée", f"{e['files']} fichier(s) reçus dans\n{e['root']}\n\nVous pouvez lancer « Restaurer ».")
                if kind in ("done", "stopped"):
                    self._rx_stop()
        except queue.Empty:
            pass
        self._rx_poll_id = self.after(150, self._poll_receiver)

    def on_restore(self) -> None:
        backup = self.v_backup.get().strip().strip('"')
        if not backup or not os.path.exists(os.path.join(backup, "manifest.json")):
            found = find_backups(backup) if backup and os.path.isdir(backup) else []
            if len(found) == 1:
                backup = found[0]
                self.v_backup.set(backup)
            else:
                return messagebox.showwarning("swap", "Indiquez le dossier de la sauvegarde (celui qui contient manifest.json).")
        dry, over = self.v_restore_dry.get(), self.v_overwrite.get()
        rules = self.txt_restore_rules.get("1.0", "end")
        if not dry and not messagebox.askokcancel("Restaurer ?", "Fermez d'abord Outlook, les navigateurs et VS Code.\n\n"
                                                   "Les fichiers déjà présents sur ce PC ne sont " + ("PAS conservés (écrasés)." if over else "jamais écrasés.")):
            return
        self._set_text(self.txt_restore_log, "")
        def work(ctx):
            ctx.progress("Restauration...")
            return self.session.restore(ctx, backup, overwrite=over, dry_run=dry, rules_text=rules)

        self.start_job(work, self.ui_restore)

    def _restore_done(self, job: Job) -> None:
        if job.cancelled:
            return self.v_restore_status.set("Restauration annulée (les fichiers déjà restaurés restent en place).")
        if job.error:
            self.v_restore_status.set("La restauration a échoué.")
            return self._show_error("La restauration a échoué", job)
        res = job.result
        for item_id, r in res["items"].items():
            self._log(self.txt_restore_log, f"{item_id}: {r['restored']} restauré(s), {r['skipped']} déjà présent(s)"
                      + (f", {len(r['errors'])} erreur(s)" if r["errors"] else "") + (f"  → {r['dest']}" if r.get("dest") else ""))
        for e in res["errors"][:30]:
            self._log(self.txt_restore_log, f"⚠ {e}")
        self.v_restore_status.set("Restauration terminée." + (f" {len(res['errors'])} erreur(s)." if res["errors"] else ""))
        self._log(self.txt_restore_log, "Étape suivante : « Lancer le script d'installation des applications ».")

    def on_run_script(self) -> None:
        script = os.path.join(self.v_backup.get().strip(), "installer_et_configurer.ps1")
        if not os.path.exists(script):
            return messagebox.showwarning("swap", "Script introuvable dans la sauvegarde.")
        if not is_windows():
            return messagebox.showinfo("swap", f"Le script est prévu pour Windows PowerShell :\n{script}")
        if messagebox.askokcancel("Lancer le script ?", "Il va recréer les lecteurs réseau et imprimantes, ajouter des variables d'environnement, "
                                  "installer les applications (winget) puis lancer les installateurs récupérés (assistants à valider).\n\n"
                                  f"Vous pouvez le relire avant : {script}"):
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", script],
                             creationflags=subprocess.CREATE_NEW_CONSOLE)

    def on_open_backup_report(self) -> None:
        path = os.path.join(self.v_backup.get().strip(), "rapport.html")
        open_path(path) if os.path.exists(path) else messagebox.showinfo("swap", "Pas de rapport dans ce dossier.")

    # ------------------------------------------------------------------------------------------------------------
    # tâches en arrière-plan
    def start_job(self, fn, ui: SimpleNamespace) -> None:
        if self.job is not None and not self.job.finished.is_set():
            return messagebox.showinfo("swap", "Une opération est déjà en cours.")
        self.job = job = Job(fn)
        for b in ui.actions:
            b.config(state="disabled")
        if ui.cancel is not None:
            ui.cancel.config(state="normal")
        ui.bar.config(mode="indeterminate")
        ui.bar.start(12)
        ui.status.set("En cours...")
        started, state = time.monotonic(), {"logged": 0, "mode": "indeterminate"}
        job.start()

        def poll():
            ctx = job.ctx
            while state["logged"] < len(ctx.log):
                if ui.log is not None:
                    self._log(ui.log, ctx.log[state["logged"]])
                state["logged"] += 1
            total, done = ctx.total_bytes, ctx.done_bytes
            if total > 0:
                if state["mode"] != "determinate":
                    ui.bar.stop()
                    ui.bar.config(mode="determinate", maximum=100)
                    state["mode"] = "determinate"
                ui.bar["value"] = min(100, done * 100 / total)
                elapsed = max(time.monotonic() - started, 0.001)
                speed = done / elapsed
                left = f" — reste ~{fmt_duration((total - done) / speed)}" if speed > 0 and done < total else ""
                ui.status.set(f"{ctx.message}\n{human_size(done)} / {human_size(total)} — {human_size(speed)}/s{left}")
            elif ctx.message:
                ui.status.set(ctx.message)
            if job.finished.is_set():
                ui.bar.stop()
                ui.bar.config(mode="determinate")
                ui.bar["value"] = 100 if (not job.error and not job.cancelled) else 0
                for b in ui.actions:
                    b.config(state="normal")
                if ui.cancel is not None:
                    ui.cancel.config(state="disabled")
                ui.done(job)
                return
            self.after(120, poll)

        self.after(120, poll)

    def on_cancel(self) -> None:
        if self.job is not None and not self.job.finished.is_set():
            self.job.cancel()

    # ------------------------------------------------------------------------------------------------------------
    # utilitaires d'affichage
    def _text(self, parent, height: int = 8, pack: bool = True) -> tk.Text:
        """Zone de texte en lecture seule avec ascenseur. Si pack=False, l'appelant place `txt._frame` lui-même."""
        frame = ttk.Frame(parent)
        txt = tk.Text(frame, height=height, wrap="word", state="disabled", font="TkFixedFont", background="#fafafa")
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt._frame = frame  # type: ignore[attr-defined]
        if pack:
            frame.pack(fill="both", expand=True)
        return txt

    def _set_text(self, widget: tk.Text, content: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.config(state="disabled")

    def _log(self, widget: tk.Text, line: str) -> None:
        widget.config(state="normal")
        widget.insert("end", line + "\n")
        widget.see("end")
        widget.config(state="disabled")

    @staticmethod
    def _log_from_thread(ctx, line: str) -> None:
        ctx.say(line)

    def _show_error(self, title: str, job: Job) -> None:
        detail = job.trace.strip().splitlines()[-1] if job.trace else str(job.error)
        messagebox.showerror(title, f"{job.error}\n\n{detail}")

    def report_callback_exception(self, exc, val, tb) -> None:
        detail = "".join(traceback.format_exception(exc, val, tb))
        print(detail, file=sys.stderr)
        messagebox.showerror("swap — erreur inattendue", detail[-1500:])

    def _on_close(self) -> None:
        if self.job is not None and not self.job.finished.is_set():
            if not messagebox.askokcancel("swap", "Une opération est en cours. Quitter l'interrompt (vous pourrez reprendre ensuite)."):
                return
            self.job.cancel()
        self._rx_stop()
        self.destroy()


def run(out_dir: Optional[str] = None, backup: Optional[str] = None) -> int:
    try:
        app = App(Session(out_dir) if out_dir else None, backup)
        app.mainloop()
        return 0
    except Exception:  # noqa: BLE001 - dernier filet : montrer l'erreur plutôt que de fermer en silence (lancement sans console)
        detail = traceback.format_exc()
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("swap — erreur", detail[-1500:])
        except tk.TclError:
            print(detail, file=sys.stderr)
        return 1
