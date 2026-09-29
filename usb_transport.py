"""
usb_transport.py — talk to an Arduino over raw USB from Termux on Android,
with no root and no /dev/ttyACM0.

WHY THIS EXISTS
  On a normal computer an Arduino shows up as a serial port (COM5,
  /dev/ttyACM0) and pyserial just opens it. Android doesn't do that: an app
  asks permission and gets a raw USB *file descriptor* instead. Termux hands
  you that descriptor through `termux-usb`. This module wraps that descriptor
  with libusb and speaks the serial protocol itself.

  On a laptop you do NOT need this file at all — backend.py uses pyserial.

REQUIREMENTS (Termux)
  pkg install python libusb termux-api      + the "Termux:API" app (F-Droid)

HOW IT'S USED
  backend.py must be launched *by* termux-usb so it inherits the descriptor:
      termux-usb -r -e ./launcher.sh /dev/bus/usb/001/002
  start.sh does all of that for you:   bash start.sh termux

SUPPORTED CHIPS
  * CDC-ACM  — genuine Arduino Uno / Mega / Leonardo (VID 2341)   [standard]
  * CH340    — cheap Uno clones (VID 1A86)                        [best-effort]
  Others (FTDI / CP210x) are detected and reported, but not driven.

DIAGNOSTICS
  bash start.sh probe      prints the VID:PID and USB layout of what's plugged in.
"""

import ctypes
import logging
import os
import queue
import struct
import sys
import threading
import time
from ctypes import POINTER, byref, c_int, c_ssize_t, c_ubyte, c_uint, c_uint8, c_uint16, c_void_p

log = logging.getLogger("pump_backend.usb")


class UsbError(Exception):
    """Anything that goes wrong talking to the USB device."""


KNOWN_VIDS = {
    0x2341: "Arduino",
    0x2A03: "Arduino",
    0x1B4F: "SparkFun",
    0x239A: "Adafruit",
    0x1A86: "CH340 clone",
    0x0403: "FTDI",
    0x10C4: "CP210x",
}

_ERRORS = {
    -1: "I/O error", -3: "access denied (permission?)", -4: "device disconnected",
    -5: "not found", -6: "busy (another app is using it?)", -7: "timeout",
    -8: "overflow", -9: "pipe/stall", -12: "not supported", -99: "other",
}


def _err(code):
    return "%s (%d)" % (_ERRORS.get(code, "libusb error"), code)


# --------------------------------------------------------------------------
# Thin libusb wrapper (ctypes — no pyusb needed)
# --------------------------------------------------------------------------

def _load_libusb():
    candidates = []
    prefix = os.environ.get("PREFIX")
    if prefix:
        candidates.append(os.path.join(prefix, "lib", "libusb-1.0.so"))
    candidates += ["libusb-1.0.so", "libusb-1.0.so.0"]
    last = None
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError as e:
            last = e
    raise UsbError("libusb not found — run:  pkg install libusb   (%s)" % last)


class _Usb:
    """One USB device, opened from an Android file descriptor."""

    def __init__(self, fd):
        lib = self.lib = _load_libusb()

        lib.libusb_init.argtypes = [POINTER(c_void_p)]
        lib.libusb_init.restype = c_int
        lib.libusb_wrap_sys_device.argtypes = [c_void_p, c_ssize_t, POINTER(c_void_p)]
        lib.libusb_wrap_sys_device.restype = c_int
        lib.libusb_control_transfer.argtypes = [
            c_void_p, c_uint8, c_uint8, c_uint16, c_uint16, c_void_p, c_uint16, c_uint]
        lib.libusb_control_transfer.restype = c_int
        lib.libusb_bulk_transfer.argtypes = [
            c_void_p, c_ubyte, c_void_p, c_int, POINTER(c_int), c_uint]
        lib.libusb_bulk_transfer.restype = c_int
        lib.libusb_claim_interface.argtypes = [c_void_p, c_int]
        lib.libusb_claim_interface.restype = c_int
        lib.libusb_release_interface.argtypes = [c_void_p, c_int]
        lib.libusb_close.argtypes = [c_void_p]
        lib.libusb_exit.argtypes = [c_void_p]
        try:
            lib.libusb_set_auto_detach_kernel_driver.argtypes = [c_void_p, c_int]
        except AttributeError:
            pass

        # Android: we only own this one descriptor — never try to enumerate.
        # (LIBUSB_OPTION_NO_DEVICE_DISCOVERY == 2; must be set before init.)
        try:
            lib.libusb_set_option(None, 2)
        except Exception:
            pass

        self.ctx = c_void_p()
        r = lib.libusb_init(byref(self.ctx))
        if r != 0:
            raise UsbError("libusb_init failed: " + _err(r))

        self.handle = c_void_p()
        r = lib.libusb_wrap_sys_device(self.ctx, fd, byref(self.handle))
        if r != 0:
            raise UsbError("could not open the USB device from Android's descriptor: " + _err(r))
        self._claimed = []

    # -- control ----------------------------------------------------------
    def control(self, bm_request_type, request, value, index, data=b"", length=0, timeout=1000):
        if bm_request_type & 0x80:  # device -> host
            buf = (c_ubyte * max(length, 1))()
            n = self.lib.libusb_control_transfer(
                self.handle, bm_request_type, request, value, index, buf, length, timeout)
            if n < 0:
                raise UsbError("control transfer failed: " + _err(n))
            return bytes(buf[:n])
        buf = (c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\0")
        n = self.lib.libusb_control_transfer(
            self.handle, bm_request_type, request, value, index, buf, len(data), timeout)
        if n < 0:
            raise UsbError("control transfer failed: " + _err(n))
        return b""

    # -- descriptors ------------------------------------------------------
    def device_ids(self):
        d = self.control(0x80, 0x06, 0x0100, 0, length=18)
        if len(d) < 12:
            raise UsbError("short device descriptor")
        vid, pid = struct.unpack_from("<HH", d, 8)
        return vid, pid

    def interfaces(self):
        hdr = self.control(0x80, 0x06, 0x0200, 0, length=9)
        total = struct.unpack_from("<H", hdr, 2)[0]
        return parse_config_descriptor(self.control(0x80, 0x06, 0x0200, 0, length=total))

    # -- interfaces / bulk ------------------------------------------------
    def claim(self, number):
        try:
            self.lib.libusb_set_auto_detach_kernel_driver(self.handle, 1)
        except Exception:
            pass
        r = self.lib.libusb_claim_interface(self.handle, number)
        if r != 0:
            raise UsbError("could not claim USB interface %d: %s" % (number, _err(r)))
        self._claimed.append(number)

    def bulk_read(self, endpoint, size=64, timeout_ms=100):
        buf = (c_ubyte * size)()
        got = c_int(0)
        r = self.lib.libusb_bulk_transfer(self.handle, endpoint, buf, size, byref(got), timeout_ms)
        if r == 0 or (r == -7 and got.value):
            return bytes(buf[:got.value])
        if r == -7:
            return b""
        raise UsbError("USB read failed: " + _err(r))

    def bulk_write(self, endpoint, data, timeout_ms=1000):
        view = memoryview(data)
        while len(view):
            chunk = bytes(view[:64])
            buf = (c_ubyte * len(chunk)).from_buffer_copy(chunk)
            sent = c_int(0)
            r = self.lib.libusb_bulk_transfer(
                self.handle, endpoint, buf, len(chunk), byref(sent), timeout_ms)
            if r != 0:
                raise UsbError("USB write failed: " + _err(r))
            view = view[max(sent.value, 1):]

    def close(self):
        try:
            for n in self._claimed:
                self.lib.libusb_release_interface(self.handle, n)
            self.lib.libusb_close(self.handle)
            self.lib.libusb_exit(self.ctx)
        except Exception:
            pass


def parse_config_descriptor(cfg):
    """Returns {interface_number: {"cls":..,"sub":..,"eps":[(addr, attrs, maxpkt), ..]}}"""
    ifaces, cur, i = {}, None, 0
    while i + 2 <= len(cfg):
        length, dtype = cfg[i], cfg[i + 1]
        if length == 0:
            break
        if dtype == 0x04 and i + 9 <= len(cfg):        # interface
            number, alt, cls, sub = cfg[i + 2], cfg[i + 3], cfg[i + 5], cfg[i + 6]
            cur = ifaces.setdefault(number, {"cls": cls, "sub": sub, "eps": []}) if alt == 0 else None
        elif dtype == 0x05 and cur is not None and i + 7 <= len(cfg):   # endpoint
            cur["eps"].append((cfg[i + 2], cfg[i + 3], struct.unpack_from("<H", cfg, i + 4)[0]))
        i += length
    return ifaces


def _bulk_pair(iface):
    ep_in = next((a for a, attr, _ in iface["eps"] if attr & 3 == 2 and a & 0x80), None)
    ep_out = next((a for a, attr, _ in iface["eps"] if attr & 3 == 2 and not a & 0x80), None)
    return ep_in, ep_out


def pick_endpoints(vid, ifaces):
    """Decide how to drive this device. Returns (mode, comm_iface, data_iface, ep_in, ep_out)."""
    comm = next((n for n, i in ifaces.items() if i["cls"] == 0x02 and i["sub"] == 0x02), None)
    for n, i in ifaces.items():
        if i["cls"] == 0x0A:                              # CDC data interface
            ep_in, ep_out = _bulk_pair(i)
            if ep_in and ep_out:
                return "cdc", comm, n, ep_in, ep_out
    if vid == 0x1A86:                                     # CH340 / CH341
        for n, i in ifaces.items():
            ep_in, ep_out = _bulk_pair(i)
            if ep_in and ep_out:
                return "ch340", None, n, ep_in, ep_out
    name = KNOWN_VIDS.get(vid, "unknown chip")
    raise UsbError(
        "USB device %04x (%s) isn't a chip this backend can drive yet. "
        "Supported: genuine Arduino (CDC) and CH340 clones. "
        "Run `bash start.sh probe` and share the output." % (vid, name))


# --------------------------------------------------------------------------
# The serial-like object backend.py uses (same interface as pyserial/mock)
# --------------------------------------------------------------------------

class TermuxUsbSerial:
    """write(bytes) / readline() -> bytes (b'' on timeout) / close()."""

    def __init__(self, fd, baud=9600, reset_wait=2.0, usb=None):
        self.usb = usb or _Usb(fd)
        self.baud = baud
        self._closed = False
        self._error = None
        self._lines = queue.Queue()
        self._wlock = threading.Lock()

        self.vid, self.pid = self.usb.device_ids()
        ifaces = self.usb.interfaces()
        (self.mode, self.comm_iface, self.data_iface,
         self.ep_in, self.ep_out) = pick_endpoints(self.vid, ifaces)

        for n in sorted({x for x in (self.comm_iface, self.data_iface) if x is not None}):
            self.usb.claim(n)

        if self.mode == "cdc":
            self._init_cdc()
        else:
            self._init_ch340()

        # Opening the port makes an Uno reboot; give the sketch time to start.
        time.sleep(reset_wait)
        threading.Thread(target=self._read_loop, daemon=True).start()

    # -- setup ------------------------------------------------------------
    def _try(self, what, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except UsbError as e:
            log.warning("USB setup step '%s' failed (continuing): %s", what, e)
            return b""

    def _init_cdc(self):
        idx = self.comm_iface if self.comm_iface is not None else self.data_iface
        coding = struct.pack("<IBBB", self.baud, 0, 0, 8)      # baud, 1 stop, no parity, 8 bits
        self._try("set line coding", self.usb.control, 0x21, 0x20, 0, idx, coding)
        self._try("set DTR/RTS", self.usb.control, 0x21, 0x22, 0x0003, idx, b"")

    def _ch340_baud(self, baud):
        factor, divisor = 1532620800 // baud, 3
        while factor > 0xFFF0 and divisor > 0:
            factor >>= 3
            divisor -= 1
        factor = 0x10000 - factor
        a = ((factor & 0xFF00) | divisor) | 0x80
        b = factor & 0xFF
        self._try("ch340 baud a", self.usb.control, 0x40, 0x9A, 0x1312, a)
        self._try("ch340 baud b", self.usb.control, 0x40, 0x9A, 0x0F2C, b)

    def _init_ch340(self):
        u = self.usb
        self._try("ch340 #1", u.control, 0xC0, 0x5F, 0, 0, length=8)
        self._try("ch340 #2", u.control, 0x40, 0xA1, 0, 0)
        self._ch340_baud(self.baud)
        self._try("ch340 #4", u.control, 0xC0, 0x95, 0x2518, 0, length=8)
        self._try("ch340 lcr", u.control, 0x40, 0x9A, 0x2518, 0x00C3)     # 8N1, rx+tx on
        self._try("ch340 #6", u.control, 0xC0, 0x95, 0x0706, 0, length=8)
        self._try("ch340 #7", u.control, 0x40, 0xA1, 0x501F, 0xD90A)
        self._ch340_baud(self.baud)
        self._try("ch340 DTR/RTS", u.control, 0x40, 0xA4, ~((1 << 5) | (1 << 6)) & 0xFFFF, 0)

    # -- reading / writing ------------------------------------------------
    def _read_loop(self):
        buf, errors = b"", 0
        while not self._closed:
            try:
                chunk = self.usb.bulk_read(self.ep_in, 64, 100)
                errors = 0
            except UsbError as e:
                errors += 1
                if errors < 3:
                    time.sleep(0.1)
                    continue
                self._error = e
                self._lines.put(None)
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._lines.put(line + b"\n")

    def write(self, data):
        if self._error or self._closed:
            raise UsbError("USB connection lost: %s" % (self._error or "closed"))
        with self._wlock:
            self.usb.bulk_write(self.ep_out, data)

    def readline(self):
        try:
            item = self._lines.get(timeout=1)
        except queue.Empty:
            return b""
        if item is None:
            self._lines.put(None)
            raise UsbError("USB connection lost: %s" % self._error)
        return item

    def describe(self):
        name = KNOWN_VIDS.get(self.vid, "USB device")
        return "%s %04x:%04x via Termux USB (%s)" % (name, self.vid, self.pid, self.mode)

    def close(self):
        self._closed = True
        self.usb.close()


# --------------------------------------------------------------------------

def get_termux_fd():
    """termux-usb passes the descriptor as $TERMUX_USB_FD and/or as an argument."""
    v = os.environ.get("TERMUX_USB_FD")
    if v and v.strip().isdigit():
        return int(v)
    for a in sys.argv[1:]:
        if a.isdigit():
            return int(a)
    return None


def _probe():
    fd = get_termux_fd()
    if fd is None:
        print("Not launched through termux-usb. Use:  bash start.sh probe")
        sys.exit(1)
    usb = _Usb(fd)
    vid, pid = usb.device_ids()
    ifaces = usb.interfaces()
    print("VIDPID=%04x:%04x" % (vid, pid))
    print("ARDUINO=%s" % ("yes" if vid in KNOWN_VIDS else "no"))
    print("CHIP=%s" % KNOWN_VIDS.get(vid, "unknown"))
    for n, i in sorted(ifaces.items()):
        eps = ", ".join("0x%02x/%s" % (a, {0: "ctrl", 1: "iso", 2: "bulk", 3: "int"}[at & 3])
                        for a, at, _ in i["eps"])
        print("INTERFACE %d class=%02x sub=%02x endpoints=[%s]" % (n, i["cls"], i["sub"], eps))
    try:
        mode = pick_endpoints(vid, ifaces)[0]
        print("SUPPORTED=yes (%s)" % mode)
    except UsbError as e:
        print("SUPPORTED=no")
        print("REASON=%s" % e)
    usb.close()


if __name__ == "__main__":
    if "--probe" in sys.argv:
        _probe()
    else:
        print(__doc__)
