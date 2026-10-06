"""Transfert direct d'un PC à un autre : connexion TCP chiffrée et authentifiée par un code.

Le PC cible lance la réception et affiche un code ; le PC source se connecte avec ce code. Sans le code, personne ne peut
envoyer ni lire de données. Rien à installer de plus que cet outil.

Sécurité : le code (50 bits) sert à dériver une clé (PBKDF2-HMAC-SHA256) ; les deux PC se prouvent mutuellement qu'ils la
connaissent, puis chaque message est chiffré (flux SHAKE-256) et authentifié (HMAC-SHA256, numéro de séquence). Cette
construction n'utilise que la bibliothèque standard de Python ; elle n'a PAS été auditée : elle convient pour un transfert
sur un réseau interne, pas pour traverser Internet.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
from typing import Callable, Optional

from .sink import CHUNK, PART, Cancelled, SinkError, file_hash, is_current
from .util import decode_output, is_under, is_windows, long_path

DEFAULT_PORT = 47800
MAGIC = b"SWAP1"
MAX_FRAME = 4 * 1024 * 1024
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O/1/I pour éviter les erreurs de saisie
KDF_ROUNDS = 150_000


class ProtocolError(Exception):
    pass


# --- code et clés -----------------------------------------------------------------------------------------------
def make_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
    return raw[:5] + "-" + raw[5:]


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


@functools.lru_cache(maxsize=8)
def master_key(code: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode(), b"swap-v1", KDF_ROUNDS, 32)


def _mac(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


def _session_keys(master: bytes, cn: bytes, sn: bytes, direction: str) -> tuple:
    return _mac(master, b"enc-" + direction.encode(), cn, sn), _mac(master, b"mac-" + direction.encode(), cn, sn)


def _keystream(key: bytes, seq: int, length: int) -> bytes:
    return hashlib.shake_256(key + seq.to_bytes(8, "big")).digest(length)


def _xor(data: bytes, stream: bytes) -> bytes:
    if not data:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ProtocolError("connexion fermée par l'autre PC")
        buf += chunk
    return bytes(buf)


class SecureChannel:
    """Messages chiffrés + authentifiés au-dessus d'une socket."""

    def __init__(self, sock: socket.socket, send_keys: tuple, recv_keys: tuple):
        self.sock = sock
        self._senc, self._smac = send_keys
        self._renc, self._rmac = recv_keys
        self._sseq = 0
        self._rseq = 0

    def send(self, payload: bytes) -> None:
        n, self._sseq = self._sseq, self._sseq + 1
        cipher = _xor(payload, _keystream(self._senc, n, len(payload)))
        head = struct.pack(">I", len(cipher))
        tag = _mac(self._smac, struct.pack(">Q", n), head, cipher)
        self.sock.sendall(head + cipher + tag)

    def recv(self) -> bytes:
        head = _recv_exact(self.sock, 4)
        (length,) = struct.unpack(">I", head)
        if length > MAX_FRAME:
            raise ProtocolError("message trop grand")
        body = _recv_exact(self.sock, length + 32)
        cipher, tag = body[:length], body[length:]
        n, self._rseq = self._rseq, self._rseq + 1
        if not hmac.compare_digest(tag, _mac(self._rmac, struct.pack(">Q", n), head, cipher)):
            raise ProtocolError("message altéré ou code incorrect")
        return _xor(cipher, _keystream(self._renc, n, length))

    def send_json(self, obj: dict) -> None:
        self.send(b"J" + json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def recv_message(self) -> tuple:
        data = self.recv()
        return data[:1], data[1:]

    def recv_json(self) -> dict:
        kind, payload = self.recv_message()
        if kind != b"J":
            raise ProtocolError("message inattendu")
        return json.loads(payload.decode("utf-8"))


def client_handshake(sock: socket.socket, code: str) -> SecureChannel:
    master = master_key(code)
    cn = os.urandom(16)
    sock.sendall(MAGIC + cn)
    reply = _recv_exact(sock, 16 + 32)
    sn, proof = reply[:16], reply[16:]
    if not hmac.compare_digest(proof, _mac(master, b"S", cn, sn)):
        raise ProtocolError("code incorrect (ou ce n'est pas le bon PC)")
    sock.sendall(_mac(master, b"C", cn, sn))
    return SecureChannel(sock, _session_keys(master, cn, sn, "c2s"), _session_keys(master, cn, sn, "s2c"))


def server_handshake(sock: socket.socket, code: str) -> SecureChannel:
    master = master_key(code)
    head = _recv_exact(sock, len(MAGIC) + 16)
    if head[: len(MAGIC)] != MAGIC:
        raise ProtocolError("ce n'est pas une connexion swap")
    cn, sn = head[len(MAGIC):], os.urandom(16)
    sock.sendall(sn + _mac(master, b"S", cn, sn))
    if not hmac.compare_digest(_recv_exact(sock, 32), _mac(master, b"C", cn, sn)):
        time.sleep(1)  # freine les essais de codes
        raise ProtocolError("code incorrect")
    return SecureChannel(sock, _session_keys(master, cn, sn, "s2c"), _session_keys(master, cn, sn, "c2s"))


# --- réception (PC cible) ---------------------------------------------------------------------------------------
def safe_join(root: str, rel: str) -> str:
    """Chemin sous `root` correspondant à `rel` ; refuse tout ce qui pourrait en sortir."""
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") or re.search(r'[\\:*?"<>|\x00]', p) for p in parts):
        raise ValueError(f"chemin refusé : {rel!r}")
    path = os.path.join(root, *parts)
    if not is_under(path, root):
        raise ValueError(f"chemin refusé : {rel!r}")
    return path


def local_addresses() -> list:
    try:
        return sorted({ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")})
    except OSError:
        return []


class Receiver:
    """Serveur du PC cible : écrit ce qu'envoie le PC source sous <dossier>/SWAP-<nom du PC source>."""

    def __init__(self, dest_dir: str, code: Optional[str] = None, port: int = DEFAULT_PORT,
                 on_event: Optional[Callable[[dict], None]] = None, bind: str = "0.0.0.0"):
        self.dest_dir = dest_dir
        self.code = code or make_code()
        self.on_event = on_event or (lambda _e: None)
        self.stop_event = threading.Event()
        self.root: Optional[str] = None
        self.files = 0
        self.bytes = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind, port))
        self.sock.listen(2)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]

    def emit(self, kind: str, **info) -> None:
        self.on_event(dict(info, kind=kind))

    def stop(self) -> None:
        self.stop_event.set()

    def serve(self, once: bool = True) -> None:
        """Attend un PC source. `once` : s'arrête après un envoi terminé correctement."""
        self.emit("listening", port=self.port, code=self.code)
        try:
            while not self.stop_event.is_set():
                try:
                    conn, addr = self.sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    if self._handle(conn, addr) and once:
                        break
        finally:
            self.sock.close()
            self.emit("stopped")

    def _handle(self, conn: socket.socket, addr) -> bool:
        conn.settimeout(20)
        try:
            ch = server_handshake(conn, self.code)
        except (ProtocolError, OSError) as exc:
            self.emit("refused", peer=addr[0], error=str(exc))
            return False
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(120)
        try:
            return self._session(ch, addr)
        except (ProtocolError, OSError, ValueError, KeyError) as exc:
            self.emit("aborted", peer=addr[0], error=str(exc))
            return False

    def _session(self, ch: SecureChannel, addr) -> bool:
        os.makedirs(self.dest_dir, exist_ok=True)
        while not self.stop_event.is_set():
            msg = ch.recv_json()
            op = msg.get("op")
            if op == "hello":
                ch.send_json({"ok": True, "server_machine": socket.gethostname(), "free": shutil.disk_usage(self.dest_dir).free})
                if msg.get("probe"):
                    self.emit("probe", peer=addr[0])
                    return False
                safe = re.sub(r"[^\w.\-]", "_", msg.get("machine") or "poste")
                self.root = os.path.join(self.dest_dir, f"SWAP-{safe}")
                os.makedirs(self.root, exist_ok=True)
                self.emit("connected", peer=addr[0], machine=msg.get("machine", ""), user=msg.get("user", ""), root=self.root)
            elif self.root is None:
                raise ProtocolError("hello attendu")
            elif op == "dir":
                os.makedirs(long_path(safe_join(self.root, msg["rel"])), exist_ok=True)
                ch.send_json({"ok": True})
            elif op == "file":
                self._receive_file(ch, msg)
            elif op == "bytes":
                self._receive_file(ch, dict(msg, mtime=time.time(), hash=False))
            elif op == "bye":
                ch.send_json({"ok": True})
                self.emit("done", root=self.root, files=self.files, bytes=self.bytes)
                return True
            else:
                raise ProtocolError(f"opération inconnue : {op!r}")
        return False

    def _receive_file(self, ch: SecureChannel, msg: dict) -> None:
        dest = safe_join(self.root, msg["rel"])
        size, mtime, want_hash = int(msg["size"]), float(msg["mtime"]), bool(msg.get("hash"))
        if msg["op"] == "file" and is_current(dest, size, mtime):
            ch.send_json({"ok": True, "send": False, "hash": file_hash(dest) if want_hash else None})
            return
        tmp = long_path(dest + PART)
        try:
            os.makedirs(os.path.dirname(long_path(dest)), exist_ok=True)
            fout = open(tmp, "wb")
        except OSError as exc:
            ch.send_json({"ok": False, "error": f"{dest} : {exc}"})
            return
        ch.send_json({"ok": True, "send": True})
        digest, got = hashlib.sha256(), 0
        try:
            with fout:
                while got < size:
                    kind, payload = ch.recv_message()
                    if kind != b"D" or got + len(payload) > size:
                        raise ProtocolError("données inattendues")
                    fout.write(payload)
                    digest.update(payload)
                    got += len(payload)
                    self.bytes += len(payload)
            os.utime(tmp, (mtime, mtime))
            os.replace(tmp, long_path(dest))
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self.files += 1
        self.emit("file", rel=msg["rel"], size=size, files=self.files, bytes=self.bytes)
        ch.send_json({"ok": True, "hash": digest.hexdigest()})


# --- envoi (PC source) ------------------------------------------------------------------------------------------
class NetSink:
    """Destination distante : même interface que LocalSink, mais les fichiers partent vers un PC en mode réception."""

    def __init__(self, host: str, code: str, port: int = DEFAULT_PORT, machine: str = "", user: str = "",
                 timeout: float = 30, probe: bool = False):
        self.host, self.code, self.port = host, code, port
        self.machine, self.user, self.timeout = machine, user, timeout
        self.ch: Optional[SecureChannel] = None
        self.server_machine = ""
        self.free = -1
        self.root = ""
        self._connect(probe)

    def describe(self) -> str:
        return f"{self.server_machine or self.host} (connexion directe)"

    def _connect(self, probe: bool = False) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.gaierror:
            raise SinkError(f"Le PC « {self.host} » est introuvable sur le réseau (nom incorrect ?).") from None
        except OSError as exc:
            raise SinkError(f"Impossible de joindre {self.host}:{self.port} ({exc}). Le PC cible est-il en mode réception, "
                            "et le pare-feu autorise-t-il ce port ?") from None
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.ch = client_handshake(sock, self.code)
            self.ch.send_json({"op": "hello", "machine": self.machine, "user": self.user, "probe": probe})
            reply = self.ch.recv_json()
        except (ProtocolError, OSError) as exc:
            sock.close()
            raise SinkError(str(exc)) from None
        self.server_machine, self.free = reply.get("server_machine", ""), reply.get("free", -1)

    def _reconnect(self) -> None:
        self.close(polite=False)
        self._connect()

    def _lost(self, exc: Exception) -> SinkError:
        """Erreur fatale de connexion : on libère la socket avant de la signaler."""
        self.close(polite=False)
        return SinkError(f"connexion perdue : {exc}")

    def _call(self, obj: dict) -> dict:
        if self.ch is None:
            raise SinkError("connexion fermée")
        try:
            self.ch.send_json(obj)
            return self.ch.recv_json()
        except (ProtocolError, OSError) as exc:
            raise self._lost(exc) from None

    def _send(self, data: bytes) -> None:
        if self.ch is None:
            raise SinkError("connexion fermée")
        try:
            self.ch.send(data)
        except OSError as exc:
            raise self._lost(exc) from None

    def makedirs(self, rel: str) -> None:
        self._raise_if_error(self._call({"op": "dir", "rel": rel}))

    @staticmethod
    def _raise_if_error(reply: dict) -> None:
        if not reply.get("ok"):
            raise OSError(reply.get("error", "refusé par le PC cible"))

    def put_file(self, rel: str, src: str, size: int, mtime: float, want_hash: bool = False,
                 on_chunk: Optional[Callable[[int], None]] = None) -> tuple:
        fin = open(long_path(src), "rb")  # une erreur d'ouverture (fichier verrouillé) reste une simple erreur de fichier
        with fin:
            reply = self._call({"op": "file", "rel": rel, "size": size, "mtime": mtime, "hash": want_hash})
            self._raise_if_error(reply)
            if not reply.get("send"):
                if on_chunk:
                    on_chunk(size)
                return "unchanged", reply.get("hash")
            digest, sent = hashlib.sha256(), 0
            try:
                while sent < size:
                    try:
                        chunk = fin.read(min(CHUNK, size - sent))
                    except OSError:
                        self._reconnect()
                        raise
                    if not chunk:
                        self._reconnect()
                        raise OSError(f"{src} : fichier modifié pendant la copie")
                    self._send(b"D" + chunk)
                    digest.update(chunk)
                    sent += len(chunk)
                    if on_chunk:
                        on_chunk(len(chunk))
            except Cancelled:
                self.close(polite=False)
                raise
            try:
                reply = self.ch.recv_json()
            except (ProtocolError, OSError) as exc:
                raise self._lost(exc) from None
        self._raise_if_error(reply)
        if want_hash and reply.get("hash") != digest.hexdigest():
            raise OSError(f"{rel} : empreinte différente après l'envoi")
        return "copied", digest.hexdigest() if want_hash else None

    def write_bytes(self, rel: str, data: bytes) -> None:
        self._raise_if_error(self._call({"op": "bytes", "rel": rel, "size": len(data)}))
        for i in range(0, len(data), CHUNK):
            self._send(b"D" + data[i:i + CHUNK])
        try:
            reply = self.ch.recv_json()
        except (ProtocolError, OSError) as exc:
            raise self._lost(exc) from None
        self._raise_if_error(reply)

    def put_tree(self, local_dir: str, prefix: str = "") -> int:
        count = 0
        for folder, _dirs, files in os.walk(local_dir):
            for name in files:
                full = os.path.join(folder, name)
                st = os.stat(full)
                self.put_file(prefix + os.path.relpath(full, local_dir).replace(os.sep, "/"), full, st.st_size, st.st_mtime)
                count += 1
        return count

    def close(self, polite: bool = True) -> None:
        ch, self.ch = self.ch, None
        if ch is None:
            return
        try:
            if polite:
                ch.send_json({"op": "bye"})
                ch.recv_json()
        except (ProtocolError, OSError):
            pass
        finally:
            try:
                ch.sock.close()
            except OSError:
                pass


def probe(host: str, code: str, port: int = DEFAULT_PORT, timeout: float = 10) -> dict:
    """Teste la connexion et le code sans rien envoyer. Renvoie {"machine", "free"} ou lève SinkError."""
    sink = NetSink(host, code, port, timeout=timeout, probe=True)
    result = {"machine": sink.server_machine, "free": sink.free}
    sink.close(polite=False)
    return result


# --- partage Windows (\\PC\C$) et pare-feu ----------------------------------------------------------------------
def unc_path(host: str, share: str = "C$", folder: str = "SWAP") -> str:
    return "\\\\" + host.strip().strip("\\") + "\\" + share.strip("\\") + (("\\" + folder.strip("\\")) if folder else "")


def check_folder(path: str, timeout: float = 15) -> bool:
    """Le dossier (ou partage) est-il accessible ? Limité dans le temps car un nom réseau injoignable peut bloquer longtemps."""
    result = []
    t = threading.Thread(target=lambda: result.append(os.path.isdir(path)), daemon=True)
    t.start()
    t.join(timeout)
    return bool(result and result[0])


def firewall_open(port: int) -> tuple:
    """Autorise le port en entrée (profils Domaine/Privé). Demande les droits administrateur."""
    if not is_windows():
        return False, "Pare-feu Windows uniquement."
    proc = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name=swap-{port}", "dir=in", "action=allow",
                           "protocol=TCP", f"localport={port}", "profile=domain,private"],
                          capture_output=True, stdin=subprocess.DEVNULL)
    return proc.returncode == 0, decode_output(proc.stdout + proc.stderr).strip()


def firewall_close(port: int) -> tuple:
    if not is_windows():
        return False, ""
    proc = subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name=swap-{port}"],
                          capture_output=True, stdin=subprocess.DEVNULL)
    return proc.returncode == 0, decode_output(proc.stdout + proc.stderr).strip()
