"""Private X11 client. Xlib's fatal I/O handler stays outside the broker."""

import ctypes as C
import hashlib
import json
import re
import struct
import sys
import zlib


class XImage(C.Structure):
    _fields_ = (
        ("width", C.c_int), ("height", C.c_int), ("xoffset", C.c_int),
        ("format", C.c_int), ("data", C.c_void_p), ("byte_order", C.c_int),
        ("bitmap_unit", C.c_int), ("bitmap_bit_order", C.c_int),
        ("bitmap_pad", C.c_int), ("depth", C.c_int), ("bytes_per_line", C.c_int),
        ("bits_per_pixel", C.c_int), ("red_mask", C.c_ulong),
        ("green_mask", C.c_ulong), ("blue_mask", C.c_ulong),
    )


ALIASES = {
    "Alt": "Alt_L", "Ctrl": "Control_L", "Control": "Control_L",
    "Shift": "Shift_L", "Meta": "Super_L", "Super": "Super_L",
    "Enter": "Return", "ArrowLeft": "Left", "ArrowRight": "Right",
    "ArrowUp": "Up", "ArrowDown": "Down", "PageUp": "Page_Up",
    "PageDown": "Page_Down", "Backspace": "BackSpace", "Esc": "Escape",
    "Space": "space", " ": "space", "Tab": "Tab", "Delete": "Delete",
}
MODIFIERS = {"Alt_L", "Control_L", "Shift_L", "Super_L"}


def bind(lib, name, result, *args):
    function = getattr(lib, name)
    function.restype = result
    function.argtypes = args
    return function


class Connection:
    def __init__(self, name):
        x11 = C.CDLL("libX11.so.6")
        xtst = C.CDLL("libXtst.so.6")
        display, uint, win, integer = C.c_void_p, C.c_uint, C.c_ulong, C.c_int
        iref, uref = C.POINTER(integer), C.POINTER(uint)
        self.open = bind(x11, "XOpenDisplay", display, C.c_char_p)
        self.close = bind(x11, "XCloseDisplay", integer, display)
        self.sync = bind(x11, "XSync", integer, display, integer)
        root_window = bind(x11, "XDefaultRootWindow", win, display)
        default_screen = bind(x11, "XDefaultScreen", integer, display)
        screen_width = bind(x11, "XDisplayWidth", integer, display, integer)
        screen_height = bind(x11, "XDisplayHeight", integer, display, integer)
        self.image = bind(x11, "XGetImage", C.POINTER(XImage), display, win,
                          integer, integer, uint, uint, win, integer)
        self.destroy_image = bind(x11, "XDestroyImage", integer, C.POINTER(XImage))
        self.keycode = bind(x11, "XKeysymToKeycode", C.c_ubyte, display, win)
        self.keysym = bind(x11, "XStringToKeysym", win, C.c_char_p)
        self.key_level = bind(x11, "XkbKeycodeToKeysym", win, display, C.c_ubyte,
                              integer, integer)
        self.query_keys = bind(x11, "XQueryKeymap", integer, display, C.c_void_p)
        self.query_pointer = bind(x11, "XQueryPointer", integer, display, win,
                                  C.POINTER(win), C.POINTER(win), iref, iref, iref,
                                  iref, C.POINTER(uint))
        self.get_focus = bind(x11, "XGetInputFocus", integer, display, C.POINTER(win), iref)
        self.get_geometry = bind(x11, "XGetGeometry", integer, display, win, C.POINTER(win), iref, iref,
                                 uref, uref, uref, uref)
        self.translate = bind(x11, "XTranslateCoordinates", integer, display, win, win, integer, integer,
                              iref, iref, C.POINTER(win))
        self.selection_owner = bind(x11, "XGetSelectionOwner", win, display, win)
        intern_atom = bind(x11, "XInternAtom", win, display, C.c_char_p, integer)
        self.get_property = bind(x11, "XGetWindowProperty", integer, display, win, win,
                                 C.c_long, C.c_long, integer, win, C.POINTER(win),
                                 iref, C.POINTER(win), C.POINTER(win), C.POINTER(C.c_void_p))
        self.free = bind(x11, "XFree", integer, C.c_void_p)
        self.move = bind(xtst, "XTestFakeMotionEvent", integer, display, integer,
                         integer, integer, win)
        self.button = bind(xtst, "XTestFakeButtonEvent", integer, display, uint,
                           integer, win)
        self.key = bind(xtst, "XTestFakeKeyEvent", integer, display, uint, integer, win)
        self.xtest = bind(xtst, "XTestQueryExtension", integer, display, iref,
                          iref, iref, iref)
        # Every invocation requires a broker-owned display, including when XOpenDisplay fails.
        if not re.fullmatch(r"unix/:[0-9]+", name):
            raise ValueError("Expected a local X11 display")
        self.display = self.open(name.encode("ascii"))
        if not self.display:
            raise RuntimeError("Private display unavailable or unauthorized")
        self.root = root_window(self.display)
        self.screen = default_screen(self.display)
        self.width = screen_width(self.display, self.screen)
        self.height = screen_height(self.display, self.screen)
        self.clipboard = intern_atom(self.display, b"CLIPBOARD", 0)
        self.active_window = intern_atom(self.display, b"_NET_ACTIVE_WINDOW", 0)
        self.window_name = intern_atom(self.display, b"_NET_WM_NAME", 0)
        self.utf8 = intern_atom(self.display, b"UTF8_STRING", 0)

    def active(self):
        actual_type, count, remaining = C.c_ulong(), C.c_ulong(), C.c_ulong()
        width = C.c_int()
        contents = C.c_void_p()
        status = self.get_property(self.display, self.root, self.active_window, 0, 1, 0,
                                   33, C.byref(actual_type), C.byref(width), C.byref(count),
                                   C.byref(remaining), C.byref(contents))
        try:
            if status == 0 and width.value == 32 and count.value == 1 and contents:
                return C.cast(contents, C.POINTER(C.c_ulong))[0]
            return 0
        finally:
            if contents:
                self.free(contents)

    def observation(self):
        x, y, state = C.c_int(), C.c_int(), C.c_uint()
        root, child, focus = C.c_ulong(), C.c_ulong(), C.c_ulong()
        window_x, window_y, revert = C.c_int(), C.c_int(), C.c_int()
        self.query_pointer(self.display, self.root, C.byref(root), C.byref(child),
                           C.byref(x), C.byref(y), C.byref(window_x), C.byref(window_y),
                           C.byref(state))
        self.get_focus(self.display, C.byref(focus), C.byref(revert))
        values = [C.c_int() for _ in range(4)]
        if not self.xtest(self.display, *(C.byref(value) for value in values)):
            raise RuntimeError("Private display lacks XTEST")
        return {"width": self.width, "height": self.height, "x": x.value, "y": y.value,
                "state": state.value, "focus": focus.value, "root": self.root,
                "active_window": self.active(),
                "clipboard_owner": self.selection_owner(self.display, self.clipboard)}

    def catalog_ready(self):
        window = self.active()
        if not window:
            return False
        actual_type, count, remaining = C.c_ulong(), C.c_ulong(), C.c_ulong()
        width = C.c_int()
        contents = C.c_void_p()
        status = self.get_property(self.display, window, self.window_name, 0, 64, 0,
                                   self.utf8, C.byref(actual_type), C.byref(width), C.byref(count),
                                   C.byref(remaining), C.byref(contents))
        try:
            return (status == 0 and width.value == 8 and contents and
                    b"Local catalog" in C.string_at(contents, count.value))
        finally:
            if contents:
                self.free(contents)

    def focused_geometry(self):
        window = self.active()
        focus, revert = C.c_ulong(), C.c_int()
        self.get_focus(self.display, C.byref(focus), C.byref(revert))
        if window in (0, 1, self.root) or focus.value != window:
            return None
        root, child = C.c_ulong(), C.c_ulong()
        x, y = C.c_int(), C.c_int()
        width, height, border, depth = (C.c_uint() for _ in range(4))
        if not self.get_geometry(self.display, window, C.byref(root), C.byref(x), C.byref(y),
                                 C.byref(width), C.byref(height), C.byref(border), C.byref(depth)):
            return None
        if root.value != self.root or not self.translate(self.display, window, self.root, 0, 0,
                                                        C.byref(x), C.byref(y), C.byref(child)):
            return None
        return {"window": window, "viewport": [x.value, y.value, width.value, height.value],
                "root": [self.width, self.height]}

    def editor_ready(self, foreground):
        # Nano paints an inverse-video title bar on the first terminal row.
        # Sample below the borders and beyond the blank xterm's initial cursor.
        width = self.width - 48
        image = self.image(self.display, self.root, 24, 10, width, 1, C.c_ulong(-1).value, 2)
        if not image:
            raise RuntimeError("Could not observe the private editor")
        try:
            info = image.contents
            check_pixel_format(info)
            row = C.string_at(info.data, width * 4)
            color = foreground.to_bytes(3, "little")
            return any(row[offset:offset + 3] == color for offset in range(0, len(row), 4))
        finally:
            self.destroy_image(image)

    def png(self, region=None, previous_digest=None):
        x, y, width, height = region if region is not None else (0, 0, self.width, self.height)
        image = self.image(self.display, self.root, x, y, width, height,
                           C.c_ulong(-1).value, 2)
        if not image:
            raise RuntimeError("Could not capture private display")
        digest = b""
        try:
            info = image.contents
            check_pixel_format(info)
            if (info.width, info.height) != (width, height):
                raise RuntimeError("Unexpected Xvfb frame dimensions")
            stride = info.bytes_per_line
            if not width * 4 <= stride <= width * 4 + 4:
                raise RuntimeError("Unexpected Xvfb scanline size")
            size = height * stride
            if previous_digest is not None:
                # Hash the borrowed XImage buffer before allocating RGB or PNG data.
                digest = hashlib.sha256((C.c_ubyte * size).from_address(info.data)).digest()
                if digest == previous_digest:
                    return digest
            pixels = C.string_at(info.data, size)
        finally:
            self.destroy_image(image)
        compressor = zlib.compressobj(1)
        scanline = bytearray(1 + width * 3)
        chunks = []
        for offset in range(0, len(pixels), stride):
            end = offset + width * 4
            scanline[1::3] = pixels[offset + 2:end:4]
            scanline[2::3] = pixels[offset + 1:end:4]
            scanline[3::3] = pixels[offset:end:4]
            chunks.append(compressor.compress(scanline))
        chunks.append(compressor.flush())
        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (digest + b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) +
                png_chunk(b"IDAT", b"".join(chunks)) + png_chunk(b"IEND", b""))

    def key_for(self, name, shifted):
        name = ALIASES.get(name, name)
        if len(name) == 1:
            symbol = ord(name)
        else:
            symbol = self.keysym(name.encode("ascii"))
        code = self.keycode(self.display, symbol)
        if not code:
            raise ValueError("Key is not available in the private keyboard layout")
        needs_shift = (shifted and self.key_level(self.display, code, 0, 0) != symbol
                       and self.key_level(self.display, code, 0, 1) == symbol)
        return code, needs_shift

    def key_sequence(self, names):
        modifiers = [ALIASES.get(name, name) for name in names[:-1]]
        if any(name not in MODIFIERS for name in modifiers) or len(set(modifiers)) != len(modifiers):
            raise ValueError("Invalid key modifiers")
        key = names[-1]
        if modifiers and len(key) == 1 and key.isalpha():
            key = key.lower()
        code, shifted = self.key_for(key, True)
        if shifted and "Shift_L" not in modifiers:
            modifiers.append("Shift_L")
        held = [self.key_for(name, False)[0] for name in modifiers]
        if code in held:
            raise ValueError("Repeated final key")
        for modifier in held:
            self.key(self.display, modifier, 1, 0)
        self.key(self.display, code, 1, 0)
        self.key(self.display, code, 0, 0)
        for modifier in reversed(held):
            self.key(self.display, modifier, 0, 0)

    def release(self):
        keys = (C.c_ubyte * 32)()
        self.query_keys(self.display, keys)
        for code in range(8, 256):
            if keys[code // 8] & (1 << (code % 8)):
                self.key(self.display, code, 0, 0)
        state = self.observation()["state"]
        for button in range(1, 6):
            if state & (1 << (7 + button)):
                self.button(self.display, button, 0, 0)

    def apply(self, action):
        kind = action["type"]
        if kind in {"move", "click", "scroll", "drag"}:
            self.move(self.display, self.screen, action["x"], action["y"], 0)
        if kind == "click":
            self.button(self.display, action["button"], 1, 0)
            self.button(self.display, action["button"], 0, 0)
        elif kind == "drag":
            self.button(self.display, action["button"], 1, 0)
            self.move(self.display, self.screen, action["to_x"], action["to_y"], 0)
            self.button(self.display, action["button"], 0, 0)
        elif kind == "scroll":
            for axis, up, down in (("dy", 4, 5), ("dx", 6, 7)):
                steps = action[axis]
                button = up if steps < 0 else down
                for _ in range(abs(steps)):
                    self.button(self.display, button, 1, 0)
                    self.button(self.display, button, 0, 0)
        elif kind == "key":
            self.key_sequence(action["key"])
        elif kind == "type":
            for character in action["text"]:
                self.key_sequence([{"\n": "Return", "\t": "Tab"}.get(character, character)])
        elif kind == "release":
            self.release()
        self.sync(self.display, 0)


def check_pixel_format(info):
    if (info.bits_per_pixel, info.byte_order, info.red_mask, info.green_mask,
        info.blue_mask) != (32, 0, 0xFF0000, 0x00FF00, 0x0000FF):
        raise RuntimeError("Unsupported Xvfb pixel format")


def png_chunk(name, content):
    return (struct.pack(">I", len(content)) + name + content +
            struct.pack(">I", zlib.crc32(content, zlib.crc32(name))))


def dispatch(connection, operation, action=None):
    if operation == "png":
        return connection.png(action)
    if operation == "png-changed":
        return connection.png(previous_digest=bytes.fromhex(action) if action else b"")
    if operation == "observe":
        return json.dumps(connection.observation()).encode("ascii")
    if operation == "editor-ready":
        return b"1" if connection.editor_ready(action) else b"0"
    if operation == "catalog-ready":
        return b"1" if connection.catalog_ready() else b"0"
    if operation == "focused-geometry":
        return json.dumps(connection.focused_geometry()).encode("ascii")
    if operation == "apply":
        connection.apply(action)
        return b""
    raise ValueError("Unknown private X11 operation")


def serve(connection):
    source = sys.stdin.buffer
    destination = sys.stdout.buffer
    while request := source.readline(65537):
        if len(request) > 65536:
            return
        try:
            operation, action = json.loads(request)
            content = dispatch(connection, operation, action)
            status = 0
        except (OSError, RuntimeError, ValueError) as error:
            content = str(error).encode("utf-8")
            status = 1
        destination.write(struct.pack(">BI", status, len(content)))
        destination.write(content)
        destination.flush()


def main():
    try:
        operation, name = sys.argv[1:]
        connection = Connection(name)
        try:
            if operation == "serve":
                serve(connection)
            else:
                action = json.load(sys.stdin) if operation == "apply" else None
                sys.stdout.buffer.write(dispatch(connection, operation, action))
        finally:
            connection.close(connection.display)
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
