"""Interface web locale : un petit serveur HTTP sur 127.0.0.1 qui sert une page unique (webui/index.html).

Rien à installer : bibliothèque standard uniquement. Le serveur n'écoute QUE sur cette machine et exige un jeton aléatoire
(dans l'adresse ouverte au démarrage) : un autre programme ou un site web ne peut pas le piloter.
`WebApp` contient toute la logique (testable sans HTTP) ; `Handler` ne fait que traduire HTTP <-> JSON.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import platform
import queue
import secrets
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, net, transfer
from .locations import profile_label
from .session import Job, Session, category_rank, find_backups
from .sink import SinkError
from .util import human_size, is_windows

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "webui", "index.html")
LOG_CAP = 20000  # lignes de journal gardées en mémoire


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _int(value, default: int, name: str = "valeur") -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        raise ApiError(f"Le champ « {name} » doit être un nombre.") from None


class WebApp:
    """État et actions de l'interface. Chaque méthode publique `api_*` reçoit un dict JSON et renvoie un dict JSON."""

    def __init__(self, session: Optional[Session] = None, backup: Optional[str] = None, server: bool = False):
        self.session = session or Session(os.path.abspath("swap-sortie"))
        self.server_mode = server   # hébergé sur un serveur : le PC source et le PC cible sont toujours d'AUTRES postes
        self.initial_backup = os.path.normpath(backup) if backup else ""
        self.lock = threading.RLock()
        self.job: Optional[Job] = None
        self.job_kind = ""
        self.job_started = 0.0
        self.job_view: Optional[dict] = None   # résultat mis en forme, calculé une fois la tâche finie
        self.job_dry = False
        self.job_seq = 0
        self.rx: Optional[net.Receiver] = None
        self.rx_events: "queue.Queue[dict]" = queue.Queue()
        self.rx_firewall = False
        self.rx_log: list = []
        self.rx_done_root = ""
        self.rx_progress = ""
        self.last_activity = time.monotonic()
        self.quit_event = threading.Event()

    # ------------------------------------------------------------------------------------------------------------
    # état général
    def busy(self) -> bool:
        return bool((self.job is not None and not self.job.finished.is_set()) or self.rx is not None)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def _profile_rows(self) -> list:
        return [{"name": p["name"], "path": p["path"], "current": p["current"], "label": profile_label(p)}
                for p in self.session.profiles()]

    def api_state(self, _p=None) -> dict:
        s = self.session
        meta = s.inv["meta"] if s.inv else {}
        profiles = self._profile_rows()
        chosen = next((p for p in profiles if p["path"] == s.source_home), None) or next((p for p in profiles if p["current"]), None)
        out = {
            "version": __version__, "machine": platform.node(), "windows": is_windows(), "out_dir": os.path.abspath(s.out_dir),
            "profiles": profiles, "source": chosen["path"] if chosen else "", "source_is_current": not s.source_home,
            "rules": s.rules_text, "excludes": s.exclude_text, "last_backup": s.last_backup, "initial_backup": self.initial_backup,
            "has_inventory": bool(s.inv), "inventory": None, "server": self.server_mode,
            "source_host": s.source_host, "source_user": s.source_user,
            "default_rx_dest": "C:\\SWAP" if is_windows() else os.path.expanduser("~/SWAP"), "port": net.DEFAULT_PORT,
        }
        if s.inv:
            out["inventory"] = {
                "machine": meta.get("machine", ""), "user": meta.get("user", ""), "date": meta.get("date", ""), "os": meta.get("os", ""),
                "profile_current": meta.get("profile_current", True),
                "items": len(s.items), "selected": len(s.selected), "size": s.total_size(), "size_h": human_size(s.total_size()),
                "apps": len([a for a in s.inv["apps"] if not a.get("component")]),
                "advice": [{"level": a["level"], "text": a["text"]} for a in s.inv["advice"]],
                "report": os.path.exists(s.report_path()),
            }
        return out

    def api_items(self, p=None) -> dict:
        s = self.session
        rows = []
        for it in sorted(s.items, key=lambda i: (category_rank(i.category), i.label.lower())):
            rows.append({"id": it.id, "label": it.label, "category": it.category, "kind": it.kind, "src": it.src, "size": it.size,
                         "size_h": "registre" if it.kind == "registry" else human_size(it.size), "files": it.files,
                         "sensitive": it.sensitive, "note": it.note, "cloud": it.cloud_files, "selected": it.id in s.selected})
        return {"items": rows, **self._totals()}

    def _totals(self) -> dict:
        s = self.session
        return {"selected": len(s.selected), "total": len(s.items), "size": s.total_size(), "size_h": human_size(s.total_size())}

    # ------------------------------------------------------------------------------------------------------------
    # profils, analyse, sélection
    def api_profiles(self, _p=None) -> dict:
        return {"profiles": self._profile_rows()}

    def api_set_source(self, p) -> dict:
        """PC source : `source_host` (vide = ce poste) + `source_user` pour un autre PC, ou `path` = profil de ce poste."""
        path = (p.get("path") or "").strip()
        prof = next((x for x in self.session.profiles() if x["path"] == path), None)
        home = "" if (prof is None or prof["current"]) else prof["path"]
        host = (p.get("source_host") or "").strip()
        if self.server_mode and not host:
            raise ApiError("Saisissez le nom du PC source.")
        self.session.set_source(host, p.get("source_user") or "", home)
        return {"source_is_current": not (self.session.source_home or self.session.source_host)}

    def api_scan(self, p) -> dict:
        self.api_set_source(p)
        self._start("scan", self.session.scan)
        return {"started": True}

    def api_load_inventory(self, p) -> dict:
        path = (p.get("path") or "").strip().strip('"')
        try:
            self.session.load_inventory(path)
        except (OSError, ValueError, KeyError) as exc:
            raise ApiError(f"Fichier illisible : {exc}") from None
        self.session.load_rules_text()
        return {"loaded": True}

    def api_select(self, p) -> dict:
        s = self.session
        if p.get("defaults"):
            s.select_defaults()
        elif "all" in p:
            s.select_all(bool(p["all"]))
        else:
            ids = set(p.get("ids") or [])
            known = {i.id for i in s.items}
            if p.get("value"):
                s.selected |= ids & known
            else:
                s.selected -= ids
        return self._totals()

    def api_options(self, p) -> dict:
        if "rules" in p:
            self.session.rules_text = str(p["rules"]).strip()
        if "excludes" in p:
            self.session.exclude_text = str(p["excludes"])
        return {"ok": True}

    # ------------------------------------------------------------------------------------------------------------
    # tâches de fond
    def _start(self, kind: str, fn) -> None:
        with self.lock:
            if self.job is not None and not self.job.finished.is_set():
                raise ApiError("Une opération est déjà en cours : attendez la fin ou annulez-la.", 409)
            self.job, self.job_kind, self.job_started, self.job_view = Job(fn), kind, time.monotonic(), None
            self.job_seq += 1
            self.job.start()

    def api_cancel(self, _p=None) -> dict:
        if self.job is not None and not self.job.finished.is_set():
            self.job.cancel()
        return {"ok": True}

    def api_status(self, p) -> dict:
        """Tâche en cours (avec les nouvelles lignes de journal depuis `since`) et réception."""
        out = {"job": self._job_status(_int(p.get("since"), 0, "since")), "rx": self._rx_status()}
        return out

    def _job_status(self, since: int) -> Optional[dict]:
        job = self.job
        if job is None:
            return None
        ctx = job.ctx
        total, done = ctx.total_bytes, ctx.done_bytes
        elapsed = max(time.monotonic() - self.job_started, 0.001)
        speed = done / elapsed if total > 0 else 0
        running = not job.finished.is_set()
        log = list(ctx.log)
        view = {
            "id": self.job_seq, "kind": self.job_kind, "running": running, "cancelled": job.cancelled, "message": ctx.message,
            "done": done, "total": total, "done_h": human_size(done), "total_h": human_size(total), "speed": speed,
            "speed_h": human_size(speed) + "/s", "eta": int((total - done) / speed) if speed > 0 and done < total else None,
            "elapsed": int(elapsed), "log": log[since:since + 500], "log_total": len(log), "error": None, "error_kind": "", "result": None,
        }
        if not running:
            if job.error is not None:
                view["error"] = str(job.error) or job.error.__class__.__name__
                view["error_kind"] = "sink" if isinstance(job.error, SinkError) else "other"
                view["trace"] = job.trace if view["error_kind"] == "other" else ""
            elif not job.cancelled:
                with self.lock:
                    if self.job_view is None:
                        self.job_view = self._result_view(job)
                view["result"] = self.job_view
        return view

    def _result_view(self, job: Job) -> dict:
        res, kind = job.result, self.job_kind
        if kind == "scan":
            return {"ok": True}
        if kind == "test":
            return {"message": str(res)}
        if kind == "send":
            items = res["items"].values()
            return {"copied": sum(r.get("copied", 0) for r in items), "unchanged": sum(r.get("unchanged", 0) for r in items),
                    "errors": res["errors"][:50], "error_count": len(res["errors"]), "backup": res["backup"], "dry": self.job_dry,
                    "bytes": sum(r.get("bytes", 0) for r in items), "placed_in": res.get("placed_in", ""),
                    "report_dir": res.get("report_dir", ""), "skipped_registry": res.get("skipped_registry", [])}
        if kind == "restore":
            return {"items": [{"id": k, "restored": v["restored"], "skipped": v["skipped"], "errors": v["errors"][:5], "dest": v.get("dest", "")}
                              for k, v in res["items"].items()], "errors": res["errors"][:30], "error_count": len(res["errors"])}
        return {}

    # ------------------------------------------------------------------------------------------------------------
    # envoi
    def _dest_args(self, p) -> tuple:
        mode = p.get("mode") or "direct"
        host, folder = (p.get("host") or "").strip(), (p.get("folder") or "").strip()
        kw = dict(host=host, code=p.get("code") or "", port=_int(p.get("port"), net.DEFAULT_PORT, "port"), folder=folder,
                  share=(p.get("share") or "C$").strip() or "C$", subfolder=(p.get("subfolder") or "SWAP").strip())
        kw["user"] = (p.get("user") or "").strip()
        if mode not in ("remote", "direct", "share", "folder"):
            raise ApiError("Mode d'envoi inconnu.")
        if mode == "folder" and not folder:
            raise ApiError("Choisissez le dossier ou le disque de destination.")
        if mode in ("remote", "direct", "share") and not host:
            raise ApiError("Saisissez le nom (ou l'adresse) du PC cible, en haut de la page.")
        if mode == "remote" and not kw["user"]:
            raise ApiError("Choisissez l'utilisateur du PC cible dans lequel déposer les données (bouton « Charger les utilisateurs »).")
        if mode == "direct" and not net.normalize_code(kw["code"]):
            raise ApiError("Saisissez le code affiché sur le PC cible (étape « Recevoir »).")
        return mode, kw

    def api_test(self, p) -> dict:
        mode, kw = self._dest_args(p)
        self._start("test", lambda ctx: self.session.test_destination(mode, **kw))
        return {"started": True}

    def api_send(self, p) -> dict:
        s = self.session
        self.api_options(p)
        if not s.inv:
            raise ApiError("Analysez d'abord ce poste (étape 1).")
        mode, kw = self._dest_args(p)
        if not s.selected_items():
            raise ApiError("Rien n'est coché dans l'étape « Choisir ».")
        dry, want_hash = bool(p.get("dry")), bool(p.get("hash"))
        self.job_dry = dry

        overwrite = bool(p.get("overwrite"))

        def work_remote(ctx):
            ctx.progress("Connexion au PC cible...")
            summary = s.send_remote(ctx, kw["host"], kw["user"], dry_run=dry, want_hash=want_hash, overwrite=overwrite)
            for line in summary["errors"][:50]:
                ctx.say(f"⚠ {line}")
            return summary

        def work(ctx):
            ctx.progress("Connexion...")
            sink = s.open_sink(mode, **kw)
            ctx.say(f"Destination : {sink.describe()}")
            summary = s.send(ctx, sink, dry_run=dry, want_hash=want_hash)
            for line in summary["errors"][:50]:
                ctx.say(f"⚠ {line}")
            return summary

        self._start("send", work_remote if mode == "remote" else work)
        return {"started": True}

    def api_remote_users(self, p) -> dict:
        """Profils d'un autre PC (PC source ou PC cible), lus par \\\\PC\\C$\\Users."""
        host = (p.get("host") or "").strip()
        if not host:
            raise ApiError("Saisissez le nom (ou l'adresse) du PC cible, en haut de la page.")
        users = self.session.remote_users(host)
        return {"users": [{"name": u["name"], "last_used": u["last_used"]} for u in users]}

    # ------------------------------------------------------------------------------------------------------------
    # réception (PC cible)
    def api_rx_start(self, p) -> dict:
        if self.rx is not None:
            raise ApiError("La réception est déjà démarrée.", 409)
        port, dest = _int(p.get("port"), net.DEFAULT_PORT, "port"), (p.get("dest") or "").strip()
        if not dest:
            raise ApiError("Indiquez le dossier où ranger ce qui sera reçu.")
        try:
            self.rx = net.Receiver(dest, None, port, self.rx_events.put)
        except (ValueError, OSError) as exc:
            self.rx = None
            raise ApiError(f"Impossible d'écouter sur ce port : {exc}") from None
        self.rx_log, self.rx_done_root, self.rx_progress, self.rx_firewall = [], "", "", False
        if p.get("firewall", True):
            ok, msg = net.firewall_open(self.rx.port)
            self.rx_firewall = ok
            self.rx_log.append("Pare-feu : port ouvert." if ok else
                               f"Pare-feu : impossible d'ouvrir le port ({msg or 'droits administrateur requis'}). Ouvrez-le à la main si la connexion échoue.")
        threading.Thread(target=self.rx.serve, kwargs={"once": True}, daemon=True).start()
        return self._rx_status()

    def api_rx_stop(self, _p=None) -> dict:
        self._rx_stop()
        return self._rx_status()

    def _rx_stop(self) -> None:
        if self.rx is not None:
            self.rx.stop()
            if self.rx_firewall:
                net.firewall_close(self.rx.port)
        self.rx, self.rx_firewall = None, False

    def _rx_drain(self) -> None:
        while True:
            try:
                e = self.rx_events.get_nowait()
            except queue.Empty:
                return
            kind = e["kind"]
            if kind == "connected":
                self.rx_log.append(f"Connexion de {e['machine']} ({e['peer']}), utilisateur {e['user']}")
            elif kind == "file":
                self.rx_progress = f"Réception en cours : {e['files']} fichier(s), {human_size(e['bytes'])}"
            elif kind in ("refused", "aborted"):
                self.rx_log.append(f"{'Connexion refusée' if kind == 'refused' else 'Transfert interrompu'} ({e['peer']}) : {e['error']}")
            elif kind == "done":
                self.rx_log.append(f"Terminé : {e['files']} fichier(s), {human_size(e['bytes'])} dans {e['root']}")
                self.rx_done_root = e["root"]
                self.rx_progress = f"Réception terminée : {e['files']} fichier(s), {human_size(e['bytes'])}"
            if kind in ("done", "stopped"):
                self._rx_stop()
            del self.rx_log[:-LOG_CAP]

    def _rx_status(self) -> dict:
        self._rx_drain()
        rx = self.rx
        return {"active": rx is not None, "code": rx.code if rx else "", "port": rx.port if rx else 0,
                "addresses": net.local_addresses() if rx else [], "hostname": os.environ.get("COMPUTERNAME") or platform.node(),
                "log": list(self.rx_log), "progress": self.rx_progress, "done_root": self.rx_done_root}

    # ------------------------------------------------------------------------------------------------------------
    # restauration
    def api_backup_info(self, p) -> dict:
        path = (p.get("path") or "").strip().strip('"')
        out = {"backup": "", "found": [], "user": "", "machine": "", "created": "", "items": 0, "match": "", "report": False}
        if not path or not os.path.isdir(path):
            return out
        found = find_backups(path)
        out["found"] = found
        backup = found[0] if len(found) == 1 else ""
        if not backup:
            return out
        out["backup"] = backup
        try:
            m = transfer.load_manifest(backup)
        except (OSError, ValueError):
            return out
        user = m.get("user", "")
        out.update(user=user, machine=m.get("machine", ""), created=m.get("created", ""), items=len(m.get("items", [])),
                   report=os.path.exists(os.path.join(backup, "rapport.html")))
        same = next((x for x in self.session.profiles() if x["name"].lower() == user.lower()), None) if user else None
        out["match"] = same["path"] if same else ""
        return out

    def api_restore(self, p) -> dict:
        info = self.api_backup_info(p)
        if not info["backup"]:
            raise ApiError("Indiquez le dossier de la sauvegarde (celui qui contient manifest.json).")
        path = (p.get("target") or "").strip()
        prof = next((x for x in self.session.profiles() if x["path"] == path), None)
        self.session.target_home = "" if (prof is None or prof["current"]) else prof["path"]
        backup, over, dry, rules = info["backup"], bool(p.get("overwrite")), bool(p.get("dry")), str(p.get("rules") or "")

        def work(ctx):
            ctx.progress("Restauration...")
            res = self.session.restore(ctx, backup, overwrite=over, dry_run=dry, rules_text=rules)
            for item_id, r in res["items"].items():
                ctx.say(f"{item_id}: {r['restored']} restauré(s), {r['skipped']} déjà présent(s)"
                        + (f", {len(r['errors'])} erreur(s)" if r["errors"] else "") + (f"  → {r['dest']}" if r.get("dest") else ""))
            for e in res["errors"][:30]:
                ctx.say(f"⚠ {e}")
            return res

        self._start("restore", work)
        return {"started": True}

    def api_run_script(self, p) -> dict:
        info = self.api_backup_info(p)
        script = os.path.join(info["backup"], "installer_et_configurer.ps1") if info["backup"] else ""
        if not script or not os.path.exists(script):
            raise ApiError("Script introuvable dans la sauvegarde.")
        if not is_windows():
            raise ApiError(f"Le script est prévu pour Windows PowerShell : {script}")
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", script],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        return {"ok": True}

    # ------------------------------------------------------------------------------------------------------------
    # navigation dans les dossiers (un navigateur ne donne pas les chemins : c'est le serveur local qui les liste)
    def api_browse(self, p) -> dict:
        path = (p.get("path") or "").strip().strip('"')
        drives = []
        if is_windows():
            drives = [f"{chr(65 + i)}:\\" for i in range(26) if os.path.exists(f"{chr(65 + i)}:\\")]
        if not path:
            path = drives[0] if drives else (os.path.expanduser("~") if os.path.isdir(os.path.expanduser("~")) else os.sep)
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            raise ApiError(f"Dossier introuvable : {path}")
        dirs = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs.append(e.name)
                    except OSError:
                        pass
        except OSError as exc:
            raise ApiError(f"Dossier illisible : {exc}") from None
        parent = os.path.dirname(path)
        return {"path": path, "parent": parent if parent != path else "", "dirs": sorted(dirs, key=str.lower)[:2000], "drives": drives}

    # ------------------------------------------------------------------------------------------------------------
    def report_file(self, backup: str = "") -> Optional[str]:
        if backup:
            if not os.path.exists(os.path.join(backup, transfer.MANIFEST)):
                return None
            path = os.path.join(backup, "rapport.html")
        else:
            path = self.session.report_path()
        return path if os.path.exists(path) else None

    def api_quit(self, _p=None) -> dict:
        self._rx_stop()
        if self.job is not None:
            self.job.cancel()
        self.quit_event.set()
        return {"bye": True}


# ----------------------------------------------------------------------------------------------------------------
# HTTP
LOGIN_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>swap — connexion</title><style>
:root{color-scheme:light dark;--bg:#f4f5f8;--card:#fff;--ink:#141922;--muted:#5b6575;--line:#cdd2db;--accent:#4f46e5;--bad:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b23;--ink:#e9ecf2;--muted:#9aa4b5;--line:#3a4252;--accent:#818cf8;--bad:#f87171}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,"Segoe UI",sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;width:min(360px,calc(100vw - 32px))}
h1{margin:0 0 4px;font-size:20px}p{margin:0 0 16px;color:var(--muted)}label{display:block;font-weight:600;font-size:13px;margin-bottom:5px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit;font:inherit}
button{margin-top:14px;width:100%;padding:11px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.err{color:var(--bad);margin-top:12px}</style></head><body><form method="post" action="/login">
<h1>swap</h1><p>Migration de poste. Mot de passe requis.</p><label for="p">Mot de passe</label>
<input id="p" name="password" type="password" autofocus autocomplete="current-password"><button>Se connecter</button>__MSG__</form></body></html>"""

COOKIE = "swap_session"


class Handler(BaseHTTPRequestHandler):
    server_version = "swap"
    protocol_version = "HTTP/1.1"
    # renseignés pour chaque serveur par make_server (sous-classe dédiée)
    app: WebApp
    token: str = ""
    hosts: set = set()
    password: Optional[str] = None      # mode serveur : mot de passe exigé
    sessions: set = set()
    fails: dict = {}
    secure_cookie = False
    proxy = False                        # derrière un proxy inverse local (Apache de WAMP) : on lit ses en-têtes X-Forwarded-*

    def log_message(self, *_a) -> None:  # silence
        pass

    # -- utilitaires -------------------------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _host_error(self) -> str:
        """'' si la requête est acceptée, sinon l'explication (affichée à l'utilisateur)."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if self.password is not None:   # mode serveur : le mot de passe protège ; on exige seulement une origine identique
            if self.proxy:
                host = self.headers.get("X-Forwarded-Host", host).split(",")[0].strip()
            if origin is None or origin.split("//", 1)[-1] == host:
                return ""
            return (f"Origine refusée : la page vient de « {origin} » mais le serveur est joint par « {host} ». Derrière Apache, activez "
                    "« ProxyPreserveHost On » (voir wamp/swap-apache.conf) et lancez swap avec --proxy.")
        if host in self.hosts and (origin is None or origin.split("//", 1)[-1] in self.hosts):   # contre le « DNS rebinding »
            return ""
        return (f"Hôte refusé : « {host} ». Cette interface locale (LANCER.bat, « swap web ») ne répond que sur "
                f"{' ou '.join(sorted(self.hosts))}, ouverte sur le poste qui l'a lancée. Pour y accéder depuis le réseau "
                "(nom du serveur, WAMP/Apache…), lancez swap en mode serveur : SERVEUR.bat ou SERVEUR-WAMP.bat.")

    def _host_ok(self) -> bool:
        return not self._host_error()

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _cookie(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _client_ip(self) -> str:
        if self.proxy:  # le proxy local ajoute l'adresse réelle en dernier : seule celle-là est fiable
            forwarded = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            if forwarded:
                return forwarded
        return self.client_address[0]

    def _https(self) -> bool:
        return self.secure_cookie or (self.proxy and self.headers.get("X-Forwarded-Proto", "").lower() == "https")

    def _authed(self) -> bool:
        return self.password is None or self._cookie() in self.sessions

    def _login(self) -> None:
        ip = self._client_ip()
        count, until = self.fails.get(ip, (0, 0.0))
        if until > time.monotonic():
            return self._send(429, LOGIN_PAGE.replace("__MSG__", '<p class="err">Trop d\'essais : patientez une minute.</p>').encode("utf-8"),
                              "text/html; charset=utf-8")
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        given = (form.get("password") or [""])[0]
        if hmac.compare_digest(given.encode("utf-8"), self.password.encode("utf-8")):
            self.fails.pop(ip, None)
            sid = secrets.token_urlsafe(24)
            self.sessions.add(sid)
            flags = "; HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if self._https() else "")
            return self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": f"{COOKIE}={sid}{flags}"})
        count += 1
        self.fails[ip] = (count, time.monotonic() + 60 if count >= 5 else 0.0)
        time.sleep(1)   # ralentit les essais de mots de passe
        self._send(401, LOGIN_PAGE.replace("__MSG__", '<p class="err">Mot de passe incorrect.</p>').encode("utf-8"), "text/html; charset=utf-8")

    # -- routes ------------------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        problem = self._host_error()
        if problem:
            return self._json(403, {"error": problem})
        self.app.touch()
        path, query = urlparse(self.path).path, self._query()
        try:
            if self.password is not None:
                if method == "POST" and path == "/login":
                    return self._login()
                if path == "/logout":
                    self.sessions.discard(self._cookie())
                    return self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
                if not self._authed():
                    if path.startswith("/api/"):
                        return self._json(401, {"error": "Session expirée : rechargez la page."})
                    return self._send(200, LOGIN_PAGE.replace("__MSG__", "").encode("utf-8"), "text/html; charset=utf-8")
            allowed = self.password is not None or query.get("t") == self.token
            if method == "GET" and path == "/":
                if not allowed:
                    return self._send(403, "Adresse incomplète : ouvrez l'adresse affichée par swap.".encode("utf-8"), "text/plain; charset=utf-8")
                with open(INDEX, encoding="utf-8") as fh:
                    page = fh.read().replace("__TOKEN__", self.token)
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if method == "GET" and path in ("/report", "/backup-report"):
                if not (allowed and (self.password is not None or query.get("t") == self.token)):
                    return self._send(403, b"", "text/plain")
                file = self.app.report_file(query.get("path", "") if path == "/backup-report" else "")
                if not file:
                    return self._send(404, "Pas de rapport pour le moment.".encode("utf-8"), "text/plain; charset=utf-8")
                with open(file, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._send(404, b"", "text/plain")
            if not secrets.compare_digest(self.headers.get("X-Swap-Token", ""), self.token):
                return self._json(403, {"error": "Jeton invalide. Rouvrez swap."})
            fn = getattr(self.app, "api_" + path[5:].replace("-", "_"), None)
            if fn is None:
                return self._json(404, {"error": "Action inconnue."})
            if path == "/api/quit" and self.app.server_mode:
                return self._json(403, {"error": "Le serveur ne s'arrête pas depuis la page."})
            payload = dict(query)
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 4 * 1024 * 1024:
                    return self._json(413, {"error": "Requête trop volumineuse."})
                raw = self.rfile.read(length) if length else b""
                payload.update(json.loads(raw.decode("utf-8")) if raw.strip() else {})
            self._json(200, fn(payload))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except SinkError as exc:
            self._json(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - l'interface doit afficher l'erreur plutôt que de couper la connexion
            import traceback

            trace = traceback.format_exc()
            try:
                with open(os.path.join(self.app.session.out_dir, "web-erreurs.log"), "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.path}\n{trace}\n")
            except OSError:
                pass
            self._json(500, {"error": f"Erreur interne : {exc}", "trace": trace})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(app: WebApp, port: int = 0, listen: str = "127.0.0.1", password: Optional[str] = None,
                certfile: str = "", keyfile: str = "", proxy: bool = False) -> tuple:
    """Crée le serveur ; renvoie (serveur, jeton). Hors 127.0.0.1, un mot de passe est obligatoire (refus sinon).

    `proxy` : derrière Apache/WAMP ; n'est permis qu'en écoute locale, sinon n'importe qui pourrait falsifier X-Forwarded-*."""
    if proxy and listen not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("--proxy exige d'écouter sur 127.0.0.1 (seul Apache doit pouvoir joindre swap).")
    if listen not in ("127.0.0.1", "localhost", "::1") and not password:
        raise ValueError("Un mot de passe est obligatoire pour écouter ailleurs que sur ce poste.")
    token = secrets.token_urlsafe(18)
    server = _Server((listen, port), None)
    real = server.server_address[1]
    server.RequestHandlerClass = type("SwapHandler", (Handler,), {
        "app": app, "token": token, "hosts": {f"127.0.0.1:{real}", f"localhost:{real}"}, "password": password or None,
        "sessions": set(), "fails": {}, "secure_cookie": bool(certfile), "proxy": proxy})
    if certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile or None)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server, token


def open_window(url: str) -> None:
    """Ouvre la page dans une fenêtre d'application (Edge/Chrome sans barre d'onglets) si possible, sinon le navigateur."""
    if is_windows():
        for browser in ("msedge", "chrome"):
            try:
                proc = subprocess.run(["cmd", "/c", "start", "", browser, f"--app={url}"], capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
                if proc.returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                pass
    webbrowser.open(url)


def run(out_dir: Optional[str] = None, backup: Optional[str] = None, port: int = 0, browser: bool = True, idle: int = 300,
        listen: str = "127.0.0.1", password: Optional[str] = None, certfile: str = "", keyfile: str = "", server: bool = False,
        firewall: bool = False, proxy: bool = False) -> int:
    """Lance l'interface web.

    Poste local : s'arrête avec « Quitter », Ctrl+C, ou après `idle` secondes sans activité (0 = jamais).
    `server=True` : hébergée sur un serveur, protégée par mot de passe, ne s'arrête jamais toute seule ; le PC source et
    le PC cible sont toujours d'autres postes."""
    if sys.stdout is None:  # pythonw : pas de console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    app = WebApp(Session(out_dir) if out_dir else None, backup, server=server)
    try:
        httpd, token = make_server(app, port, listen, password, certfile, keyfile, proxy)
    except (OSError, ValueError, ssl.SSLError) as exc:
        print(f"Impossible de démarrer l'interface web : {exc}")
        return 1
    real = httpd.server_address[1]
    opened_fw = False
    if server and firewall:
        opened_fw, msg = net.firewall_open(real)
        print("Pare-feu : port ouvert." if opened_fw else f"Pare-feu : impossible d'ouvrir le port ({msg or 'droits administrateur requis'}).")
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    if server:
        scheme = "https" if certfile else "http"
        print(f"swap {__version__} — serveur de migration : {scheme}://{platform.node()}:{real}/  (écoute sur {listen})")
        if proxy:
            print("Mode proxy : swap n'est joignable que par Apache (WAMP) ; l'adresse à ouvrir est celle du site Apache.")
        elif not certfile:
            print("ATTENTION : sans --cert/--key, le mot de passe circule en clair sur le réseau (HTTP). Réseau interne uniquement.")
        print("Le compte Windows qui lance ce serveur doit être administrateur des PC source et cible. Ctrl+C pour arrêter.")
    else:
        url = f"http://127.0.0.1:{real}/?t={token}"
        print(f"swap {__version__} — interface web : {url}\nFermez cette page puis le bouton « Quitter » (ou Ctrl+C) pour arrêter.")
        if browser:
            open_window(url)
    try:
        while not app.quit_event.wait(1.0):
            if not server and idle and not app.busy() and time.monotonic() - app.last_activity > idle:
                print("Inactif : arrêt de swap.")
                break
    except KeyboardInterrupt:
        pass
    finally:
        app._rx_stop()
        if opened_fw:
            net.firewall_close(real)
        httpd.shutdown()
        httpd.server_close()
    return 0
