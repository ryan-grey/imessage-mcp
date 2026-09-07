"""Owner-only Unix socket transports existing MCP stdio; no network listener.

Run the service only as a child of Ryan Contacts, launched by LaunchServices.
Each connection gets an isolated MCP process with the original write guards.
"""
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path.home() / '.local/run/ryan-contacts'
SOCKET = ROOT / 'mcp.sock'
APP = Path.home() / 'Applications/Ryan Contacts.app'
SERVER = Path(__file__).resolve().parents[1] / 'contacts.py'
CHILDREN = set()
CHILD_LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(8)


def session(connection):
    with connection:
        child = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with CHILD_LOCK:
            CHILDREN.add(child)
        def incoming():
            try:
                while data := connection.recv(65536):
                    child.stdin.write(data)
                    child.stdin.flush()
            except (OSError, BrokenPipeError):
                pass
            finally:
                child.terminate()
        thread = threading.Thread(target=incoming, daemon=True)
        thread.start()
        try:
            while data := os.read(child.stdout.fileno(), 65536):
                connection.sendall(data)
        except OSError:
            pass
        finally:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            with CHILD_LOCK:
                CHILDREN.discard(child)
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve():
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ROOT.is_symlink() or ROOT.stat().st_uid != os.getuid():
        raise RuntimeError('Unsafe socket directory')
    ROOT.chmod(0o700)
    lock = (ROOT / 'service.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    SOCKET.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(SOCKET))
    listener.listen(8)
    def stop(*_):
        listener.close()
        SOCKET.unlink(missing_ok=True)
        with CHILD_LOCK:
            for child in CHILDREN:
                child.terminate()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    while True:
        connection, _ = listener.accept()
        if not SLOTS.acquire(blocking=False):
            connection.close()
            continue
        def run(client):
            try:
                session(client)
            finally:
                SLOTS.release()
        threading.Thread(target=run, args=(connection,), daemon=True).start()


def connect():
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.connect(str(SOCKET))
    except (FileNotFoundError, ConnectionRefusedError):
        subprocess.run(['/usr/bin/open', '-g', str(APP)], check=True,
                       stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while True:
            try:
                connection.connect(str(SOCKET))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Open Ryan Contacts to start the helper')
                time.sleep(0.1)
    def incoming():
        try:
            while data := os.read(sys.stdin.fileno(), 65536):
                connection.sendall(data)
        finally:
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    threading.Thread(target=incoming, daemon=True).start()
    with connection:
        while data := connection.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


if __name__ == '__main__':
    serve() if sys.argv[1:] == ['serve'] else connect()
