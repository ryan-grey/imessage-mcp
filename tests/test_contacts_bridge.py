"""Transport lifecycle checks using synthetic echo workers, never Contacts."""
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import threading

spec = importlib.util.spec_from_file_location(
    'bridge', Path(__file__).resolve().parents[1] / 'helper/bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_isolated_sessions_and_disconnect_cleanup(monkeypatch):
    original = subprocess.Popen
    def echo(*args, **kwargs):
        return original([sys.executable, '-u', '-c',
                         'import sys\nfor line in sys.stdin: print(line.strip(), flush=True)'],
                        **kwargs)
    monkeypatch.setattr(bridge.subprocess, 'Popen', echo)
    clients, threads = [], []
    for _ in range(2):
        client, server = socket.socketpair()
        client.settimeout(5)
        worker = threading.Thread(target=bridge.session, args=(server,))
        worker.start()
        clients.append(client)
        threads.append(worker)
    try:
        for n, client in enumerate(clients):
            payload = f'session-{n}\n'.encode()
            client.sendall(payload)
            assert client.recv(1024) == payload
    finally:
        for client in clients:
            client.close()
        for worker in threads:
            worker.join(5)
            assert not worker.is_alive()
    assert not bridge.CHILDREN


def test_denied_store_does_not_prompt():
    import contacts
    class Store:
        @staticmethod
        def authorizationStatusForEntityType_(_): return 0
        @classmethod
        def alloc(cls): return cls()
        def init(self): return self
        def requestAccessForEntityType_completionHandler_(self, *_):
            raise AssertionError('Implicit permission prompt')
    class Framework:
        CNContactStore = Store
        CNEntityTypeContacts = 0
    adapter = contacts._RealCN()
    adapter._fw = lambda: Framework
    import pytest
    with pytest.raises(RuntimeError, match='Open Ryan Contacts'):
        adapter._store()
