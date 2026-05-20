"""
single_instance.py — drop-in Windows single-instance guard for Python tray apps.

Copy this file into any new project and call it from your entry point.

WHY THIS EXISTS
---------------
The Python venv launcher (venv/Scripts/pythonw.exe) spawns the real interpreter
as a child inside a Windows Job Object, then waits.  Killing the launcher
collapses the Job Object and kills the real Python too — self-inflicted death.
The fix: exclude the entire ancestor chain from the kill list so we never touch
our own venv launcher.

USAGE
-----
    # main.py
    if __name__ == '__main__':
        from single_instance import SingleInstance
        si = SingleInstance(port=47294, mutex_name='MyApp_StartupLock_v1')
        si.ensure()                         # elect winner, kill old instance
        app = App()
        # in App.__init__:
        #   threading.Thread(target=si.watch, args=(app._quit,), daemon=True).start()
        # in App._quit (BEFORE sys.exit):
        #   si.cleanup()
        app.run()
"""

import os
import sys
import time
import socket
import ctypes
import threading
from typing import Callable


class SingleInstance:
    """Guarantees exactly one running copy of a tray app on Windows."""

    def __init__(
        self,
        port: int = 47294,
        mutex_name: str = 'PythonApp_StartupLock_v1',
        app_name_in_cmdline: str | None = None,
    ) -> None:
        """
        port             — localhost TCP port used for graceful-quit signalling.
        mutex_name       — Windows named mutex (must be unique per app).
        app_name_in_cmdline — substring that identifies THIS app in process
                              command lines (e.g. 'main.py', 'myapp').
                              Defaults to the basename of sys.argv[0].
        """
        self._port       = port
        self._mutex_name = mutex_name
        self._marker     = app_name_in_cmdline or os.path.basename(
            sys.argv[0] if sys.argv else 'main.py'
        )
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def ensure(self) -> None:
        """Call once at startup (before creating the app).

        The WINNER (first concurrent launch) does:
          1. Graceful quit via socket → pystray removes the tray icon cleanly
          2. Hard-kill any survivor with its full process tree
          3. Sleep 1.5 s so Windows reaps ghost tray entries
          4. Bind socket as the graceful-quit channel for the next launch

        Every LOSER (rapid re-launch while winner is starting) sleeps 4 s,
        then exits if a fresh instance is already running.
        """
        kernel32   = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, True, self._mutex_name)
        err   = kernel32.GetLastError()

        if err == 183:          # ERROR_ALREADY_EXISTS — another launch is starting
            kernel32.CloseHandle(mutex)
            time.sleep(4.0)    # wait for the winner to finish
            if self._find_other_pids():
                sys.exit(0)    # fresh instance is running — not needed
            # Winner crashed mid-startup; become the new winner
            self.ensure()
            return

        # ── We are THE winner ─────────────────────────────────────────────
        # 1. Graceful quit
        try:
            c = socket.create_connection(('127.0.0.1', self._port), timeout=1)
            c.sendall(b'QUIT')
            c.close()
            time.sleep(2.5)    # give pystray time to call NIM_DELETE
        except Exception:
            pass               # nothing listening — first ever launch

        # 2. Hard-kill survivors (entire process tree)
        try:
            import psutil
            for pid in self._find_other_pids():
                try:
                    proc = psutil.Process(pid)
                    for child in proc.children(recursive=True):
                        try: child.kill()
                        except Exception: pass
                    proc.kill()
                except Exception:
                    pass
        except ImportError:
            pass

        # 3. Let Windows clear ghost tray icons
        time.sleep(1.5)

        # 4. Bind socket as graceful-quit channel for the next launch
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', self._port))
            s.listen(5)
            self._sock = s
        except Exception:
            pass               # not critical — psutil hard-kill works without it

        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)

    def watch(self, on_quit: Callable[[], None]) -> None:
        """Run in a daemon thread.  Calls on_quit() when a new launch signals QUIT.

        Ignores stale QUIT messages from already-dead launchers.
        """
        if not self._sock:
            return
        while True:
            try:
                conn, _ = self._sock.accept()
                try:
                    conn.recv(16)
                finally:
                    conn.close()
                # Only quit if a real new instance is actually still running
                if self._find_other_pids():
                    on_quit()
                    return
                # else: stale QUIT from a dead rapid-launcher — ignore
            except Exception:
                return         # socket closed during normal cleanup

    def cleanup(self) -> None:
        """Call from your app's quit/shutdown method BEFORE sys.exit()."""
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _find_other_pids(self) -> list[int]:
        """Return PIDs of other top-level instances of this app.

        Excludes our entire lineage (descendants AND ancestors) so we never
        accidentally kill the venv launcher (our parent) which would collapse
        its Windows Job Object and terminate us too.
        """
        try:
            import psutil
        except ImportError:
            return []

        my_pid = os.getpid()
        safe: set[int] = {my_pid}

        # Exclude descendants
        try:
            for c in psutil.Process(my_pid).children(recursive=True):
                safe.add(c.pid)
        except Exception:
            pass

        # Exclude ancestors (parent chain) — critical for venv launcher safety
        try:
            p = psutil.Process(my_pid)
            while True:
                p = p.parent()
                if p is None:
                    break
                safe.add(p.pid)
        except Exception:
            pass

        marker = self._marker.lower()

        # First pass — collect candidates
        candidates: dict[int, int] = {}   # pid → ppid
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
            try:
                if proc.pid in safe:
                    continue
                name = (proc.info['name'] or '').lower()
                if name not in ('pythonw.exe', 'python.exe', 'hotkeys.exe'):
                    continue
                cmdline = ' '.join(proc.info['cmdline'] or []).lower()
                if marker in cmdline:
                    candidates[proc.pid] = proc.info.get('ppid') or 0
            except Exception:
                pass

        # Second pass — keep only roots (parent not itself a candidate)
        return [pid for pid, ppid in candidates.items() if ppid not in candidates]
