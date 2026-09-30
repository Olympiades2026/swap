"""Tests de l'interface graphique. Nécessitent tkinter et un écran (sous Linux : xvfb-run -a python3 -m unittest)."""

import os
import tempfile
import time
import unittest
from unittest import mock

try:
    import tkinter

    _root = tkinter.Tk()
    _root.destroy()
    TK_OK = True
except Exception:  # noqa: BLE001 - pas de tkinter ou pas d'écran : les tests sont ignorés
    TK_OK = False

from test_swap import make_profile, read, write  # noqa: E402

from swap.locations import Locations  # noqa: E402
from swap.session import Session  # noqa: E402


def pump(app, cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if cond():
            for _ in range(5):
                app.update()
                time.sleep(0.02)
            return
        time.sleep(0.02)
    raise TimeoutError("l'interface n'a pas terminé à temps")


def wait_job(app):
    pump(app, lambda: app.job is not None and app.job.finished.is_set())
    end = time.time() + 0.5  # laisse la boucle d'interface traiter la fin de la tâche (rappel « done »)
    while time.time() < end:
        app.update()
        time.sleep(0.02)


@unittest.skipUnless(TK_OK, "tkinter ou écran indisponible")
class GuiTests(unittest.TestCase):
    def setUp(self):
        from swap import gui

        self.gui = gui
        self.tmp = tempfile.TemporaryDirectory()
        self.loc = make_profile(self.tmp.name)
        self.session = Session(os.path.join(self.tmp.name, "sortie"), self.loc)
        patches = [mock.patch.object(gui.messagebox, n) for n in ("showinfo", "showwarning", "showerror", "askokcancel")]
        self.boxes = [p.start() for p in patches]
        self.info, self.warn, self.error, self.ask = self.boxes
        self.ask.return_value = True
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.app = gui.App(self.session)
        self.app.update()
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.app._rx_stop()
            self.app.destroy()
        except tkinter.TclError:
            pass
        self.tmp.cleanup()

    def scan(self):
        self.app.on_scan()
        wait_job(self.app)
        self.app.update()

    def test_window_structure(self):
        tabs = [self.app.nb.tab(i, "text") for i in range(self.app.nb.index("end"))]
        self.assertEqual(len(tabs), 4)
        self.assertIn("Analyser", tabs[0])
        self.assertIn("Recevoir", tabs[3])
        self.assertIn("(ce poste)", self.app.lbl_source.cget("text"))

    def test_scan_fills_summary_and_list(self):
        self.scan()
        self.assertIsNotNone(self.session.inv)
        rows = self.app.tree.get_children()
        self.assertEqual(len(rows), len(self.session.items))
        self.assertIn("proposé(s) par défaut", self.app.txt_summary.get("1.0", "end"))
        self.assertEqual(self.app.nb.index(self.app.nb.select()), 1)          # passe à l'onglet « Choisir »
        self.assertEqual(str(self.app.btn_report.cget("state")), "normal")
        self.assertTrue(os.path.exists(self.session.report_path()))

    def test_checkbox_toggle_filter_and_totals(self):
        self.scan()
        first = self.app.tree.get_children()[0]
        was = first in self.session.selected
        self.app._toggle(first)
        self.assertEqual(first in self.session.selected, not was)
        self.assertEqual(self.app.tree.set(first, "check"), "☑" if not was else "☐")
        self.app._select(False)
        self.assertIn("0 coché(s)", self.app.v_totals.get())
        self.app.v_filter.set("documents")
        self.app.update()
        shown = [self.app.tree.set(i, "label") for i in self.app.tree.get_children()]
        self.assertTrue(shown and all("ocuments" in s or "documents" in s.lower() for s in shown))
        self.app.v_filter.set("")
        self.app._select_defaults()
        self.assertEqual(len(self.session.selected), len([i for i in self.session.items if i.default]))

    def test_real_mouse_click_on_checkbox_column(self):
        self.scan()
        self.app.update()
        first = self.app.tree.get_children()[0]
        bbox = self.app.tree.bbox(first, "check")
        self.assertTrue(bbox, "la ligne doit être visible")
        before = first in self.session.selected
        self.app.tree.event_generate("<Button-1>", x=bbox[0] + 5, y=bbox[1] + 5)
        self.app.update()
        self.assertEqual(first in self.session.selected, not before)

    def test_rules_and_excludes_are_read_from_the_widgets(self):
        self.scan()
        self.app.txt_rules.delete("1.0", "end")
        self.app.txt_rules.insert("1.0", "pcsoft-projet = C:\\Mes Projets")
        self.app.v_excl.set("*.iso, *.msu")
        self.app._sync_options()
        self.assertEqual(self.session.rules(), [("pcsoft-projet", "C:\\Mes Projets")])
        self.assertEqual(self.session.excludes(), ["*.iso", "*.msu"])

    def test_send_to_folder_with_progress_then_restore(self):
        self.scan()
        dest = os.path.join(self.tmp.name, "disque")
        self.app.v_mode.set("folder")
        self.app.v_folder.set(dest)
        self.app.on_send()
        wait_job(self.app)
        self.assertIsNone(self.app.job.error)
        backup = os.path.join(dest, "SWAP-" + self.session.machine)
        self.assertTrue(os.path.exists(os.path.join(backup, "manifest.json")))
        self.assertTrue(os.path.exists(os.path.join(backup, "RESTAURER.bat")))
        self.assertEqual(self.app.v_backup.get(), backup)                     # prêt à restaurer
        self.assertIn("Envoi terminé", self.app.v_send_status.get())
        self.assertGreaterEqual(float(self.app.send_bar["value"]), 99)

        base = os.path.join(self.tmp.name, "nouveau")
        self.session.loc = Locations(base, os.path.join(base, "R"), os.path.join(base, "L"), {n: os.path.join(base, n) for n in self.loc.known})
        self.app.txt_restore_rules.insert("1.0", f"dossier-desktop = {os.path.join(self.tmp.name, 'Ailleurs')}")
        self.app.on_restore()
        wait_job(self.app)
        self.assertIsNone(self.app.job.error)
        self.assertEqual(read(os.path.join(base, "Documents", "rapport.docx")), "rapport")
        self.assertEqual(read(os.path.join(self.tmp.name, "Ailleurs", "note.txt")), "note")   # règle appliquée
        self.assertIn("Restauration terminée", self.app.v_restore_status.get())
        self.assertIn("dossier-documents", self.app.txt_restore_log.get("1.0", "end"))

    def test_direct_transfer_between_two_pcs(self):
        self.scan()
        recv = os.path.join(self.tmp.name, "recu")
        self.app.v_rx_dest.set(recv)
        self.app.v_rx_port.set("0")
        self.app.v_rx_fw.set(False)
        self.app.on_rx_toggle()                                                # ---- PC cible : démarre la réception
        self.assertIsNotNone(self.app.rx)
        code = self.app.v_rx_code.get()
        self.assertRegex(code, r"^[A-Z2-9]{5}-[A-Z2-9]{5}$")
        self.assertIn("port", self.app.v_rx_info.get())

        self.app.v_mode.set("direct")                                          # ---- PC source : envoie
        self.app.v_host.set("127.0.0.1")
        self.app.v_code.set(code.lower())
        self.app.v_port.set(str(self.app.rx.port))
        self.app.on_test_destination()
        wait_job(self.app)
        self.assertIn("Destination accessible", self.app.v_send_status.get())
        self.app.on_send()
        wait_job(self.app)
        self.assertIsNone(self.app.job.error, self.app.job.trace)
        pump(self.app, lambda: self.app.rx is None)                            # la réception se termine seule
        backup = os.path.join(recv, "SWAP-" + self.session.machine)
        self.assertTrue(os.path.exists(os.path.join(backup, "manifest.json")))
        self.assertEqual(self.app.v_backup.get(), backup)
        self.assertEqual(self.app.v_rx_code.get(), "")
        self.assertTrue(any("Réception terminée" in str(c) for c in self.info.call_args_list))

    def test_wrong_code_gives_clear_error(self):
        self.scan()
        self.app.v_rx_dest.set(os.path.join(self.tmp.name, "recu"))
        self.app.v_rx_port.set("0")
        self.app.v_rx_fw.set(False)
        self.app.on_rx_toggle()
        self.app.v_mode.set("direct")
        self.app.v_host.set("127.0.0.1")
        self.app.v_code.set("AAAAA-BBBBB")
        self.app.v_port.set(str(self.app.rx.port))
        self.app.on_test_destination()
        wait_job(self.app)
        self.assertEqual(self.error.call_count, 1)
        self.assertIn("code incorrect", self.error.call_args[0][1])
        self.assertIn("inaccessible", self.app.v_send_status.get())

    def test_missing_fields_are_reported_before_anything_starts(self):
        self.scan()
        self.app.v_mode.set("direct")
        self.app.v_host.set("")
        self.app.on_send()
        self.assertIn("PC cible", self.warn.call_args[0][1])
        self.app.v_host.set("TPSEL045")
        self.app.v_code.set("")
        self.app.on_send()
        self.assertIn("code", self.warn.call_args[0][1])
        self.app.v_mode.set("folder")
        self.app.v_folder.set("")
        self.app.on_send()
        self.assertIn("dossier", self.warn.call_args[0][1])
        self.assertFalse(self.ask.called)                                      # aucune confirmation, rien lancé

    def test_send_before_scan_redirects_to_first_tab(self):
        self.app.v_mode.set("folder")
        self.app.v_folder.set(self.tmp.name)
        self.app.on_send()
        self.assertEqual(self.app.nb.index(self.app.nb.select()), 0)
        self.assertTrue(self.info.called)

    def test_cancel_stops_job_and_reports_it(self):
        started = []

        def slow(ctx):
            started.append(True)
            while True:
                ctx.progress("occupé...")
                time.sleep(0.02)

        self.app.start_job(slow, self.app.ui_send)
        pump(self.app, lambda: bool(started))
        self.assertEqual(str(self.app.btn_send_cancel.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_send.cget("state")), "disabled")
        self.app.on_cancel()
        wait_job(self.app)
        self.assertTrue(self.app.job.cancelled)
        self.assertEqual(str(self.app.btn_send.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_send_cancel.cget("state")), "disabled")

    def test_second_job_is_refused_while_one_runs(self):
        release = []

        def wait(ctx):
            while not release:
                ctx.check()
                time.sleep(0.02)

        from types import SimpleNamespace

        neutral = SimpleNamespace(**dict(vars(self.app.ui_send), done=lambda job: None))
        self.app.start_job(wait, neutral)
        self.app.start_job(wait, neutral)
        self.assertEqual(self.info.call_count, 1)
        release.append(1)
        wait_job(self.app)

    def test_dry_run_sends_nothing(self):
        self.scan()
        dest = os.path.join(self.tmp.name, "vide")
        self.app.v_mode.set("folder")
        self.app.v_folder.set(dest)
        self.app.v_dry.set(True)
        self.app.on_send()
        wait_job(self.app)
        self.assertIn("Simulation terminée", self.app.v_send_status.get())
        self.assertFalse(os.path.exists(os.path.join(dest, "SWAP-" + self.session.machine)))
        self.assertFalse(self.ask.called)                                      # pas de confirmation pour une simulation

    def test_opened_from_restore_launcher_goes_straight_to_restore(self):
        backup = os.path.join(self.tmp.name, "SWAP-TPSEL023")
        app2 = self.gui.App(Session(os.path.join(self.tmp.name, "s2"), self.loc), backup=backup)
        try:
            app2.update()
            self.assertEqual(app2.nb.index(app2.nb.select()), 3)
            self.assertEqual(app2.v_backup.get(), backup)
        finally:
            app2.destroy()

    def test_share_mode_shows_unc_path(self):
        self.app.v_mode.set("share")
        self.app.v_host.set("TPSEL045")
        self.app.update()
        self.assertEqual(self.app.lbl_unc.cget("text"), "\\\\TPSEL045\\C$\\SWAP")
        self.app.v_mode.set("folder")
        self.app._sync_mode()
        self.assertEqual(self.app.lbl_unc.cget("text"), "")

    def test_load_existing_inventory(self):
        self.scan()
        path = os.path.join(self.session.out_dir, "inventaire.json")
        other = Session(os.path.join(self.tmp.name, "autre"), self.loc)
        app2 = self.gui.App(other)
        try:
            with mock.patch.object(self.gui.filedialog, "askopenfilename", return_value=path):
                app2.on_load_inventory()
            self.assertEqual(len(app2.tree.get_children()), len(other.items))
            self.assertGreater(len(other.items), 0)
        finally:
            app2.destroy()

    def test_error_in_a_tk_callback_is_shown_not_swallowed(self):
        try:
            raise ValueError("boum")
        except ValueError:
            import sys

            self.app.report_callback_exception(*sys.exc_info())
        self.assertIn("boum", self.error.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
