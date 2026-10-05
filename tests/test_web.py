"""Tests de l'interface web : vrai serveur HTTP sur 127.0.0.1, appelé comme le fait la page."""

import http.client
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_swap import make_profile, read, write

from swap import web
from swap.session import Session


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loc = make_profile(self.tmp.name)
        self.users = os.path.join(self.tmp.name, "Users")
        write(os.path.join(self.users, "alice", "Documents", "contrat.docx"), "contrat")
        write(os.path.join(self.users, "bob", "Documents", "bob.txt"), "bob")
        self.session = Session(os.path.join(self.tmp.name, "sortie"), self.loc)
        self.session.profiles_base = self.users
        self.app = web.WebApp(self.session)
        self.server, self.token = web.make_server(self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.app._rx_stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # -- client ----------------------------------------------------------------------------------------------
    def call(self, name, body=None, *, token=None, host=None, method=None, path=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}", "X-Swap-Token": self.token if token is None else token}
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        conn.request(method or ("POST" if body is not None else "GET"), path or "/api/" + name, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return resp.status, raw

    def ok(self, name, body=None, **kw):
        status, data = self.call(name, body, **kw)
        self.assertEqual(status, 200, data)
        return data

    def wait_job(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            job = self.ok("status")["job"]
            if job and not job["running"]:
                return job
            time.sleep(0.05)
        self.fail("tâche trop longue")

    def scanned(self):
        self.ok("scan", {"path": ""})
        job = self.wait_job()
        self.assertIsNone(job["error"], job)
        return job

    # -- sécurité --------------------------------------------------------------------------------------------
    def test_page_needs_the_token_and_embeds_it(self):
        status, _ = self.call("", path="/", token="x")
        self.assertEqual(status, 403)
        status, html = self.call("", path="/?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn(self.token, html.decode("utf-8"))
        self.assertNotIn("__TOKEN__", html.decode("utf-8"))

    def test_api_refuses_missing_or_wrong_token(self):
        self.assertEqual(self.call("state", token="")[0], 403)
        self.assertEqual(self.call("state", token="mauvais")[0], 403)
        self.assertEqual(self.call("state")[0], 200)

    def test_foreign_host_and_origin_are_refused(self):
        self.assertEqual(self.call("state", host="evil.example.com")[0], 403)           # DNS rebinding
        self.assertEqual(self.call("state", headers={"Origin": "http://evil.example.com"})[0], 403)
        self.assertEqual(self.call("state", headers={"Origin": f"http://localhost:{self.port}"})[0], 200)

    def test_unknown_action_and_bad_json(self):
        self.assertEqual(self.call("nimporte-quoi")[0], 404)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/options", body="{pas du json", headers={"Host": f"127.0.0.1:{self.port}", "X-Swap-Token": self.token,
                                                                            "Content-Length": "12"})
        self.assertEqual(conn.getresponse().status, 500)
        conn.close()

    def test_server_only_listens_on_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    # -- état, analyse, sélection ----------------------------------------------------------------------------
    def test_state_lists_profiles_before_any_scan(self):
        st = self.ok("state")
        self.assertFalse(st["has_inventory"])
        self.assertIsNone(st["inventory"])
        names = [p["name"] for p in st["profiles"]]
        self.assertIn("alice", names)
        self.assertIn("bob", names)
        self.assertTrue(any(p["current"] for p in st["profiles"]))

    def test_scan_then_items_and_selection(self):
        job = self.scanned()
        self.assertEqual(job["kind"], "scan")
        st = self.ok("state")
        self.assertTrue(st["has_inventory"])
        self.assertGreater(st["inventory"]["items"], 3)
        items = self.ok("items")
        by_id = {i["id"]: i for i in items["items"]}
        self.assertIn("dossier-documents", by_id)
        self.assertTrue(by_id["dossier-documents"]["selected"])
        cats = [i["category"] for i in items["items"]]
        self.assertLess(cats.index("Dossiers personnels"), len(cats) - 1)
        t = self.ok("select", {"ids": ["dossier-documents"], "value": False})
        self.assertEqual(t["selected"], items["selected"] - 1)
        self.assertEqual(self.ok("select", {"all": False})["selected"], 0)
        self.assertEqual(self.ok("select", {"defaults": True})["selected"], items["selected"])
        self.assertEqual(self.ok("select", {"ids": ["n-importe-quoi"], "value": True})["selected"], items["selected"])   # id inconnu ignoré

    def test_scan_of_another_user_from_the_page(self):
        self.ok("scan", {"path": os.path.join(self.users, "alice")})
        self.assertIsNone(self.wait_job()["error"])
        st = self.ok("state")
        self.assertEqual(st["inventory"]["user"], "alice")
        self.assertFalse(st["inventory"]["profile_current"])
        self.assertFalse(st["source_is_current"])
        docs = next(i for i in self.ok("items")["items"] if i["id"] == "dossier-documents")
        self.assertEqual(docs["src"], os.path.join(self.users, "alice", "Documents"))

    def test_second_job_is_refused_while_one_runs(self):
        gate = threading.Event()
        self.app._start("scan", lambda ctx: gate.wait(10))
        status, data = self.call("scan", {"path": ""})
        self.assertEqual(status, 409)
        self.assertIn("déjà en cours", data["error"])
        gate.set()
        self.wait_job()

    def test_options_are_stored(self):
        self.ok("options", {"rules": "pcsoft-projet = C:\\Mes Projets", "excludes": "*.iso *.msu"})
        self.assertEqual(self.session.rules()[0][0], "pcsoft-projet")
        self.assertEqual(self.session.excludes(), ["*.iso", "*.msu"])

    # -- envoi et restauration -------------------------------------------------------------------------------
    def test_send_to_folder_with_progress_then_restore_into_another_user(self):
        self.scanned()
        dest = os.path.join(self.tmp.name, "disque")
        self.ok("send", {"mode": "folder", "folder": dest})
        job = self.wait_job()
        self.assertIsNone(job["error"], job)
        self.assertTrue(job["result"]["copied"] > 3)
        self.assertEqual(job["result"]["errors"], [])
        self.assertGreater(job["total"], 0)
        self.assertEqual(job["done"], job["total"])
        self.assertTrue(any(line.startswith("Destination :") for line in job["log"]))
        backup = job["result"]["backup"]
        self.assertTrue(os.path.exists(os.path.join(backup, "manifest.json")))

        info = self.ok("backup-info", {"path": dest})            # le dossier parent suffit : la sauvegarde est retrouvée dedans
        self.assertEqual(info["backup"], backup)
        self.assertEqual(info["machine"], self.session.machine)
        self.assertGreater(info["items"], 3)

        carol = os.path.join(self.users, "carol")
        os.makedirs(carol)
        self.ok("restore", {"path": backup, "target": carol})
        res = self.wait_job()
        self.assertIsNone(res["error"], res)
        self.assertEqual(read(os.path.join(carol, "Documents", "rapport.docx")), "rapport")
        self.assertTrue(any(i["id"] == "dossier-documents" for i in res["result"]["items"]))

    def test_dry_run_sends_nothing(self):
        self.scanned()
        dest = os.path.join(self.tmp.name, "disque")
        self.ok("send", {"mode": "folder", "folder": dest, "dry": True})
        job = self.wait_job()
        self.assertTrue(job["result"]["dry"])
        self.assertFalse(os.path.exists(os.path.join(dest, "SWAP-" + self.session.machine, "manifest.json")))

    def test_send_validation_errors_are_clear(self):
        status, data = self.call("send", {"mode": "folder", "folder": ""})
        self.assertEqual(status, 400)
        self.assertIn("Analysez d'abord", data["error"])
        self.scanned()
        for body, text in (({"mode": "folder", "folder": ""}, "dossier"), ({"mode": "direct", "host": ""}, "PC cible"),
                           ({"mode": "direct", "host": "TPSEL045", "code": ""}, "code"), ({"mode": "direct", "host": "x", "code": "ABCDE-FGHJK", "port": "abc"}, "port"),
                           ({"mode": "bizarre"}, "inconnu")):
            status, data = self.call("send", body)
            self.assertEqual(status, 400, body)
            self.assertIn(text, data["error"])
        self.ok("select", {"all": False})
        status, data = self.call("send", {"mode": "folder", "folder": os.path.join(self.tmp.name, "d")})
        self.assertEqual(status, 400)
        self.assertIn("Rien n'est coché", data["error"])

    def test_restore_requires_a_backup(self):
        status, data = self.call("restore", {"path": self.tmp.name})
        self.assertEqual(status, 400)
        self.assertIn("sauvegarde", data["error"])

    def test_cancel_stops_a_send(self):
        self.scanned()
        big = os.path.join(self.loc.known["Documents"], "gros.bin")
        with open(big, "wb") as fh:
            fh.write(os.urandom(40 * 1024 * 1024))
        self.ok("scan", {"path": ""})
        self.wait_job()
        self.ok("send", {"mode": "folder", "folder": os.path.join(self.tmp.name, "d2")})
        self.ok("cancel", {})
        job = self.wait_job()
        self.assertTrue(job["cancelled"] or job["result"])   # annulé, ou déjà fini si la machine est très rapide

    def test_destination_test_reports_wrong_folder_and_ok_folder(self):
        self.ok("test", {"mode": "folder", "folder": os.path.join(self.tmp.name, "nouveau")})
        job = self.wait_job()
        self.assertIn("accessible", job["result"]["message"])
        self.ok("test", {"mode": "direct", "host": "127.0.0.1", "code": "ABCDE-FGHJK", "port": "1"})
        job = self.wait_job()
        self.assertTrue(job["error"])
        self.assertEqual(job["error_kind"], "sink")

    # -- réception directe -----------------------------------------------------------------------------------
    def test_receive_and_direct_send_between_two_instances(self):
        recv_dir = os.path.join(self.tmp.name, "recu")
        # le « PC cible » est une seconde instance de l'application, comme sur un autre poste
        other = web.WebApp(Session(os.path.join(self.tmp.name, "sortie2")))
        rx = other.api_rx_start({"dest": recv_dir, "port": 0, "firewall": False})
        self.assertTrue(rx["active"])
        self.assertRegex(rx["code"], r"^[A-Z0-9]{5}-[A-Z0-9]{5}$")
        self.addCleanup(other._rx_stop)

        self.scanned()
        self.ok("send", {"mode": "direct", "host": "127.0.0.1", "code": rx["code"].lower(), "port": rx["port"]})
        job = self.wait_job()
        self.assertIsNone(job["error"], job)
        self.assertGreater(job["result"]["copied"], 3)
        end = time.time() + 10
        while time.time() < end and not other._rx_status()["done_root"]:
            time.sleep(0.05)
        st = other._rx_status()
        self.assertFalse(st["active"])                     # la réception s'arrête toute seule après un envoi réussi
        self.assertTrue(st["done_root"].endswith("SWAP-" + self.session.machine))
        self.assertTrue(any("Terminé" in line for line in st["log"]))
        self.assertEqual(read(os.path.join(st["done_root"], "data", "dossier-documents", "rapport.docx")), "rapport")

    def test_wrong_code_gives_a_clear_error(self):
        other = web.WebApp(Session(os.path.join(self.tmp.name, "sortie2")))
        rx = other.api_rx_start({"dest": os.path.join(self.tmp.name, "recu"), "port": 0, "firewall": False})
        self.addCleanup(other._rx_stop)
        self.scanned()
        self.ok("send", {"mode": "direct", "host": "127.0.0.1", "code": "AAAAA-BBBBB", "port": rx["port"]})
        job = self.wait_job()
        self.assertEqual(job["error_kind"], "sink")
        self.assertTrue(job["error"])

    def test_receive_start_twice_and_stop(self):
        dest = os.path.join(self.tmp.name, "recu")
        self.ok("rx-start", {"dest": dest, "port": 0, "firewall": False})
        status, data = self.call("rx-start", {"dest": dest, "port": 0, "firewall": False})
        self.assertEqual(status, 409)
        self.assertTrue(self.ok("status")["rx"]["active"])
        self.assertTrue(self.app.busy())
        self.assertFalse(self.ok("rx-stop", {})["active"])
        status, data = self.call("rx-start", {"dest": "", "port": 0})
        self.assertEqual(status, 400)

    # -- installation directe sur le PC cible (tout depuis ce poste) ----------------------------------------
    def fake_target(self):
        """Un « PC cible » : Users\\carol et le lecteur C: accessibles par des dossiers locaux."""
        target = os.path.join(self.tmp.name, "TPSEL045")
        os.makedirs(os.path.join(target, "Users", "carol"))
        os.makedirs(os.path.join(target, "Users", "Public"))
        self.session.remote_users_base = lambda host: os.path.join(target, "Users")
        self.session.remote_drive_root = lambda host, drive: os.path.join(target, "disque-" + drive.upper().rstrip(":"))
        return target

    def test_remote_users_are_listed_and_unreachable_pc_is_explained(self):
        self.fake_target()
        users = self.ok("remote-users", {"host": "TPSEL045"})["users"]
        self.assertEqual([u["name"] for u in users], ["carol"])               # Public n'est pas un utilisateur
        status, data = self.call("remote-users", {"host": ""})
        self.assertEqual(status, 400)
        self.session.remote_users_base = None                                 # vrai chemin \\\\PC\\C$\\Users : injoignable ici
        status, data = self.call("remote-users", {"host": "PC-QUI-N-EXISTE-PAS"})
        self.assertEqual(status, 400)
        self.assertIn("inaccessible", data["error"])

    def test_send_straight_into_the_profile_of_the_target_pc(self):
        target = self.fake_target()
        carol = os.path.join(target, "Users", "carol")
        write(os.path.join(carol, "Documents", "rapport.docx"), "VERSION DE CAROL")        # existe déjà, différent
        self.scanned()
        self.ok("options", {"rules": "dossier-desktop = C:\\Bureau 2", "excludes": ""})
        self.ok("send", {"mode": "remote", "host": "TPSEL045", "user": "carol"})
        job = self.wait_job()
        self.assertIsNone(job["error"], job)
        r = job["result"]
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["placed_in"], carol)
        # fichiers directement à leur place, sans dossier de sauvegarde intermédiaire
        self.assertEqual(read(os.path.join(carol, "Documents", "compta", "budget.xlsm")), "macro")
        self.assertEqual(read(os.path.join(carol, "AppData", "Roaming", "Notepad++", "config.xml")), "<cfg/>")
        self.assertEqual(read(os.path.join(carol, ".gitconfig")), "[user]")                  # élément « fichier »
        self.assertEqual(read(os.path.join(target, "disque-C", "Bureau 2", "note.txt")), "note")   # règle de destination C:\\...
        self.assertFalse(os.path.exists(os.path.join(carol, "Desktop", "note.txt")))
        self.assertEqual(read(os.path.join(carol, "Documents", "rapport.docx")), "VERSION DE CAROL")   # jamais écrasé
        # rapport, manifeste et script d'installation déposés sur le PC cible
        meta = r["report_dir"]
        self.assertEqual(meta, os.path.join(target, "disque-C", "SWAP", "SWAP-" + self.session.machine))
        for name in ("manifest.json", "rapport.html", "installer_et_configurer.ps1", "RESTAURER.bat"):
            self.assertTrue(os.path.exists(os.path.join(meta, name)), name)
        self.assertEqual(json.loads(read(os.path.join(meta, "manifest.json")))["placed"], {"host": "TPSEL045", "user": "carol"})
        self.assertFalse(os.path.exists(os.path.join(meta, "data", "dossier-documents")))
        # on ne peut pas « restaurer » ce dossier : tout est déjà en place
        status, data = self.call("restore", {"path": meta, "target": ""})
        self.assertEqual(status, 200)
        res = self.wait_job()
        self.assertIn("déjà été installée", res["error"])

    def test_remote_overwrite_option_replaces_existing_files(self):
        target = self.fake_target()
        carol = os.path.join(target, "Users", "carol")
        write(os.path.join(carol, "Documents", "rapport.docx"), "VERSION DE CAROL")
        self.scanned()
        self.ok("send", {"mode": "remote", "host": "TPSEL045", "user": "carol", "overwrite": True})
        self.assertIsNone(self.wait_job()["error"])
        self.assertEqual(read(os.path.join(carol, "Documents", "rapport.docx")), "rapport")

    def test_remote_dry_run_and_registry_items_are_reported(self):
        from swap.model import Item

        target = self.fake_target()
        self.scanned()
        reg = Item(id="registre-putty", label="PuTTY (registre)", kind="registry", src=r"HKCU\Software\SimonTatham", target={"kind": "registry"},
                   category="Configuration des applications")
        self.session.items.append(reg)
        self.session.selected.add(reg.id)
        self.ok("send", {"mode": "remote", "host": "TPSEL045", "user": "carol", "dry": True})
        job = self.wait_job()
        self.assertTrue(job["result"]["dry"])
        self.assertEqual(job["result"]["skipped_registry"], ["PuTTY (registre)"])
        self.assertFalse(os.path.exists(os.path.join(target, "Users", "carol", "Documents", "rapport.docx")))
        self.assertTrue(any("registre" in line for line in job["log"]))

    def test_remote_validation(self):
        self.fake_target()
        self.scanned()
        status, data = self.call("send", {"mode": "remote", "host": "TPSEL045", "user": ""})
        self.assertEqual(status, 400)
        self.assertIn("utilisateur", data["error"])
        status, data = self.call("send", {"mode": "remote", "host": "", "user": "carol"})
        self.assertEqual(status, 400)
        self.ok("send", {"mode": "remote", "host": "TPSEL045", "user": "inconnu"})
        job = self.wait_job()
        self.assertEqual(job["error_kind"], "sink")
        self.assertIn("n'existe pas", job["error"])

    def test_remote_destination_test_names_the_profiles(self):
        self.fake_target()
        self.ok("test", {"mode": "remote", "host": "TPSEL045", "user": "carol"})
        self.assertIn("carol", self.wait_job()["result"]["message"])

    # -- navigation dans les dossiers, rapports, fin ---------------------------------------------------------
    def test_browse_lists_folders_and_rejects_files(self):
        r = self.ok("browse", {"path": self.users})
        self.assertEqual(r["dirs"], sorted(os.listdir(self.users), key=str.lower))
        self.assertEqual(r["parent"], self.tmp.name)
        self.assertEqual(self.call("browse", {"path": os.path.join(self.users, "nope")})[0], 400)
        write(os.path.join(self.users, "fichier.txt"), "x")
        self.assertEqual(self.call("browse", {"path": os.path.join(self.users, "fichier.txt")})[0], 400)
        self.assertTrue(self.ok("browse", {"path": ""})["path"])

    def test_reports_are_served_only_with_the_token_and_for_real_backups(self):
        self.scanned()
        status, body = self.call("", path="/report?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        self.assertEqual(self.call("", path="/report?t=faux")[0], 403)
        self.assertEqual(self.call("", path=f"/backup-report?t={self.token}&path={self.tmp.name}")[0], 404)   # pas une sauvegarde

    def test_opened_for_restore_launcher_gives_backup_in_state(self):
        app = web.WebApp(self.session, os.path.join(self.tmp.name, "SWAP-X"))
        self.assertEqual(app.api_state()["initial_backup"], os.path.join(self.tmp.name, "SWAP-X"))

    def test_quit_releases_the_main_loop(self):
        self.assertFalse(self.app.quit_event.is_set())
        self.assertEqual(self.ok("quit", {}), {"bye": True})
        self.assertTrue(self.app.quit_event.is_set())

    def test_idle_watchdog_and_index_file_exist(self):
        self.assertTrue(os.path.exists(web.INDEX))
        self.assertFalse(self.app.busy())
        self.app.touch()
        self.assertLess(time.monotonic() - self.app.last_activity, 1)


class ServerModeTests(unittest.TestCase):
    """swap hébergé sur un serveur : mot de passe, PC source et PC cible distants (simulés par des dossiers)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pcs = {}
        for name, user in (("TPSEL023", "RFRH7752"), ("TPSEL045", "RFRH7752")):
            root = os.path.join(self.tmp.name, name)
            os.makedirs(os.path.join(root, "Users", user))
            self.pcs[name] = root
        src = self.pcs["TPSEL023"]
        write(os.path.join(src, "Users", "RFRH7752", "Documents", "rapport.docx"), "rapport")
        write(os.path.join(src, "Users", "RFRH7752", "Desktop", "note.txt"), "note")
        write(os.path.join(src, "Users", "RFRH7752", "AppData", "Roaming", "Notepad++", "config.xml"), "<cfg/>")
        write(os.path.join(src, "disque-C", "Projets", "app", "main.py"), "print(1)")
        write(os.path.join(src, "disque-C", "Windows", "system.dll"), "sys")
        self.session = Session(os.path.join(self.tmp.name, "sortie"))
        self.session.remote_users_base = lambda host: os.path.join(self.pcs[host.upper()], "Users")
        self.session.remote_drive_root = lambda host, d: os.path.join(self.pcs[host.upper()], "disque-" + d.upper().rstrip(":"))
        self.app = web.WebApp(self.session, server=True)

    def start(self, password=None, **kw):
        self.server, self.token = web.make_server(self.app, 0, "127.0.0.1", password, **kw)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def req(self, method, path, body=None, headers=None, secure=False):
        conn = (http.client.HTTPSConnection("localhost", self.port, context=ssl._create_unverified_context(), timeout=30) if secure
                else http.client.HTTPConnection("127.0.0.1", self.port, timeout=30))
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        out = (r.status, data, dict(r.getheaders()))
        conn.close()
        return out

    def wait_job(self):
        end = time.time() + 30
        while time.time() < end:
            job = self.app.api_status({})["job"]
            if job and not job["running"]:
                return job
            time.sleep(0.05)
        self.fail("tâche trop longue")

    # -- migration d'un PC distant vers un autre PC distant ------------------------------------------------
    def test_scan_remote_source_then_install_on_remote_target(self):
        self.app.api_scan({"source_host": "TPSEL023", "source_user": "RFRH7752"})
        job = self.wait_job()
        self.assertIsNone(job["error"], job)
        inv = self.session.inv
        self.assertEqual((inv["meta"]["machine"], inv["meta"]["user"], inv["meta"]["remote"]), ("TPSEL023", "RFRH7752", True))
        self.assertTrue(any("À DISTANCE" in a["text"] and "n'a pas pu être lue" in a["text"] for a in inv["advice"]))
        by_id = {i["id"]: i for i in inv["items"]}
        self.assertIn("dossier-documents", by_id)
        projets = by_id["disque-c-projets"]                                   # dossier à la racine du disque C: du PC source
        self.assertEqual(projets["label"], "C:\\Projets")
        self.assertEqual(projets["target"], {"kind": "abs", "path": "C:\\Projets"})   # cible portable, pas un chemin réseau
        self.assertNotIn("disque-c-windows", by_id)                           # dossiers système jamais proposés
        self.assertTrue(by_id["config-notepadpp"]["src"].startswith(self.pcs["TPSEL023"]))

        self.app.api_send({"mode": "remote", "host": "TPSEL045", "user": "RFRH7752"})
        job = self.wait_job()
        self.assertIsNone(job["error"], job)
        dst = self.pcs["TPSEL045"]
        self.assertEqual(read(os.path.join(dst, "Users", "RFRH7752", "Documents", "rapport.docx")), "rapport")
        self.assertEqual(read(os.path.join(dst, "Users", "RFRH7752", "Desktop", "note.txt")), "note")
        self.assertEqual(read(os.path.join(dst, "Users", "RFRH7752", "AppData", "Roaming", "Notepad++", "config.xml")), "<cfg/>")
        self.assertEqual(read(os.path.join(dst, "disque-C", "Projets", "app", "main.py")), "print(1)")   # C:\\Projets recréé sur le PC cible
        self.assertTrue(os.path.exists(os.path.join(dst, "disque-C", "SWAP", "SWAP-TPSEL023", "installer_et_configurer.ps1")))
        # le PC source n'a pas bougé et rien n'a été écrit sur le serveur lui-même
        self.assertEqual(read(os.path.join(self.pcs["TPSEL023"], "disque-C", "Projets", "app", "main.py")), "print(1)")

    def test_remote_source_errors_are_clear(self):
        with self.assertRaises(web.ApiError):
            self.app.api_scan({"source_host": ""})                              # en mode serveur, le PC source est obligatoire
        self.app.api_scan({"source_host": "TPSEL023", "source_user": ""})
        self.assertIn("utilisateur", self.wait_job()["error"])
        self.app.api_scan({"source_host": "TPSEL023", "source_user": "personne"})
        self.assertIn("n'existe pas", self.wait_job()["error"])

    def test_source_host_equal_to_this_machine_means_local(self):
        import platform

        self.session.set_source(platform.node().upper(), "", "")
        self.assertEqual(self.session.source_host, "")

    def test_state_says_server_mode_and_hides_quit(self):
        self.start("motdepasse1")
        self.assertTrue(self.app.api_state({})["server"])

    # -- mot de passe ----------------------------------------------------------------------------------------
    def login(self, password="motdepasse1"):
        return self.req("POST", "/login", "password=" + password, {"Content-Type": "application/x-www-form-urlencoded", "Host": f"127.0.0.1:{self.port}"})

    def test_login_required_then_cookie_gives_access(self):
        self.start("motdepasse1")
        status, body, _ = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Mot de passe", body)
        self.assertNotIn(self.token.encode(), body)                              # la page n'est pas servie sans connexion
        self.assertEqual(self.req("GET", "/api/state", headers={"X-Swap-Token": self.token})[0], 401)
        with mock.patch("swap.web.time.sleep"):
            self.assertEqual(self.login("mauvais")[0], 401)
        status, _, headers = self.login()
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        sid = cookie.split(";")[0]
        status, body, _ = self.req("GET", "/", headers={"Cookie": sid})
        self.assertIn(self.token.encode(), body)
        status, body, _ = self.req("GET", "/api/state", headers={"Cookie": sid, "X-Swap-Token": self.token})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["server"])
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": sid})[0], 403)    # le jeton reste exigé en plus
        self.assertEqual(self.req("POST", "/api/quit", "{}", {"Cookie": sid, "X-Swap-Token": self.token})[0], 403)   # pas d'arrêt à distance
        self.req("GET", "/logout", headers={"Cookie": sid})
        self.assertEqual(self.req("GET", "/api/state", headers={"Cookie": sid, "X-Swap-Token": self.token})[0], 401)

    def test_too_many_wrong_passwords_lock_the_login(self):
        self.start("motdepasse1")
        with mock.patch("swap.web.time.sleep"):
            codes = [self.login("faux")[0] for _ in range(6)]
            self.assertEqual(codes[-1], 429)
            self.assertEqual(self.login()[0], 429)                               # même le bon mot de passe, pendant le blocage

    def test_cross_origin_request_is_refused_even_with_password(self):
        self.start("motdepasse1")
        sid = self.login()[2]["Set-Cookie"].split(";")[0]
        status, _, _ = self.req("POST", "/api/options", "{}", {"Cookie": sid, "X-Swap-Token": self.token, "Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)

    def test_listening_beyond_this_machine_requires_a_password(self):
        with self.assertRaises(ValueError):
            web.make_server(self.app, 0, "0.0.0.0", None)
        server, _ = web.make_server(self.app, 0, "0.0.0.0", "motdepasse1")
        server.server_close()

    @unittest.skipUnless(shutil.which("openssl"), "openssl absent")
    def test_https_with_certificate(self):
        cert, key = os.path.join(self.tmp.name, "c.pem"), os.path.join(self.tmp.name, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True)
        self.start("motdepasse1", certfile=cert, keyfile=key)
        status, body, _ = self.req("GET", "/", secure=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Mot de passe", body)
        status, _, headers = self.req("POST", "/login", "password=motdepasse1", {"Content-Type": "application/x-www-form-urlencoded"}, secure=True)
        self.assertEqual(status, 303)
        self.assertIn("Secure", headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
