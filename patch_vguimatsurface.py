#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HL2FlashlightFix - software patch for the Half-Life 2 flashlight HUD icon.

The Win32 vgui font backend in bin\\vguimatsurface.dll rasterizes glyphs with
the ANSI GDI import GetGlyphOutlineA.  On DBCS code pages (CP936/CP932/CP949)
the engine's Unicode characters U+00A9 / U+00AE cannot be looked up through
that ANSI entry point, so GDI returns the '?' glyph (the down arrow in
halflife2.ttf).

This tool renames that single import in the PE import name table:

    GetGlyphOutlineA  ->  GetGlyphOutlineW

The loader then binds the IAT slot to the Unicode version and every existing
call site in vguimatsurface.dll uses it.  The function signature is identical;
only the character argument is now treated as a UTF-16 code unit, which is
what the engine already passes.

Usage:
    python patch_vguimatsurface.py status
    python patch_vguimatsurface.py apply
    python patch_vguimatsurface.py revert
    python patch_vguimatsurface.py --dll <path> status|apply|revert

A backup is written next to the DLL as <name>.hl2flashlightfix.bak and is used
by "revert".  Steam updates replace the DLL, in which case "apply" has to be
run again.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys

OLD_NAME = b"GetGlyphOutlineA"
NEW_NAME = b"GetGlyphOutlineW"
DLL_REL = os.path.join("bin", "vguimatsurface.dll")
BACKUP_SUFFIX = ".hl2flashlightfix.bak"


# --------------------------------------------------------------------------- PE

class PEImage:
    def __init__(self, data: bytes):
        self.data = data
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise ValueError("not a PE file (missing MZ header)")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("not a PE file (missing PE signature)")
        self.coff = pe + 4
        self.nsect = struct.unpack_from("<H", data, self.coff + 2)[0]
        self.opt_size = struct.unpack_from("<H", data, self.coff + 16)[0]
        self.opt = self.coff + 20
        self.magic = struct.unpack_from("<H", data, self.opt)[0]
        if self.magic == 0x10B:          # PE32
            self.image_base = struct.unpack_from("<I", data, self.opt + 28)[0]
            self.dd = self.opt + 96
            self.thunk_size = 4
        elif self.magic == 0x20B:        # PE32+
            self.image_base = struct.unpack_from("<Q", data, self.opt + 24)[0]
            self.dd = self.opt + 112
            self.thunk_size = 8
        else:
            raise ValueError("unknown optional header magic 0x%X" % self.magic)
        self.size_of_image = struct.unpack_from("<I", data, self.opt + 56)[0]

        self.sections = []
        sec = self.opt + self.opt_size
        for i in range(self.nsect):
            off = sec + 40 * i
            name = data[off:off + 8].rstrip(b"\0")
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
            self.sections.append((name, vaddr, vsize, rawptr, rawsize))

    def dir_rva(self, index: int) -> int:
        return struct.unpack_from("<I", self.data, self.dd + 8 * index)[0]

    def rva2off(self, rva: int) -> int:
        for _name, va, vsize, ptr, rawsize in self.sections:
            if va <= rva < va + max(vsize, rawsize):
                return ptr + (rva - va)
        raise ValueError("RVA 0x%X is not mapped by any section" % rva)

    def cstr(self, rva: int):
        off = self.rva2off(rva)
        end = self.data.find(b"\0", off)
        if end < 0:
            raise ValueError("unterminated string at RVA 0x%X" % rva)
        return self.data[off:end], off


def iter_imports(pe: PEImage):
    """Yield (dll, name, name_file_offset, hint_file_offset)."""
    imp_rva = pe.dir_rva(1)
    if not imp_rva:
        return
    off = pe.rva2off(imp_rva)
    stride = pe.thunk_size
    while True:
        oft, _ts, _fc, name_rva, ft = struct.unpack_from("<IIIII", pe.data, off)
        if oft == 0 and name_rva == 0 and ft == 0:
            break
        dll_b, _ = pe.cstr(name_rva)
        dll = dll_b.decode("ascii", "replace")
        thunk_rva = oft or ft
        if thunk_rva:
            toff = pe.rva2off(thunk_rva)
            idx = 0
            while True:
                if stride == 8:
                    ent = struct.unpack_from("<Q", pe.data, toff + 8 * idx)[0]
                    if ent == 0:
                        break
                    if ent & (1 << 63):
                        idx += 1
                        continue
                    by_name = ent & 0xFFFFFFFF
                else:
                    ent = struct.unpack_from("<I", pe.data, toff + 4 * idx)[0]
                    if ent == 0:
                        break
                    if ent & 0x80000000:
                        idx += 1
                        continue
                    by_name = ent & 0x7FFFFFFF
                if not (0 < by_name < pe.size_of_image):
                    raise ValueError("bound imports are not supported")
                name, name_off = pe.cstr(by_name + 2)
                yield dll, name, name_off, pe.rva2off(by_name)
                idx += 1
        off += 20


def find_import(pe: PEImage, func: bytes):
    for dll, name, name_off, hint_off in iter_imports(pe):
        if dll.lower().startswith("gdi32") and name == func:
            return dll, name_off, hint_off
    return None


# --------------------------------------------------------------- export hints

def gdi32_export_index(func: bytes, magic: int):
    """Return the export name-table index of func in the matching gdi32.dll."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    names = ["System32"]
    if magic == 0x10B and os.path.isdir(os.path.join(root, "SysWOW64")):
        names = ["SysWOW64", "System32"]
    for sub in names:
        path = os.path.join(root, sub, "gdi32.dll")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            pe = PEImage(data)
            exp_rva = pe.dir_rva(0)
            if not exp_rva:
                continue
            off = pe.rva2off(exp_rva)
            n_names = struct.unpack_from("<I", data, off + 24)[0]
            names_rva = struct.unpack_from("<I", data, off + 32)[0]
            noff = pe.rva2off(names_rva)
            for i in range(n_names):
                nrva = struct.unpack_from("<I", data, noff + 4 * i)[0]
                nm, _ = pe.cstr(nrva)
                if nm == func:
                    return i
        except Exception:
            continue
    return None


# ------------------------------------------------------------------ game path

def steam_roots():
    roots = []
    try:
        import winreg
        for hive, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for value in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(winreg.QueryValueEx(k, value)[0])
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass
    return roots


def find_game_dir(hint: str | None):
    import re
    candidates = []
    if hint:
        candidates.append(hint)
    libraries = []
    for root in steam_roots():
        if not root:
            continue
        libraries.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                libraries.append(m.group(1).replace("\\\\", "\\"))
        except OSError:
            pass
    for lib in libraries:
        candidates.append(os.path.join(lib, "steamapps", "common", "Half-Life 2"))
    candidates.append(r"C:\Program Files (x86)\Steam\steamapps\common\Half-Life 2")
    for cand in candidates:
        cand = cand.strip().rstrip("\\/")
        if os.path.isfile(os.path.join(cand, DLL_REL)):
            return cand
    return None


# ---------------------------------------------------------------------- patch

def load(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def save(path: str, data: bytes):
    tmp = path + ".hl2flashlightfix.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def status(path: str) -> int:
    data = load(path)
    pe = PEImage(data)
    hit_a = find_import(pe, OLD_NAME)
    hit_w = find_import(pe, NEW_NAME)
    print("DLL      : %s" % path)
    print("arch     : %s" % ("PE32+" if pe.magic == 0x20B else "PE32"))
    print("backup   : %s" % ("yes" if os.path.isfile(path + BACKUP_SUFFIX) else "no"))
    if hit_w:
        print("status   : PATCHED (imports GetGlyphOutlineW)")
        return 0
    if hit_a:
        print("status   : VULNERABLE (imports GetGlyphOutlineA)")
        return 1
    print("status   : UNKNOWN (no GetGlyphOutlineA/W import found)")
    return 2


def apply_patch(path: str) -> int:
    data = bytearray(load(path))
    pe = PEImage(bytes(data))
    if find_import(pe, NEW_NAME):
        print("Already patched: %s" % path)
        return 0
    hit = find_import(pe, OLD_NAME)
    if not hit:
        print("ERROR: GetGlyphOutlineA import not found in %s" % path)
        return 2
    _dll, name_off, hint_off = hit

    backup = path + BACKUP_SUFFIX
    if not os.path.isfile(backup):
        shutil.copy2(path, backup)
        print("Backup   : %s" % backup)

    # 1) rename the import: ...A\0 -> ...W\0
    data[name_off + len(OLD_NAME) - 1] = ord("W")

    # 2) refresh the hint so the loader's first guess is correct as well
    hint = gdi32_export_index(NEW_NAME, pe.magic)
    if hint is not None:
        struct.pack_into("<H", data, hint_off, hint & 0xFFFF)

    save(path, bytes(data))

    # verify
    check = PEImage(load(path))
    if not find_import(check, NEW_NAME):
        print("ERROR: verification failed")
        return 3
    print("Patched  : %s" % path)
    print("Change   : GetGlyphOutlineA -> GetGlyphOutlineW")
    if hint is None:
        print("Note     : export hint not refreshed (name lookup still works)")
    return 0


def revert_patch(path: str) -> int:
    backup = path + BACKUP_SUFFIX
    if not os.path.isfile(backup):
        print("ERROR: backup not found: %s" % backup)
        return 2
    shutil.copy2(backup, path)
    os.remove(backup)
    print("Restored : %s" % path)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Patch vguimatsurface.dll GetGlyphOutlineA -> GetGlyphOutlineW")
    ap.add_argument("action", choices=["status", "apply", "revert"])
    ap.add_argument("--dll", help="path to vguimatsurface.dll (default: auto-detect Half-Life 2)")
    ap.add_argument("--game", help="Half-Life 2 install directory")
    args = ap.parse_args(argv)

    path = args.dll
    if not path:
        game = find_game_dir(args.game)
        if not game:
            print("ERROR: Half-Life 2 installation not found.")
            print('Use:  python patch_vguimatsurface.py apply --dll "D:\\...\\bin\\vguimatsurface.dll"')
            return 2
        path = os.path.join(game, DLL_REL)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        print("ERROR: file not found: %s" % path)
        return 2

    try:
        if args.action == "status":
            return status(path)
        if args.action == "apply":
            return apply_patch(path)
        return revert_patch(path)
    except PermissionError:
        print("ERROR: cannot write %s - close the game first." % path)
        return 4
    except OSError as exc:
        print("ERROR: %s" % exc)
        return 4


if __name__ == "__main__":
    sys.exit(main())
