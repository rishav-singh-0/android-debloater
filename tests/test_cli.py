from types import SimpleNamespace

import android_debloater


def test_ctrl_c_closes_socket_without_traceback(monkeypatch, tmp_path):
    closed = []
    sock = SimpleNamespace(bind=lambda address: None, close=lambda: closed.append(True))

    def interrupted(self, sockets):
        raise KeyboardInterrupt

    monkeypatch.setattr(android_debloater.socket, "socket", lambda: sock)
    monkeypatch.setattr(android_debloater.ReadyServer, "run", interrupted)
    monkeypatch.setattr(
        android_debloater.sys, "argv", ["android-debloater", "--no-browser", "--data-dir", str(tmp_path)]
    )
    android_debloater.main()
    assert closed == [True]
