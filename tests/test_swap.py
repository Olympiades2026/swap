import json
import os
import tempfile
import unittest
from unittest import mock

from swap import apps, cli, fs, redirects, transfer
from swap.locations import Locations
from swap.model import Item
from swap.scan import dedupe_overlaps, run_scan
from swap.scripts import build_setup_script, psq
from swap.util import is_under, slugify


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


def write(path, content="x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)
    if mtime:
        os.utime(path, (mtime, mtime))


def make_profile(root):
    """Faux profil utilisateur avec quelques données et configs."""
    home = os.path.join(root, "home")
    loc = Locations(
        home=home,
        appdata=os.path.join(home, "AppData", "Roaming"),
        localappdata=os.path.join(home, "AppData", "Local"),
        known={n: os.path.join(home, n) for n in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")},
    )
    write(os.path.join(home, "Documents", "rapport.docx"), "rapport")
    write(os.path.join(home, "Documents", "Outlook", "archive.pst"), "pst")
    write(os.path.join(home, "Documents", "compta", "budget.xlsm"), "macro")
    write(os.path.join(home, "Documents", "~$rapport.docx"), "verrou")
    write(os.path.join(home, "Documents", "projet", "node_modules", "lib", "a.js"), "regen")
    write(os.path.join(home, "Desktop", "note.txt"), "note")
    write(os.path.join(home, "Projets", "app", "main.py"), "print(1)")
    write(os.path.join(home, ".ssh", "id_rsa"), "clé")
    write(os.path.join(home, ".gitconfig"), "[user]")
    write(os.path.join(loc.appdata, "Notepad++", "config.xml"), "<cfg/>")
    write(os.path.join(loc.appdata, "Mozilla", "Firefox", "Profiles", "p", "places.sqlite"), "fav")
    write(os.path.join(loc.appdata, "Mozilla", "Firefox", "Profiles", "p", "cache2", "junk"), "cache")
    write(os.path.join(loc.appdata, "Microsoft", "Signatures", "sig.htm"), "sig")
    return loc


class FsTests(unittest.TestCase):
    def test_walk_excludes_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as t:
            write(os.path.join(t, "a", "b.txt"))
            write(os.path.join(t, "a", "Thumbs.db"))
            write(os.path.join(t, "node_modules", "x.js"))
            write(os.path.join(t, "~$lock.docx"))
            rels = sorted(e.rel for e in fs.walk(t, fs.make_excluder()) if e.kind == "f")
            self.assertEqual(rels, ["a/b.txt"])

    def test_cache_dirs_only_excluded_when_asked(self):
        with tempfile.TemporaryDirectory() as t:
            write(os.path.join(t, "Cache", "c.bin"))
            self.assertEqual(fs.collect(t, fs.make_excluder(cache=False)).files, 1)
            self.assertEqual(fs.collect(t, fs.make_excluder(cache=True)).files, 0)

    def test_symlinks_not_followed(self):
        with tempfile.TemporaryDirectory() as t:
            write(os.path.join(t, "real", "f.txt"))
            os.symlink(os.path.join(t, "real"), os.path.join(t, "link"))
            self.assertEqual(fs.collect(t, fs.make_excluder()).files, 1)

    def test_onedrive_placeholder_detected(self):
        st = mock.Mock(st_file_attributes=fs.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
        self.assertTrue(fs.is_placeholder(st))
        self.assertFalse(fs.is_placeholder(mock.Mock(st_file_attributes=0x20)))

    def test_single_file_root(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "f.txt")
            write(p, "abc")
            entries = list(fs.walk(p))
            self.assertEqual([(e.kind, e.rel) for e in entries], [("f", "f.txt")])

    def test_typesink_notable_and_big(self):
        sink = fs.TypeSink(keep_big=2)
        sink.add("/a/x.pst", 10)
        sink.add("/a/y.docx", fs.BIG_FILE + 1)
        sink.add("/a/z.iso", fs.BIG_FILE + 5)
        sink.add("/a/w.iso", fs.BIG_FILE + 3)
        self.assertEqual(sink.notable[".pst"], ["/a/x.pst"])
        self.assertEqual(sorted(s for s, _ in sink.big), [fs.BIG_FILE + 3, fs.BIG_FILE + 5])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loc = make_profile(self.tmp.name)
        self.inv = run_scan(self.loc)
        self.by_id = {i["id"]: i for i in self.inv["items"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_folders_and_extras_found(self):
        self.assertIn("dossier-documents", self.by_id)
        self.assertIn("dossier-desktop", self.by_id)
        self.assertIn("profil-projets", self.by_id)

    def test_documents_ignore_locks_and_node_modules(self):
        self.assertEqual(self.by_id["dossier-documents"]["files"], 3)

    def test_app_configs_found_with_flags(self):
        self.assertIn("config-notepadpp", self.by_id)
        self.assertTrue(self.by_id["config-ssh"]["sensitive"])
        self.assertEqual(self.by_id["config-git"]["kind"], "file")
        self.assertEqual(self.by_id["config-firefox"]["files"], 1)  # cache2 ignoré

    def test_notable_files_and_hints(self):
        self.assertTrue(any(p.endswith("archive.pst") for p in self.inv["notable_files"][".pst"]))
        exts = {h["ext"] for h in self.inv["type_hints"]}
        self.assertTrue({".pst", ".xlsm"} <= exts)

    def test_advice_mentions_pst_and_generic(self):
        texts = " ".join(a["text"] for a in self.inv["advice"])
        self.assertIn(".pst", texts)
        self.assertIn("Licences", texts)
        levels = [a["level"] for a in self.inv["advice"]]
        self.assertEqual(levels, sorted(levels, key=["critique", "important", "info"].index))

    def test_json_serialisable_and_report_renders(self):
        json.dumps(self.inv)
        with tempfile.TemporaryDirectory() as out:
            paths = cli.write_outputs(self.inv, out)
            html = read(paths["rapport_html"])
            md = read(paths["rapport_md"])
            self.assertIn("Documents", html)
            self.assertIn("| Élément |", md)
            self.assertTrue(read(paths["script"], "rb").startswith(b"\xef\xbb\xbf"))

    def test_onedrive_folder_unchecked_by_default(self):
        od = os.path.join(self.loc.home, "OneDrive - Contoso")
        write(os.path.join(od, "Documents", "cloud.docx"))
        self.loc.known["Documents"] = os.path.join(od, "Documents")
        inv = run_scan(self.loc)
        doc = next(i for i in inv["items"] if i["id"] == "dossier-documents")
        self.assertFalse(doc["default"])
        self.assertIn("OneDrive", doc["note"])
        # le dossier OneDrive lui-même n'est pas proposé une seconde fois
        self.assertFalse(any("onedrive" in i["id"] for i in inv["items"] if i["id"].startswith("profil-")))

    def test_dedupe_overlaps(self):
        a = Item("a", "A", "dir", "/x/y", {}, size=1)
        b = Item("b", "B", "dir", "/x", {}, size=2)
        dedupe_overlaps([a, b])
        self.assertFalse(a.default)
        self.assertTrue(b.default)
        self.assertIn("Déjà inclus", a.note)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loc = make_profile(self.tmp.name)
        self.inv = run_scan(self.loc)
        self.items = [Item.from_dict(d) for d in self.inv["items"] if d["default"]]
        self.dest = os.path.join(self.tmp.name, "usb")

    def tearDown(self):
        self.tmp.cleanup()

    def test_select_only_skip(self):
        allitems = [Item.from_dict(d) for d in self.inv["items"]]
        only = transfer.select_items(allitems, only=["documents"])
        self.assertEqual([i.id for i in only], ["dossier-documents"])
        skipped = transfer.select_items(allitems, skip=["ssh"])
        self.assertNotIn("config-ssh", [i.id for i in skipped])

    def test_select_interactive_toggle(self):
        allitems = [Item.from_dict(d) for d in self.inv["items"]]
        answers = iter(["n", "1 2", ""])
        chosen = transfer.select_items(allitems, interactive=True, input_fn=lambda _p: next(answers), out=lambda *_: None)
        self.assertEqual([i.id for i in chosen], [allitems[0].id, allitems[1].id])

    def test_backup_verify_restore_roundtrip(self):
        summary = transfer.run_backup(self.items, self.inv, self.dest, want_hash=True)
        self.assertEqual(summary["errors"], [])
        backup = summary["backup"]
        self.assertTrue(os.path.exists(os.path.join(backup, "manifest.json")))
        self.assertFalse(os.path.exists(os.path.join(backup, "data", "dossier-documents", "projet", "node_modules")))
        res = transfer.verify_backup(backup, deep=True)
        self.assertEqual(res["problems"], [])
        self.assertGreater(res["checked"], 5)

        # nouveau poste : autre nom d'utilisateur, autre racine
        base = os.path.join(self.tmp.name, "new", "home2")
        new = Locations(
            home=base,
            appdata=os.path.join(base, "Roaming"),
            localappdata=os.path.join(base, "Local"),
            known={n: os.path.join(base, n) for n in self.loc.known},
        )
        out = transfer.run_restore(backup, new)
        self.assertEqual(out["errors"], [])
        self.assertEqual(read(os.path.join(new.known["Documents"], "rapport.docx")), "rapport")
        self.assertTrue(os.path.exists(os.path.join(new.appdata, "Notepad++", "config.xml")))
        self.assertEqual(read(os.path.join(new.home, ".gitconfig")), "[user]")
        self.assertTrue(os.path.exists(os.path.join(new.home, ".ssh", "id_rsa")))
        self.assertTrue(os.path.exists(os.path.join(new.home, "Projets", "app", "main.py")))
        self.assertFalse(os.path.exists(os.path.join(new.appdata, "Mozilla", "Firefox", "Profiles", "p", "cache2")))

    def test_restore_never_overwrites_unless_asked(self):
        backup = transfer.run_backup(self.items, self.inv, self.dest)["backup"]
        target = os.path.join(self.loc.known["Documents"], "rapport.docx")
        write(target, "version plus récente sur le nouveau poste")
        res = transfer.run_restore(backup, self.loc)
        self.assertEqual(read(target), "version plus récente sur le nouveau poste")
        self.assertGreater(res["items"]["dossier-documents"]["skipped"], 0)
        transfer.run_restore(backup, self.loc, overwrite=True)
        self.assertEqual(read(target), "rapport")

    def test_incremental_second_run_copies_only_changes(self):
        transfer.run_backup(self.items, self.inv, self.dest)
        second = transfer.run_backup(self.items, self.inv, self.dest)
        self.assertTrue(all(r.get("copied", 0) == 0 for r in second["items"].values()))
        write(os.path.join(self.loc.known["Documents"], "nouveau.txt"), "n")
        third = transfer.run_backup(self.items, self.inv, self.dest)
        self.assertEqual(third["items"]["dossier-documents"]["copied"], 1)

    def test_verify_detects_missing_and_corrupt(self):
        backup = transfer.run_backup(self.items, self.inv, self.dest, want_hash=True)["backup"]
        os.remove(os.path.join(backup, "data", "dossier-desktop", "note.txt"))
        with open(os.path.join(backup, "data", "dossier-documents", "rapport.docx"), "w") as fh:
            fh.write("rappor?")  # même taille, contenu différent
        shallow = transfer.verify_backup(backup)
        self.assertEqual(len(shallow["problems"]), 1)  # le fichier manquant seulement
        deep = transfer.verify_backup(backup, deep=True)
        self.assertEqual(len(deep["problems"]), 2)

    def test_dry_run_writes_nothing(self):
        summary = transfer.run_backup(self.items, self.inv, self.dest, dry_run=True)
        self.assertFalse(os.path.exists(summary["backup"]))
        self.assertGreater(summary["items"]["dossier-documents"]["copied"], 0)

    def test_locked_source_file_is_reported_not_fatal(self):
        real_copy = transfer.copy_file

        def flaky(src, dest, want_hash=False):
            if src.endswith("note.txt"):
                raise PermissionError("verrouillé")
            return real_copy(src, dest, want_hash)

        with mock.patch.object(transfer, "copy_file", flaky):
            summary = transfer.run_backup(self.items, self.inv, self.dest)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("note.txt", summary["errors"][0])
        self.assertTrue(os.path.exists(os.path.join(summary["backup"], "erreurs.log")))
        self.assertTrue(os.path.exists(os.path.join(summary["backup"], "data", "dossier-documents", "rapport.docx")))

    def test_no_partial_files_left_behind(self):
        backup = transfer.run_backup(self.items, self.inv, self.dest)["backup"]
        leftovers = [f for _r, _d, files in os.walk(backup) for f in files if f.endswith(transfer.PART)]
        self.assertEqual(leftovers, [])

    def test_abs_target_missing_drive_is_relocated(self):
        loc = Locations("/h", "/h/a", "/h/l", {})
        with mock.patch("swap.locations.is_windows", return_value=True), mock.patch("os.path.exists", return_value=False), \
                mock.patch("os.path.splitdrive", side_effect=lambda p: (p[:2], p[2:])):
            path = loc.resolve({"kind": "abs", "path": "D:\\Data\\x"})
        self.assertIn("Migration_D", path)

    def test_cli_end_to_end(self):
        out = os.path.join(self.tmp.name, "sortie")
        with mock.patch("swap.cli.Locations.detect", return_value=self.loc), \
                mock.patch("swap.cli.run_scan", side_effect=lambda **kw: run_scan(self.loc, **kw)):
            self.assertEqual(cli.main(["scan", "--out", out]), 0)
            self.assertEqual(cli.main(["copy", "--dest", self.dest, "--out", out, "-y", "--skip", "ssh"]), 0)
            backup = os.path.join(self.dest, os.listdir(self.dest)[0])
            self.assertTrue(os.path.exists(os.path.join(backup, "RESTAURER.bat")))
            self.assertTrue(os.path.exists(os.path.join(backup, "outil", "swap", "cli.py")))
            self.assertFalse(os.path.exists(os.path.join(backup, "data", "config-ssh")))
            self.assertEqual(cli.main(["verify", "--backup", backup]), 0)
            self.assertEqual(cli.main(["restore", "--backup", backup, "--dry-run"]), 0)


class RealWorldTests(unittest.TestCase):
    """Cas relevés sur un vrai poste : installation WinDev, logiciels dans C:\\, certificat Intune, ISO..."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loc = make_profile(self.tmp.name)
        self.root = os.path.join(self.tmp.name, "C")
        pc = os.path.join(self.root, "PC SOFT", "WINDEV Suite SaaS 2025")
        write(os.path.join(pc, "Examples", "WD", "Complete examples", "Demo", "Demo.wdp"))
        write(os.path.join(pc, "Examples", "WD", "Complete examples", "Demo", "Demo.fic"))
        write(os.path.join(pc, "Programs", "Data", "aide.fic"))
        write(os.path.join(pc, "Personal", "Guide", "guide.FIC"), "vos données")
        write(os.path.join(pc, "Personal", "My WINDEV", "pref.txt"), "vos préférences")
        write(os.path.join(self.root, "Applications_CLMB", "Annuaire", "salaries.fic"), "données entreprise")
        write(os.path.join(self.root, "Applications_CLMB", "Annuaire", "salaries.ndx"), "index")
        write(os.path.join(self.root, "laragon", "bin", "php.exe"), "logiciel")
        write(os.path.join(self.root, "laragon", "www", "monsite", "index.php"), "<?php")
        write(os.path.join(self.root, "Temp", "windows.iso"), "iso")
        write(os.path.join(self.root, "Intune Content Prep tool", "x.exe"))
        write(os.path.join(self.root, "AVerMedia", "cam.dll"))
        write(os.path.join(self.root, "CLMB", "donnees.txt"), "mes données")
        os.makedirs(os.path.join(self.root, "Vide"))
        os.makedirs(os.path.join(self.loc.home, "DossierVide"))
        os.symlink(os.path.join(self.loc.home, "Projets"), os.path.join(self.loc.home, "Voisinage réseau"))
        fake_apps = [
            {"name": "Laragon 8.6.1", "version": "8.6.1", "publisher": "leokhoa", "scope": "machine"},
            {"name": "Assist Central Pro", "version": "4", "publisher": "AVerMedia TECHNOLOGIES, Inc.", "scope": "machine"},
            {"name": "AutomaticUpdate", "version": "30", "publisher": "PC SOFT", "scope": "machine"},
        ]
        self.patches = [mock.patch("swap.scan.fixed_drives", return_value=[self.root]),
                        mock.patch("swap.scan.apps.installed_apps", return_value=fake_apps)]
        for p in self.patches:
            p.start()
        self.inv = run_scan(self.loc)
        self.by_id = {i["id"]: i for i in self.inv["items"]}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_windev_installation_is_not_copied_but_personal_is(self):
        pcsoft = self.by_id["disque-x-pc-soft"]
        self.assertFalse(pcsoft["default"])
        self.assertIn("Installation de WinDev", pcsoft["note"])
        personal = self.by_id["garder-pc-soft-personal"]
        self.assertTrue(personal["default"])
        self.assertEqual(personal["files"], 2)
        self.assertIn(personal["src"], pcsoft["exclude_paths"])
        # les exemples et données de l'installation ne sont ni des projets ni des « fichiers remarquables »
        self.assertFalse([i for i in self.inv["items"] if i["id"].startswith("pcsoft-projet")])
        exts = {h["ext"] for h in self.inv["type_hints"]}
        self.assertNotIn(".wdp", exts)
        self.assertEqual(self.inv["notable_files"][".fic"], [os.path.join(self.root, "Applications_CLMB", "Annuaire", "salaries.fic")])

    def test_your_own_pcsoft_data_is_carved_out(self):
        self.assertIn("pcsoft-donnees-annuaire", self.by_id)
        self.assertTrue(self.by_id["pcsoft-donnees-annuaire"]["default"])
        self.assertTrue(self.by_id["disque-x-applications-clmb"]["default"])

    def test_installed_software_and_noise_folders_unchecked(self):
        for item_id in ("disque-x-laragon", "disque-x-temp", "disque-x-intune-content-prep-tool", "disque-x-avermedia"):
            self.assertFalse(self.by_id[item_id]["default"], item_id)
        self.assertIn("Assist Central Pro", self.by_id["disque-x-avermedia"]["note"])
        self.assertTrue(self.by_id["disque-x-clmb"]["default"])

    def test_laragon_keeps_www_only(self):
        www = self.by_id["garder-laragon-www"]
        self.assertTrue(www["default"])
        self.assertEqual(www["files"], 1)
        self.assertIn(www["src"], self.by_id["disque-x-laragon"]["exclude_paths"])

    def test_empty_folders_and_junctions_are_ignored(self):
        self.assertNotIn("disque-x-vide", self.by_id)
        self.assertNotIn("profil-dossiervide", self.by_id)
        self.assertFalse([i for i in self.inv["items"] if "Voisinage" in i["label"]])

    def test_backup_of_this_layout_has_no_duplicate_and_honours_defaults(self):
        items = [Item.from_dict(d) for d in self.inv["items"] if d["default"]]
        backup = transfer.run_backup(items, self.inv, os.path.join(self.tmp.name, "usb"))["backup"]
        data = os.path.join(backup, "data")
        self.assertTrue(os.path.exists(os.path.join(data, "garder-pc-soft-personal", "My WINDEV", "pref.txt")))
        self.assertTrue(os.path.exists(os.path.join(data, "garder-laragon-www", "monsite", "index.php")))
        self.assertFalse(os.path.exists(os.path.join(data, "disque-x-pc-soft")))
        self.assertFalse(os.path.exists(os.path.join(data, "disque-x-temp")))

    def test_exclude_patterns(self):
        items = [Item.from_dict(d) for d in self.inv["items"]]
        only = [i for i in items if i.id == "disque-x-temp"]
        kept = transfer.run_backup(only, self.inv, os.path.join(self.tmp.name, "u1"))["backup"]
        self.assertTrue(os.path.exists(os.path.join(kept, "data", "disque-x-temp", "windows.iso")))
        skipped = transfer.run_backup(only, self.inv, os.path.join(self.tmp.name, "u2"), exclude_files=["*.iso"])["backup"]
        self.assertFalse(os.path.exists(os.path.join(skipped, "data", "disque-x-temp", "windows.iso")))

    def test_intune_certificate_is_not_critical(self):
        from swap.advice import compute_advice

        inv = dict(self.inv)
        inv["system"] = {"certificates": [
            {"subject": "CN=09fe5669-8cc4-45af-9d14-ffe6f5df8074", "issuer": "CN=Microsoft Intune MDM Device CA", "private_key": True},
        ]}
        adv = compute_advice(inv)
        self.assertFalse([a for a in adv if a["level"] == "critique" and "certificat" in a["text"]])
        self.assertTrue([a for a in adv if "Intune" in a["text"] and a["level"] == "info"])
        inv["system"]["certificates"].append({"subject": "CN=Jean Dupont, O=Société", "issuer": "CN=CA Interne", "private_key": True})
        crit = [a for a in compute_advice(inv) if a["level"] == "critique" and "certificat" in a["text"]]
        self.assertEqual(len(crit), 1)
        self.assertIn("1 certificat", crit[0]["text"])

    def test_report_hides_vendor_tasks_and_default_dsn(self):
        from swap.report import build_report

        inv = dict(self.inv)
        inv["system"] = {
            "tasks": [
                {"name": "Sauvegarde nocturne", "path": "\\", "state": "3", "action": "D:\\scripts\\backup.bat"},
                {"name": "OneDrive Startup", "path": "\\", "state": "3", "action": "C:\\Program Files\\Microsoft OneDrive\\OneDriveLauncher.exe"},
            ],
            "odbc": [{"name": "Excel Files", "driver": "x", "scope": "utilisateur"}, {"name": "COMPTA", "driver": "MariaDB", "scope": "utilisateur"}],
        }
        md = build_report(inv).md()
        self.assertIn("Sauvegarde nocturne", md)
        self.assertNotIn("OneDrive Startup", md)
        self.assertIn("1 tâche(s) créée(s) par des logiciels installés", md)
        self.assertIn("COMPTA", md)
        self.assertNotIn("| Excel Files |", md)


class PcSoftTests(unittest.TestCase):
    """Projets WinDev/WebDev et données HFSQL : repérés où qu'ils soient, extraits, redirigeables."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loc = make_profile(self.tmp.name)
        docs = self.loc.known["Documents"]
        self.proj = os.path.join(docs, "Mes Projets", "GestionStock")
        write(os.path.join(self.proj, "GestionStock.wdp"), "projet")
        write(os.path.join(self.proj, "WIN_Main.wdw"), "fenetre")
        write(os.path.join(self.proj, "Data", "CLIENT.fic"), "donnees")
        write(os.path.join(self.proj, "Data", "CLIENT.ndx"), "index")
        write(os.path.join(self.tmp.name, "home", "Projets", "web", "site.wwp"), "webdev")
        self.inv = run_scan(self.loc)
        self.by_id = {i["id"]: i for i in self.inv["items"]}
        self.dest = os.path.join(self.tmp.name, "usb")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roots_carved_out_as_separate_items(self):
        for item_id in ("pcsoft-projet-gestionstock", "pcsoft-donnees-data", "pcsoft-projet-web"):
            self.assertIn(item_id, self.by_id)
        self.assertEqual(self.by_id["pcsoft-projet-gestionstock"]["category"], "PC SOFT (WinDev / WebDev / HFSQL)")
        self.assertIn(self.proj, self.by_id["dossier-documents"]["exclude_paths"])
        self.assertEqual(self.by_id["pcsoft-projet-gestionstock"]["exclude_paths"], [os.path.join(self.proj, "Data")])
        # aucun octet compté deux fois
        self.assertEqual(self.by_id["pcsoft-donnees-data"]["files"], 2)
        self.assertEqual(self.by_id["pcsoft-projet-gestionstock"]["files"], 2)
        self.assertEqual(self.by_id["dossier-documents"]["files"], 3)

    def test_type_hints_and_advice_for_pcsoft(self):
        exts = {h["ext"] for h in self.inv["type_hints"]}
        self.assertTrue({".wdp", ".fic", ".wwp"} <= exts)

    def test_no_duplicate_copy_and_portable_targets(self):
        items = [Item.from_dict(d) for d in self.inv["items"] if d["default"]]
        backup = transfer.run_backup(items, self.inv, self.dest)["backup"]
        docs = os.path.join(backup, "data", "dossier-documents")
        self.assertFalse(os.path.exists(os.path.join(docs, "Mes Projets", "GestionStock")))  # extrait, pas dupliqué
        self.assertTrue(os.path.exists(os.path.join(backup, "data", "pcsoft-projet-gestionstock", "GestionStock.wdp")))
        self.assertFalse(os.path.exists(os.path.join(backup, "data", "pcsoft-projet-gestionstock", "Data")))
        self.assertTrue(os.path.exists(os.path.join(backup, "data", "pcsoft-donnees-data", "CLIENT.fic")))
        self.assertEqual(self.by_id["pcsoft-projet-gestionstock"]["target"],
                         {"kind": "known", "name": "Documents", "rel": "Mes Projets/GestionStock"})

    def test_restore_without_rules_goes_back_to_same_relative_place(self):
        items = [Item.from_dict(d) for d in self.inv["items"] if d["default"]]
        backup = transfer.run_backup(items, self.inv, self.dest)["backup"]
        base = os.path.join(self.tmp.name, "new")
        new = Locations(base, os.path.join(base, "R"), os.path.join(base, "L"), {n: os.path.join(base, n) for n in self.loc.known})
        transfer.run_restore(backup, new)
        self.assertTrue(os.path.exists(os.path.join(base, "Documents", "Mes Projets", "GestionStock", "GestionStock.wdp")))
        self.assertTrue(os.path.exists(os.path.join(base, "Documents", "Mes Projets", "GestionStock", "Data", "CLIENT.fic")))

    def test_restore_with_destination_rules(self):
        target = os.path.join(self.tmp.name, "C_Mes Projets")
        data_target = os.path.join(self.tmp.name, "D_Donnees")
        items = [Item.from_dict(d) for d in self.inv["items"] if d["default"]]
        # une règle enregistrée avec la sauvegarde...
        backup = transfer.run_backup(items, self.inv, self.dest,
                                     redirects=redirects.parse_rules([f"pcsoft-projet = {target}"]))["backup"]
        base = os.path.join(self.tmp.name, "new")
        new = Locations(base, os.path.join(base, "R"), os.path.join(base, "L"), {n: os.path.join(base, n) for n in self.loc.known})
        # ...complétée par une règle donnée à la restauration (elle prime)
        res = transfer.run_restore(backup, new, rules=redirects.parse_rules([f"pcsoft-donnees = {data_target}"]))
        self.assertEqual(res["errors"], [])
        # 2 projets correspondent -> un sous-dossier par projet
        self.assertTrue(os.path.exists(os.path.join(target, "GestionStock", "GestionStock.wdp")))
        self.assertTrue(os.path.exists(os.path.join(target, "web", "site.wwp")))
        # 1 seul élément de données -> directement dans la destination
        self.assertTrue(os.path.exists(os.path.join(data_target, "CLIENT.fic")))
        self.assertEqual(res["items"]["pcsoft-projet-gestionstock"]["dest"], os.path.join(target, "GestionStock"))
        # le reste n'a pas bougé
        self.assertTrue(os.path.exists(os.path.join(base, "Documents", "rapport.docx")))
        self.assertFalse(os.path.exists(os.path.join(base, "Documents", "Mes Projets", "GestionStock")))

    def test_cli_map_option_and_rules_file(self):
        out = os.path.join(self.tmp.name, "sortie")
        target = os.path.join(self.tmp.name, "MesProjets")
        with mock.patch("swap.cli.Locations.detect", return_value=self.loc), \
                mock.patch("swap.cli.run_scan", side_effect=lambda **kw: run_scan(self.loc, **kw)):
            self.assertEqual(cli.main(["scan", "--out", out]), 0)
            self.assertTrue(os.path.exists(os.path.join(out, "regles.txt")))
            with open(os.path.join(out, "regles.txt"), "a", encoding="utf-8") as fh:
                fh.write(f"pcsoft-projet-gestionstock = {target}\n")
            self.assertEqual(cli.main(["copy", "--dest", self.dest, "--out", out, "-y"]), 0)
            backup = os.path.join(self.dest, os.listdir(self.dest)[0])
            self.assertEqual(cli.main(["restore", "--backup", backup]), 0)
        self.assertTrue(os.path.exists(os.path.join(target, "GestionStock.wdp")))


class RulesTests(unittest.TestCase):
    def test_parse_rules(self):
        rules = redirects.parse_rules(["# commentaire", "", "  pcsoft-projet = C:\\Mes Projets ", 'a|b = "D:\\X y"', "sans egal"])
        self.assertEqual(rules, [("pcsoft-projet", "C:\\Mes Projets"), ("a|b", "D:\\X y")])

    def test_first_rule_wins_and_alternatives(self):
        it = Item("pcsoft-projet-x", "PC SOFT : projet X", "dir", "/d/x", {})
        other = Item("config-git", "Git", "file", "/h/.gitconfig", {})
        reg = Item("registre-putty", "PuTTY", "registry", "HKCU\\x", {})
        rules = [("nomatch", "/z"), ("git|projet", "/first"), ("pcsoft", "/second")]
        self.assertEqual(redirects.matching_rule(it, rules), 1)
        plan = redirects.plan_redirects([it, other, reg], rules)
        self.assertEqual(plan["pcsoft-projet-x"], os.path.join("/first", "x"))  # la règle vise 2 éléments -> un sous-dossier chacun
        self.assertIn("config-git", plan)
        self.assertEqual(plan["config-git"], os.path.join("/first", ".gitconfig"))  # fichier : rangé sous son nom
        self.assertNotIn("registre-putty", plan)

    def test_template_has_no_active_rule(self):
        self.assertEqual(redirects.parse_rules(redirects.TEMPLATE.splitlines()), [])


class MiscTests(unittest.TestCase):
    WINGET = (
        "\r-\r\\\r  \rNom                    ID                       Version   Source\r\n"
        "------------------------------------------------------------------------\r\n"
        "7-Zip 23.01 (x64)       7zip.7zip                23.01     winget\r\n"
        "Git                     Git.Git                  2.44.0    winget\r\n"
        "Notepad++ (64-bit x64)  Notepad++.Notepad++      8.6.4     winget\r\n"
    )

    def test_parse_winget_table(self):
        m = apps.parse_winget_table(self.WINGET)
        self.assertEqual(m["Git"], "Git.Git")
        self.assertEqual(m["7-Zip 23.01 (x64)"], "7zip.7zip")
        self.assertEqual(len(m), 3)
        self.assertEqual(apps.parse_winget_table("rien d'utile"), {})

    def test_parse_winget_real_world_quirks(self):
        # Vrai format observé : version « > 1.8.10 » (avec espace), colonne « Disponible » vide, doublons, espaces insécables.
        rows = [("Git", "Git.Git", "2.53.0.3", "2.55.0.5"),
                ("Microsoft\xa0Office fr-fr", "Microsoft.Office", "16.0.20430.2009", ""),
                ("WindowsAppRuntime.1.8", "Microsoft.WindowsAppRuntime.1.8", "> 1.8.10", ""),
                ("WindowsAppRuntime.1.8", "Microsoft.WindowsAppRuntime.1.8", "1.8.9", "")]
        fmt = "{:<40}{:<33}{:<18}{}\r\n"
        text = fmt.format("Nom", "ID", "Version", "Disponible") + "-" * 110 + "\r\n"
        text += "".join(fmt.format(*r).rstrip(" ") + "\r\n" if not r[3] else fmt.format(*r) for r in rows).replace("\r\n\r\n", "\r\n")
        m = apps.parse_winget_table(text)
        self.assertEqual(m["Git"], "Git.Git")
        self.assertEqual(m["Microsoft Office fr-fr"], "Microsoft.Office")  # espace insécable normalisé
        self.assertEqual(len(m), 3)

    def test_component_classification(self):
        from swap.catalog import COMPONENT_RE

        for name in ["Microsoft Visual C++ v14 Redistributable (x64) - 14.50", "Microsoft Windows Desktop Runtime 10.0.12 (x64)",
                     "Microsoft Intune Management Extension", "GLPI Agent 1.13", "Mozilla Maintenance Service",
                     "Microsoft Outlook 2016 - fr-fr", "Microsoft Teams Meeting Add-in for Microsoft Office",
                     "Instance AD LDS Annuaire", "Intel(R) Wireless Bluetooth Driver"]:
            self.assertTrue(COMPONENT_RE.search(name), name)
        for name in ["MariaDB ODBC Driver 64-bit", "Git", "Microsoft 365 Apps for enterprise - fr-fr", "WinSCP 6.5.5",
                     "Microsoft PowerBI Desktop (x64)", "KeePassXC", "Oracle ODAC 19 version 19.3.1"]:
            self.assertFalse(COMPONENT_RE.search(name), name)

    def test_winget_guess(self):
        self.assertEqual(apps.guess_winget("Google Chrome"), "Google.Chrome")
        self.assertEqual(apps.guess_winget("WinSCP 6.5.5"), "WinSCP.WinSCP")
        self.assertEqual(apps.guess_winget("Logiciel maison"), "")

    def test_setup_script_guesses_and_no_duplicate_version(self):
        inv = {"meta": {"machine": "PC1"}, "system": {}, "apps": [
            {"name": "Google Chrome", "version": "1", "publisher": "Google", "winget_id": "", "winget_guess": "Google.Chrome"},
            {"name": "Connector 8.0.19", "version": "8.0.19", "publisher": "Oracle", "winget_id": "", "winget_guess": ""}]}
        s = build_setup_script(inv)
        self.assertIn("winget install --id Google.Chrome", s)
        self.assertIn("Suggestions winget", s)
        self.assertIn("#   Connector 8.0.19  (Oracle)", s)

    def test_setup_script(self):
        inv = {
            "meta": {"machine": "PC1"},
            "system": {
                "drives": [{"letter": "Z:", "path": r"\\srv\compta", "user": ""}],
                "printers": [{"name": "HP", "driver": "d", "port": "IP_1", "network_path": r"\\srv\imp1", "default": True},
                             {"name": "Zebra", "driver": "z", "port": "USB001", "network_path": "", "default": False},
                             {"name": "Ricoh IP", "driver": "RICOH PCL6", "port": "IP_10.1.2.3", "network_path": "", "default": False}],
                "env": {"PROJ": "l'été", "Path": r"%USERPROFILE%\AppData\Local\Microsoft\WindowsApps;C:\Applications_CLMB\Framework;", "OneDrive": r"C:\Users\x\OneDrive"},
            },
            "apps": [{"name": "Git", "version": "2", "publisher": "", "winget_id": "Git.Git"},
                     {"name": "Sage 100", "version": "9", "publisher": "Sage", "winget_id": ""},
                     {"name": "VC++ redist", "version": "1", "publisher": "", "winget_id": "", "component": True}],
        }
        s = build_setup_script(inv)
        self.assertIn('net use Z: "\\\\srv\\compta" /persistent:yes', s)
        self.assertIn("Add-Printer -ConnectionName '\\\\srv\\imp1'", s)
        self.assertIn("SetEnvironmentVariable('PROJ', 'l''été', 'User')", s)
        self.assertIn("'C:\\Applications_CLMB\\Framework'", s)   # entrée PATH personnalisée conservée
        self.assertNotIn("WindowsApps", s)                       # entrée standard ignorée
        self.assertNotIn("SetEnvironmentVariable('OneDrive'", s)  # variable propre au profil : jamais recopiée
        self.assertIn("Add-PrinterPort -Name 'IP_10.1.2.3' -PrinterHostAddress '10.1.2.3'", s)
        self.assertIn("winget install --id Git.Git", s)
        self.assertIn("#   Sage 100", s)
        self.assertNotIn("VC++", s)
        self.assertEqual(psq("a'b"), "'a''b'")

    def test_slug_and_is_under(self):
        self.assertEqual(slugify("Été & Projets 2"), "ete-projets-2")
        self.assertTrue(is_under("/a/b/c", "/a/b"))
        self.assertFalse(is_under("/a/bc", "/a/b"))


if __name__ == "__main__":
    unittest.main()
