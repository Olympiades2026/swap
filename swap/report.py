"""Rapport lisible (Markdown + HTML autonome) à partir de l'inventaire."""

from __future__ import annotations

import html
import re
from collections import OrderedDict

from .util import human_size

NOTABLE_SHOWN = 25
TASK_STATE = {"0": "inconnu", "1": "désactivée", "2": "en attente", "3": "prête", "4": "en cours"}
DEFAULT_DSN = {"MS Access Database", "Excel Files", "dBASE Files"}
VENDOR_TASK_RE = re.compile(r"program files|\\windows\\|driverstore|programdata", re.I)
LEVEL_LABEL = {"critique": "🔴 Critique", "important": "🟠 Important", "info": "🔵 À savoir"}


class Report:
    def __init__(self, title: str):
        self.title = title
        self.blocks: list = []

    def h(self, text, level=2):
        self.blocks.append(("h", level, text))

    def p(self, text):
        self.blocks.append(("p", text))

    def bullets(self, items):
        if items:
            self.blocks.append(("ul", list(items)))

    def table(self, headers, rows):
        if rows:
            self.blocks.append(("table", headers, [[str(c) for c in r] for r in rows]))

    def md(self) -> str:
        out = [f"# {self.title}", ""]
        for b in self.blocks:
            if b[0] == "h":
                out += ["#" * b[1] + " " + b[2], ""]
            elif b[0] == "p":
                out += [b[1], ""]
            elif b[0] == "ul":
                out += [f"- {x}" for x in b[1]] + [""]
            else:
                esc = lambda c: c.replace("|", "\\|").replace("\n", " ")
                out.append("| " + " | ".join(esc(h) for h in b[1]) + " |")
                out.append("|" + "|".join("---" for _ in b[1]) + "|")
                out += ["| " + " | ".join(esc(c) for c in row) + " |" for row in b[2]]
                out.append("")
        return "\n".join(out)

    def html(self) -> str:
        e = html.escape
        body = []
        for b in self.blocks:
            if b[0] == "h":
                body.append(f"<h{b[1]}>{e(b[2])}</h{b[1]}>")
            elif b[0] == "p":
                body.append(f"<p>{e(b[1])}</p>")
            elif b[0] == "ul":
                body.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in b[1]) + "</ul>")
            else:
                head = "".join(f"<th>{e(h)}</th>" for h in b[1])
                rows = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in b[2])
                body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        css = (
            "body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#1b1f24}"
            "h1{border-bottom:2px solid #0b5fff;padding-bottom:.3rem}h2{margin-top:2rem;color:#0b3d91}"
            "table{border-collapse:collapse;width:100%;margin:.5rem 0 1rem;font-size:14px}"
            "th,td{border:1px solid #d0d7de;padding:4px 8px;text-align:left;vertical-align:top}"
            "th{background:#f0f4fa}tr:nth-child(even) td{background:#fafbfc}li{margin:.25rem 0}"
        )
        return (f"<!doctype html><html lang='fr'><head><meta charset='utf-8'><title>{e(self.title)}</title>"
                f"<style>{css}</style></head><body>" + "\n".join(body) + "</body></html>")


def build_report(inv: dict) -> Report:
    meta = inv["meta"]
    r = Report(f"Migration du poste {meta['machine']}")
    items = inv["items"]
    selected = [i for i in items if i["default"]]
    total = sum(i["size"] for i in selected)

    r.h("Synthèse")
    r.bullets([
        f"Poste : {meta['machine']} — utilisateur : {meta['domain'] + chr(92) if meta['domain'] else ''}{meta['user']}",
        f"Système : {meta['os']}",
        f"Analyse faite le {meta['date'].replace('T', ' à ')} (swap {meta['tool_version']})",
        f"Données proposées par défaut : {len(selected)} élément(s), {human_size(total)} au total "
        f"({len(items) - len(selected)} autre(s) décochés par défaut, voir ci-dessous)",
        f"Applications installées : {len([a for a in inv['apps'] if not a.get('component')])} (hors composants système)",
    ])

    r.h("À ne pas oublier (par ordre d'importance)")
    r.table(["Niveau", "Point d'attention"], [[LEVEL_LABEL[a["level"]], a["text"]] for a in inv["advice"]])

    r.h("Données à copier")
    by_cat: "OrderedDict[str, list]" = OrderedDict()
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)
    for cat, group in by_cat.items():
        r.h(cat, 3)
        rows = []
        for it in group:
            flags = ("" if it["default"] else "décoché ") + ("🔒 " if it["sensitive"] else "")
            rows.append([
                it["label"], human_size(it["size"]) if it["kind"] != "registry" else "registre", it["files"] or "",
                it["src"], (flags + it["note"]).strip(),
            ])
        r.table(["Élément", "Taille", "Fichiers", "Emplacement", "Remarques"], rows)

    apps = inv["apps"]
    if apps:
        real = [a for a in apps if not a.get("component")]
        auto = [a for a in real if a.get("winget_id")]
        manual = [a for a in real if not a.get("winget_id")]
        guessed = sum(1 for a in manual if a.get("winget_guess"))
        r.h("Applications à réinstaller")
        r.p(f"{len(auto)} réinstallable(s) automatiquement avec winget (script fourni), {len(manual)} à réinstaller à la main "
            "(prévoir installateurs et licences)."
            + (f" Pour {guessed} d'entre elles, winget propose une installation (suggestion à vérifier)." if guessed else ""))
        if manual:
            r.h("À réinstaller à la main", 3)
            r.table(["Application", "Version", "Éditeur", "Suggestion winget (à vérifier)"],
                    [[a["name"], a["version"], a["publisher"], a.get("winget_guess", "")] for a in manual])
        if auto:
            r.h("Installables avec winget", 3)
            r.table(["Application", "Version", "Identifiant winget"], [[a["name"], a["version"], a["winget_id"]] for a in auto])
        comps = [a for a in apps if a.get("component")]
        if comps:
            r.h("Composants, pilotes et outils gérés par Windows/la DSI (généralement inutile de les migrer)", 3)
            r.p(", ".join(sorted({a["name"] for a in comps}, key=str.lower)))

    inst = inv.get("installers") or {}
    if inst.get("matched") or inst.get("missing") or inst.get("others"):
        r.h("Installateurs des applications à réinstaller à la main")
        r.p("Windows ne conserve pas l'installateur d'un logiciel installé : seuls ceux qui traînent dans vos dossiers "
            "(Téléchargements, C:\\Temp…) peuvent être récupérés. Ils sont rattachés aux applications par le nom du fichier : vérifiez les versions.")
        rows = [[m["app"], "✔ " + m["file"], human_size(m["size"]), m["path"], "; ".join(m["alternatives"])] for m in inst.get("matched", [])]
        rows += [[name, "✘ aucun installateur trouvé", "", "à télécharger chez l'éditeur ou à demander à la DSI", ""] for name in inst.get("missing", [])]
        r.table(["Application", "Installateur", "Taille", "Trouvé ici", "Autres candidats"], rows)
        if inst.get("shares"):
            r.p("Dépôt(s) de logiciels probable(s) : " + ", ".join(f"{d['letter']} = {d['path']}" for d in inst["shares"]))
        if inst.get("others"):
            r.h("Autres installateurs trouvés (non rattachés à une application)", 3)
            r.table(["Taille", "Fichier"], [[human_size(o["size"]), o["path"]] for o in inst["others"]])

    if inv["type_hints"]:
        r.h("Types de fichiers rencontrés → logiciels à prévoir")
        rows = []
        for h in inv["type_hints"]:
            state = {True: "✔ trouvé", False: "✘ non trouvé dans les applis installées", None: ""}[h["installed"]]
            rows.append([h["ext"], h["count"], h["software"], state, h["advice"]])
        r.table(["Extension", "Nombre", "Logiciel", "Installé ?", "Conseil"], rows)

    if inv["notable_files"]:
        r.h("Fichiers remarquables (faciles à oublier)")
        for ext, paths in sorted(inv["notable_files"].items()):
            r.h(ext, 3)
            r.bullets(paths[:NOTABLE_SHOWN] + ([f"… et {len(paths) - NOTABLE_SHOWN} autres (voir inventaire.json)"] if len(paths) > NOTABLE_SHOWN else []))

    if inv["big_files"]:
        r.h("Très gros fichiers")
        r.table(["Taille", "Fichier"], [[human_size(b["size"]), b["path"]] for b in inv["big_files"]])

    s = inv.get("system") or {}
    if s:
        r.h("Configuration système à recréer")
        if s.get("printers"):
            r.h("Imprimantes", 3)
            r.table(["Nom", "Pilote", "Chemin réseau", "Par défaut"],
                    [[p["name"], p["driver"], p["network_path"] or p["port"], "oui" if p["default"] else ""] for p in s["printers"]])
        if s.get("drives"):
            r.h("Lecteurs réseau", 3)
            r.table(["Lettre", "Chemin", "Utilisateur"], [[d["letter"], d["path"], d["user"]] for d in s["drives"]])
        if s.get("wifi"):
            r.h("Profils Wi-Fi", 3)
            r.bullets(s["wifi"])
        st = s.get("startup") or {}
        if st.get("registry") or st.get("folder"):
            r.h("Programmes lancés au démarrage", 3)
            r.table(["Nom", "Commande", "Portée"], [[x["name"], x["command"], x["scope"]] for x in st.get("registry", [])]
                    + [[f, "(dossier Démarrage)", "utilisateur"] for f in st.get("folder", [])])
        if s.get("tasks"):
            mine = [t for t in s["tasks"] if not VENDOR_TASK_RE.search(t["action"] or "")]
            r.h("Tâches planifiées (hors Microsoft)", 3)
            if len(s["tasks"]) > len(mine):
                r.p(f"{len(s['tasks']) - len(mine)} tâche(s) créée(s) par des logiciels installés (OneDrive, Chrome, pilotes…) ne sont pas listées : "
                    "elles se recréent à l'installation.")
            r.table(["Nom", "Action", "État"], [[t["name"], t["action"], TASK_STATE.get(t["state"], t["state"])] for t in mine])
        if s.get("odbc"):
            custom = [o for o in s["odbc"] if o["name"] not in DEFAULT_DSN]
            r.h("Sources de données ODBC", 3)
            if len(custom) < len(s["odbc"]):
                r.p("Les sources par défaut d'Office (MS Access Database, Excel Files, dBASE Files) ne sont pas listées.")
            r.table(["Nom", "Pilote", "Portée"], [[o["name"], o["driver"], o["scope"]] for o in custom])
        if s.get("env"):
            r.h("Variables d'environnement utilisateur", 3)
            r.table(["Variable", "Valeur"], [[k, v] for k, v in s["env"].items()])
        if s.get("hosts"):
            r.h("Fichier hosts (lignes personnalisées)", 3)
            r.bullets(s["hosts"])
        if s.get("certificates"):
            r.h("Certificats personnels", 3)
            r.table(["Sujet", "Clé privée", "Expire", "Empreinte"],
                    [[c["subject"], "OUI" if c["private_key"] else "non", c["expires"], c["thumbprint"]] for c in s["certificates"]])
        if s.get("credentials"):
            r.h("Identifiants Windows enregistrés (noms seulement)", 3)
            r.bullets(s["credentials"])
    return r
