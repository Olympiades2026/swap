import json
import os
import tempfile
import unittest
from unittest import mock

from swap import apps, cli, fs, transfer
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

    def test_setup_script(self):
        inv = {
            "meta": {"machine": "PC1"},
            "system": {
                "drives": [{"letter": "Z:", "path": r"\\srv\compta", "user": ""}],
                "printers": [{"name": "HP", "driver": "d", "port": "IP_1", "network_path": r"\\srv\imp1", "default": True},
                             {"name": "Zebra", "driver": "z", "port": "USB001", "network_path": "", "default": False}],
                "env": {"PROJ": "l'été", "PATH": "x"},
            },
            "apps": [{"name": "Git", "version": "2", "publisher": "", "winget_id": "Git.Git"},
                     {"name": "Sage 100", "version": "9", "publisher": "Sage", "winget_id": ""},
                     {"name": "VC++ redist", "version": "1", "publisher": "", "winget_id": "", "component": True}],
        }
        s = build_setup_script(inv)
        self.assertIn('net use Z: "\\\\srv\\compta" /persistent:yes', s)
        self.assertIn("Add-Printer -ConnectionName '\\\\srv\\imp1'", s)
        self.assertIn("SetEnvironmentVariable('PROJ', 'l''été', 'User')", s)
        self.assertNotIn("'PATH'", s)
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
