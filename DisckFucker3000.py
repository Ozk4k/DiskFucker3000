#!/usr/bin/env python3
#DiskFucker3000 ;p
"""
DiskFucker3000 - prototype

Safely ejects a USB external drive (UASP included):
  1) looks for processes that still use the drive
  2) checks for ongoing read/write activity
  3) flushes caches (sync)
  4) unmounts every partition
  5) powers the drive off

Layout: a small OS-agnostic Backend base class holds the eject workflow,
LinuxBackend / WindowsBackend implement the OS-specific bits, and a PySide6 UI
runs everything in a worker thread so the window never freezes.

Dependencies:  pip install PySide6 psutil
Linux:   needs udisks2 (udisksctl) and lsblk. Does not need root.
Windows: must run as administrator (the app asks for UAC by itself).
"""
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from ctypes import wintypes  # only used on Windows, but safe to import everywhere

import psutil
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)


# ───────────────────────── model ─────────────────────────
# Any expected failure (busy drive, missing tool, OS refusal) is raised as
# EjectError; its message is shown to the user as-is, so keep it readable.
class EjectError(Exception):
    pass


@dataclass
class Part:
    device: str   # Linux: /dev/sdb1  | Windows: E:
    mount: str    # Linux: /media/x   | Windows: E:\
    label: str = ""


@dataclass
class Drive:
    id: str       # Linux: /dev/sdb   | Windows: disk number (e.g. "1")
    name: str
    size: int     # bytes
    parts: list = field(default_factory=list)
    # backend-private data; on Windows it holds the device interface path,
    # which power_off() needs later to find the device node
    extra: dict = field(default_factory=dict)

    def title(self):
        gb = self.size / 1e9
        mounts = ", ".join(p.mount for p in self.parts) or "no mounted partition"
        return f"{self.name}  ({gb:.0f} GB)  [{self.id}]  →  {mounts}"


# ───────────────────────── base backend ─────────────────────────
# Subclasses implement the OS-specific steps; eject() below is the shared
# workflow and decides the order. Every step either succeeds or raises
# EjectError, and a failure stops the whole thing (never power off after a
# failed unmount).
class Backend:
    def list_drives(self):
        raise NotImplementedError

    # Key of this disk in psutil.disk_io_counters(perdisk=True)
    def io_key(self, d):
        raise NotImplementedError

    def flush(self, d):
        pass  # Windows flushes while locking the volume, so it needs nothing here

    def unmount(self, d, log):
        raise NotImplementedError

    def power_off(self, d, log):
        raise NotImplementedError

    # --- who is using the drive? (best effort; the unmount is the final barrier) ---
    # A process counts as "using" the drive if it has a file open there or if its
    # current directory is inside it (a shell sitting in the mount blocks unmounting).
    # Processes we are not allowed to inspect (other users, root) raise an error
    # and are skipped, so they can be missed here. If one of them really holds a
    # file, the unmount step fails with "busy" instead of corrupting anything.
    def busy_processes(self, d):
        roots = [os.path.normcase(p.mount).rstrip("\\/") for p in d.parts]
        found = []
        if not roots:
            return found
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                paths = [f.path for f in proc.open_files()]
                try:
                    paths.append(proc.cwd())
                except (psutil.Error, OSError):
                    pass
            except (psutil.Error, OSError):
                continue
            for path in paths:
                n = os.path.normcase(path)
                # compare with a trailing separator so /media/HD2 doesn't match /media/HD
                if any(n == r or n.startswith(r + os.sep) for r in roots):
                    found.append((proc.info["pid"], proc.info["name"], path))
                    break  # one hit is enough to report this process
        return found

    # --- is there I/O happening on the disk right now? ---
    # Catches activity from processes busy_processes() cannot see. We sample the
    # kernel's per-disk byte counters 1s apart. The 256 KiB threshold ignores
    # background noise (filesystem journal, access-time updates) while still
    # catching a real copy.
    def io_active(self, d):
        key = self.io_key(d)
        a = psutil.disk_io_counters(perdisk=True).get(key)
        time.sleep(1.0)
        b = psutil.disk_io_counters(perdisk=True).get(key)
        if not a or not b:
            return False  # counters unavailable: don't block the eject over it
        moved = (b.write_bytes - a.write_bytes) + (b.read_bytes - a.read_bytes)
        return moved > 256 * 1024

    # --- full workflow ---
    # Order matters: check first (cheap, harmless), then flush, unmount, and only
    # then power off.
    def eject(self, d, log):
        log("🔎 Looking for programs using the drive...")
        busy = self.busy_processes(d)
        if busy:
            for pid, name, path in busy[:15]:  # cap the log so a huge list doesn't flood it
                log(f"   • {name} (PID {pid}) → {path}")
            raise EjectError("A program is still using the drive. Close it and try again.")

        log("📊 Checking read/write activity (1s)...")
        if self.io_active(d):
            raise EjectError("The drive still has I/O activity (a copy in progress?). Wait for it to finish.")

        log("💾 Flushing cache...")
        self.flush(d)

        self.unmount(d, log)

        log("🔌 Powering off the drive...")
        self.power_off(d, log)
        return True


# ───────────────────────── Linux ─────────────────────────
class LinuxBackend(Backend):
    # psutil names disks by kernel name only: "sdb", not "/dev/sdb"
    def io_key(self, d):
        return os.path.basename(d.id)

    # lsblk gives us the whole device tree as JSON (-J) with sizes in bytes (-b).
    # UASP drives need no special handling: the kernel exposes them as regular
    # SCSI disks and lsblk reports TRAN=usb for them just like for plain
    # bulk-only drives.
    def list_drives(self):
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,TRAN,MODEL,LABEL,MOUNTPOINT"],
            capture_output=True, text=True, check=True,
        ).stdout
        drives = []
        for dev in json.loads(out)["blockdevices"]:
            if dev.get("type") != "disk" or dev.get("tran") != "usb":
                continue
            parts = []

            # Recursive on purpose: mounts can be nested (LVM, LUKS, ...), not
            # only direct children of the disk.
            def walk(node):
                mp = node.get("mountpoint")
                if mp and node is not dev:
                    parts.append(Part(node["path"], mp, node.get("label") or ""))
                for c in node.get("children") or []:
                    walk(c)

            walk(dev)
            if dev.get("mountpoint"):  # filesystem directly on the disk, no partition table
                parts.append(Part(dev["path"], dev["mountpoint"], dev.get("label") or ""))
            # safety guard: never touch a disk the system is running from
            # (e.g. booted from a USB stick)
            if any(p.mount == "/" or p.mount.startswith("/boot") for p in parts):
                continue
            drives.append(Drive(dev["path"], (dev.get("model") or "USB Drive").strip(),
                                int(dev.get("size") or 0), parts))
        return drives

    # sync(2) flushes dirty pages for the whole system, not just this drive.
    # That's fine: it's cheap and strictly safer.
    def flush(self, d):
        os.sync()

    # udisksctl talks to the udisks2 daemon over D-Bus, and polkit allows
    # active-session users to unmount/power off removable drives without root.
    def _udisks(self, *args):
        exe = shutil.which("udisksctl")
        if not exe:
            raise EjectError("udisksctl not found. Install the 'udisks2' package.")
        return subprocess.run([exe, *args], capture_output=True, text=True)

    # Unmount every partition first. If anything still holds a file open, udisks
    # fails with "target is busy" - the real safety net behind busy_processes().
    def unmount(self, d, log):
        for p in d.parts:
            log(f"⏏️  Unmounting {p.device} ({p.mount})")
            r = self._udisks("unmount", "-b", p.device)
            if r.returncode:
                raise EjectError(f"Failed to unmount {p.device}: {(r.stderr or r.stdout).strip()}")

    # power-off syncs the drive's own write cache, spins it down and detaches it.
    # Whether the USB port itself loses power depends on the hub/port hardware.
    def power_off(self, d, log):
        r = self._udisks("power-off", "-b", d.id)
        if r.returncode:
            raise EjectError(f"Failed to power off {d.id}: {(r.stderr or r.stdout).strip()}")
        log("   (the drive spins down; if the USB port stays powered, "
            "the hub has no per-port power control)")


# ───────────────────────── Windows ─────────────────────────
# Windows API constants (winioctl.h / cfgmgr32.h / fileapi.h)
FSCTL_LOCK_VOLUME = 0x00090018       # succeeds only if nobody else has the volume open
FSCTL_DISMOUNT_VOLUME = 0x00090020   # detaches the filesystem from the volume
GENERIC_RW = 0x80000000 | 0x40000000 # GENERIC_READ | GENERIC_WRITE
SHARE_RW = 0x1 | 0x2                 # FILE_SHARE_READ | FILE_SHARE_WRITE
OPEN_EXISTING = 3
CM_DRP_CAPABILITIES = 0x0F           # device node property: capability flags
CM_DEVCAP_REMOVABLE = 0x04           # flag: device can be removed ("eject-able")

# Listing goes through PowerShell because the Storage cmdlets expose BusType.
# Win32_DiskDrive.InterfaceType is not reliable here: UASP drives often show up
# as "SCSI" there instead of "USB".
# Notes on the script:
#  - system/boot disks are filtered out (safety guard)
#  - ConvertTo-Json -InputObject @(...) forces a JSON array even for 0 or 1 disks
#  - UTF-8 output keeps accented drive names intact
#  - 'path' is the device interface path, used later to find the device node
#  - SilentlyContinue avoids error noise from disks without partitions
PS_LIST = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$out = @(Get-Disk | Where-Object { $_.BusType -eq 'USB' -and -not $_.IsSystem -and -not $_.IsBoot } | ForEach-Object {
  $n = $_.Number
  $vols = @(Get-Partition -DiskNumber $n | Where-Object DriveLetter | ForEach-Object {
    $l = [string]$_.DriveLetter
    [pscustomobject]@{ letter = $l; label = (Get-Volume -DriveLetter $l).FileSystemLabel }
  })
  [pscustomobject]@{ number = $n; name = $_.FriendlyName; size = $_.Size; path = $_.Path; vols = $vols }
})
ConvertTo-Json -InputObject $out -Depth 4 -Compress
"""


class WindowsBackend(Backend):
    def __init__(self):
        k = self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declaring argtypes/restype is not optional: without them ctypes treats
        # handles as 32-bit ints and would truncate them on 64-bit Windows.
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                      wintypes.LPVOID]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        k.FlushFileBuffers.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.cfg = ctypes.WinDLL("cfgmgr32")  # Configuration Manager: device tree + eject

    # psutil names physical disks "PhysicalDrive<N>" on Windows
    def io_key(self, d):
        return f"PhysicalDrive{d.id}"

    def list_drives(self):
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_LIST],
            capture_output=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no flashing console window
        )
        txt = (r.stdout or "").strip()
        data = json.loads(txt) if txt else []
        drives = []
        for x in data:
            parts = [Part(f"{v['letter']}:", f"{v['letter']}:\\", v.get("label") or "")
                     for v in (x.get("vols") or [])]
            drives.append(Drive(str(x["number"]), x.get("name") or "USB Drive",
                                int(x.get("size") or 0), parts, {"path": x.get("path") or ""}))
        return drives

    # Lock + flush + dismount one volume.
    # Opening "\\.\E:" gives us the raw volume (this is why admin is required).
    # FSCTL_LOCK_VOLUME only succeeds when no other handle is open on the volume,
    # which is exactly the test Explorer's eject uses, so a failed lock means
    # some program is still using the drive.
    def _lock_dismount(self, letter):
        k = self.k32
        invalid = ctypes.c_void_p(-1).value  # INVALID_HANDLE_VALUE
        h = k.CreateFileW(f"\\\\.\\{letter}:", GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h in (None, invalid):
            err = ctypes.get_last_error()
            raise EjectError(f"Could not open {letter}: (error {err}). "
                             + ("Run as administrator." if err == 5 else ""))  # 5 = ACCESS_DENIED
        ret = wintypes.DWORD()
        try:
            # Retry a few times: short-lived handles (indexer, antivirus scan)
            # often release the volume within a moment.
            for _ in range(4):
                if k.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None):
                    break
                time.sleep(0.5)
            else:
                raise EjectError(f"{letter}: is in use by some program (could not lock the volume).")
            k.FlushFileBuffers(h)  # write everything still sitting in the cache
            k.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
        finally:
            k.CloseHandle(h)  # always release the handle, even on failure

    def unmount(self, d, log):
        for p in d.parts:
            log(f"⏏️  Locking and unmounting {p.device}")
            self._lock_dismount(p.device[0])  # drive letter only, "E:" -> "E"

    # Turn a device interface path into a device instance ID:
    #   \\?\scsi#disk&ven_x#1&2c1a&0&0#{53f56307-...}  ->  SCSI\DISK&VEN_X\1&2C1A&0&0
    # i.e. drop the "\\?\" prefix and the trailing interface-class GUID, then
    # swap "#" for "\". IDs are case-insensitive, so upper-casing is harmless.
    @staticmethod
    def _instance_id(iface_path):
        p = iface_path
        if p.startswith("\\\\?\\"):
            p = p[4:]
        p = re.sub(r"#\{[0-9a-fA-F-]+\}$", "", p)
        return p.replace("#", "\\").upper()

    # Ejects through the Configuration Manager, the same mechanism as the
    # "Safely Remove Hardware" tray icon. Windows runs its own checks and, if
    # something blocks the removal, reports who vetoed it.
    def power_off(self, d, log):
        cfg = self.cfg
        inst = self._instance_id(d.extra.get("path", ""))
        if not inst:
            raise EjectError("Could not find this drive's device ID.")
        node = wintypes.DWORD()
        if cfg.CM_Locate_DevNodeW(ctypes.byref(node), inst, 0) != 0:
            raise EjectError(f"Windows could not locate the device ({inst}).")
        cur = node.value
        # Walk up the device tree until we hit a node flagged as removable.
        # The disk node itself often isn't (on UASP it is SCSI\DISK...); the one
        # Windows actually ejects is the USB device above it. Capped at 6 levels
        # as a guard against a malformed tree.
        for _ in range(6):
            caps, size = wintypes.DWORD(), wintypes.ULONG(4)
            cfg.CM_Get_DevNode_Registry_PropertyW(cur, CM_DRP_CAPABILITIES, None,
                                                  ctypes.byref(caps), ctypes.byref(size), 0)
            if caps.value & CM_DEVCAP_REMOVABLE:
                break
            parent = wintypes.DWORD()
            if cfg.CM_Get_Parent(ctypes.byref(parent), cur, 0) != 0:
                raise EjectError("Could not find the parent USB device to eject.")
            cur = parent.value
        # veto = why Windows refused (type code + the name of whoever blocked it)
        veto, name = ctypes.c_int(0), ctypes.create_unicode_buffer(260)
        if cfg.CM_Request_Device_EjectW(cur, ctypes.byref(veto), name, 260, 0) != 0:
            raise EjectError(f"Windows vetoed the removal ({name.value or 'veto ' + str(veto.value)}).")


# ───────────────────────── threads ─────────────────────────
# Generic worker: runs fn(log) off the UI thread, so the process scan, the 1s
# I/O sample and the external commands never freeze the window. Results come
# back through signals, because Qt widgets may only be touched from the GUI thread.
class Job(QThread):
    log = Signal(str)
    ok = Signal(object)
    fail = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.ok.emit(self.fn(self.log.emit))
        except Exception as e:  # noqa: BLE001
            # EjectError and unexpected crashes both end up as a message in the UI
            self.fail.emit(str(e))


# ───────────────────────── UI ─────────────────────────
STYLE = """
QWidget { background:#14161c; color:#e6e8ee; font-size:13px; }
QLabel#title { font-size:18px; font-weight:600; }
QListWidget, QPlainTextEdit { background:#1c1f27; border:1px solid #2a2e3a; border-radius:8px; padding:4px; }
QListWidget::item { padding:10px; border-radius:6px; }
QListWidget::item:selected { background:#2f6fed; color:white; }
QPushButton { background:#262a36; border:1px solid #343a4a; border-radius:8px; padding:9px 14px; }
QPushButton:hover { background:#2f3544; }
QPushButton#eject { background:#2f6fed; border:none; font-weight:600; }
QPushButton#eject:hover { background:#4682ff; }
QPushButton:disabled { background:#1c1f27; color:#666c7c; }
QProgressBar { background:#1c1f27; border:none; border-radius:3px; max-height:6px; }
QProgressBar::chunk { background:#2f6fed; border-radius:3px; }
"""


class Window(QWidget):
    def __init__(self, backend):
        super().__init__()
        self.be, self.drives, self.job = backend, [], None
        self.setWindowTitle("DiskFucker3000 ⏏")
        self.resize(680, 540)
        self.setStyleSheet(STYLE)

        title = QLabel("Safely eject external drive")
        title.setObjectName("title")  # hook for the #title rule in STYLE
        self.list = QListWidget()
        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_eject = QPushButton("⏏ Safely eject")
        self.btn_eject.setObjectName("eject")
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # min == max == 0 makes it an indeterminate "busy" bar
        self.bar.hide()
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)

        row = QHBoxLayout()
        row.addWidget(self.btn_refresh)
        row.addStretch()
        row.addWidget(self.btn_eject)

        lay = QVBoxLayout(self)
        for w in (title, self.list):
            lay.addWidget(w)
        lay.addLayout(row)
        lay.addWidget(self.bar)
        lay.addWidget(self.logbox, 1)  # stretch factor 1: the log takes the spare space

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_eject.clicked.connect(self.eject)
        self.refresh()

    def log(self, msg):
        self.logbox.appendPlainText(msg)

    # While a job runs: show the progress bar and lock the controls so nobody
    # can start a second operation (or pick another drive) mid-eject.
    def busy(self, on):
        self.bar.setVisible(on)
        for b in (self.btn_refresh, self.btn_eject, self.list):
            b.setEnabled(not on)

    # Single entry point for background work. self.job keeps a reference to the
    # thread; if it were only a local variable, Python could garbage-collect the
    # QThread while it's still running and crash. The lambdas re-enable the UI
    # before handing the result (or the error) over.
    def run_job(self, fn, on_ok, on_fail=None):
        self.busy(True)
        self.job = Job(fn)
        self.job.log.connect(self.log)
        self.job.ok.connect(lambda r: (self.busy(False), on_ok(r)))
        self.job.fail.connect(lambda e: (self.busy(False), (on_fail or self.show_error)(e)))
        self.job.start()

    def show_error(self, msg):
        self.log(f"❌ {msg}")
        QMessageBox.warning(self, "Something went wrong", msg)

    def refresh(self):
        self.log("🔄 Looking for USB drives...")
        self.run_job(lambda log: self.be.list_drives(), self.fill_list)

    def fill_list(self, drives):
        self.drives = drives  # list index == row index, which eject() relies on
        self.list.clear()
        for d in drives:
            self.list.addItem(QListWidgetItem(d.title()))
        if drives:
            self.list.setCurrentRow(0)
        self.log(f"   {len(drives)} USB drive(s) found.")

    def eject(self):
        row = self.list.currentRow()
        if row < 0:
            QMessageBox.information(self, "DiskFucker3000", "Select a drive from the list.")
            return
        d = self.drives[row]
        self.log(f"\n── Ejecting {d.name} ──")
        self.run_job(lambda log: self.be.eject(d, log), lambda _: self.done(d))

    def done(self, d):
        self.log("✅ Done! You can unplug the cable.")
        QMessageBox.information(self, "DiskFucker3000", f"{d.name} was ejected safely.\nYou can unplug the cable.")
        self.refresh()  # the drive is gone now, so refresh the list


# ───────────────────────── main ─────────────────────────
def main():
    system = platform.system()
    if system == "Windows":
        # Locking a volume needs admin. If we're not elevated, relaunch ourselves
        # through the "runas" verb (triggers the UAC prompt) and quit this
        # instance. A PyInstaller build made with --uac-admin is already
        # elevated, so this branch is skipped there.
        if not ctypes.windll.shell32.IsUserAnAdmin():
            params = " ".join(f'"{a}"' for a in sys.argv)
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
            return 0
        backend = WindowsBackend()
    elif system == "Linux":
        backend = LinuxBackend()
    else:
        print("Unsupported system:", system)
        return 1

    app = QApplication(sys.argv)
    win = Window(backend)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())