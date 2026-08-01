#!/usr/bin/env python3
"""
Balatro Portrait Mobile - Unified Build Script

Handles everything: resource extraction, Game.love creation, and APK packaging.
Runs on Windows, macOS, Linux, and Termux on Android. On Termux, use
`bash termux-build.sh` for a PC-free build from the installed Play Store app;
REAndroid/APKEditor is downloaded automatically to compile and pack resources.

Usage:
    python build.py [options]

Options:
    --disable-crt         Disable the CRT shader in portrait (fixes black-ellipse
                          artifacts on some devices; --crt is a deprecated alias)
    --keep-crt            Keep the CRT shader enabled (default; --no-crt is a
                          deprecated alias)
    --readabletro         Apply Readabletro font and high-res texture patch (default)
    --no-readabletro      Skip Readabletro patch
    --ios                 Also build an iOS .ipa for sideloading (EXPERIMENTAL)
    --no-ios              Skip the iOS build (default)
    --balatro PATH        Path to Balatro game file (skips the interactive prompt)
    --skip-setup          Skip resource extraction (if src/resources already exists)
    --skip-apk            Only build Game.love, skip APK packaging
    --force               Force Game.love rebuild even if sources are unchanged
    --import-save PATH    Bake a desktop save folder or Takeout zip into the APK
    --steamodded [TAG]    Bundle Steamodded into the APK (default: latest release)
    --version             Print the mod version and exit
"""

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

def _read_mod_version():
    """The mod version lives in src/portrait_config.lua (PORTRAIT_CONFIG.version)
    so the game can show it in Options -> Diagnostics; parse it from there
    instead of keeping a second copy here that can drift."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "src", "portrait_config.lua")
    with open(config_path, encoding="utf-8") as fh:
        match = re.search(r'^\s*version\s*=\s*"([^"]+)"', fh.read(), re.MULTILINE)
    if not match:
        raise RuntimeError("PORTRAIT_CONFIG.version not found in src/portrait_config.lua")
    return match.group(1)


MOD_VERSION = _read_mod_version()

CONFIG_FILE = ".buildconfig.json"
CACHE_FILE  = ".build_cache.json"
OFFICIAL_ANDROID_PACKAGE = "com.playstack.balatro.android"
DEFAULT_BUILD_CONFIG = {
    # Legacy key name kept for saved .buildconfig.json files: "crt": True
    # means the CRT shader gets DISABLED (the user-facing flag is --disable-crt).
    "crt": False,
    "readabletro": True,
    "ios": False,
}

WORKDIR  = os.path.abspath("balatro-mobile-maker")
JAVA_BIN = "java"  # resolved in _setup_jdk()

IS_TERMUX = bool(os.environ.get("TERMUX_VERSION")) or os.path.isdir("/data/data/com.termux/files/usr")

APKEDITOR_URL  = "https://github.com/REAndroid/APKEditor/releases/download/V1.4.9/APKEditor-1.4.9.jar"
SIGNER_URL     = "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar"
PATCH_URL      = "https://github.com/blake502/balatro-apk-maker/releases/download/Additional-Tools-1.0/Balatro-APK-Patch.zip"
# The LMM base APK is rebuilt upstream without version-pinned URLs, so it cannot
# be hash-pinned here. All version-pinned downloads above are SHA-256 verified.
LOVELY_APK_URL = "https://lmm.shorty.systems/base.apk"

# iOS (experimental): prebuilt unsigned LOVE iOS app shell from balatro-apk-maker.
# Game.love is inserted into the .app, Info.plist is locked to portrait, and the
# result is sideloaded with Sideloadly/AltStore which re-sign it with the user's
# Apple ID — no Xcode or macOS needed.
IOS_BASE_URL = "https://github.com/blake502/balatro-apk-maker/releases/download/Additional-Tools-1.0/balatro-base.ipa"

TOOL_SHA256 = {
    APKEDITOR_URL: "a9cd40df818845456be6d696de6110c89edf4b0a0580cb83438ed6b25a366e67",
    SIGNER_URL:    "e1299fd6fcf4da527dd53735b56127e8ea922a321128123b9c32d619bba1d835",
    PATCH_URL:     "efa47e113b15b2963a193ff6b988544f58e0dab26a75b439943d55dba0f5b489",
    IOS_BASE_URL:  "1b7a060dc06f7d3ea54fd24f04ff9fcedde7a0e3539c96bfee175499b723f661",
}

# These strings must match exactly what's in src/game.lua
CRT_PATCH_ORIGINAL = 'if (not G.recording_mode or G.video_control) and true then'
CRT_PATCH_MODIFIED = 'if (not G.recording_mode or G.video_control) and true and not G.F_PORTRAIT then'
CRT_MASK_ORIGINAL = '''    //smoothly transition the edge to black
    //buffer for the outer edge, this gets wonky if there is no buffer
    MY_HIGHP_OR_MEDIUMP number mask = (1.0 - smoothstep(1.0-feather_fac,1.0,abs(tc.x) - BUFF))
                * (1.0 - smoothstep(1.0-feather_fac,1.0,abs(tc.y) - BUFF));'''
CRT_MASK_MODIFIED = CRT_MASK_ORIGINAL + '''
    mask = 1.0 - (1.0 - mask) * clamp(crt_intensity/(0.16*0.3), 0.0, 1.0);'''
CRT_NOISE_COMMENTED_LINES = (
    ("//extern MY_HIGHP_OR_MEDIUMP number noise_fac;", "extern MY_HIGHP_OR_MEDIUMP number noise_fac;"),
    ("    //MY_HIGHP_OR_MEDIUMP number x = (tc.x - mod(tc.x, 0.002)) * (tc.y - mod(tc.y, 0.0013)) * time * 1000.0;",
     "    MY_HIGHP_OR_MEDIUMP number x = (tc.x - mod(tc.x, 0.002)) * (tc.y - mod(tc.y, 0.0013)) * time * 1000.0;"),
    ("\t//x = mod( x, 13.0 ) * mod( x, 123.0 );",
     "\tx = mod( x, 13.0 ) * mod( x, 123.0 );"),
    ("\t//MY_HIGHP_OR_MEDIUMP number dx = mod( x, 0.11 )/0.11;",
     "\tMY_HIGHP_OR_MEDIUMP number dx = mod( x, 0.11 )/0.11;"),
    ("\t//rgb_result = (1.0-clamp( noise_fac*artifact_amplifier, 0.0,1.0 ))*rgb_result + dx * clamp( noise_fac*artifact_amplifier, 0.0,1.0 ) * vec3(1.0,1.0,1.0);",
     "\trgb_result = (1.0-clamp( noise_fac*artifact_amplifier, 0.0,1.0 ))*rgb_result + dx * clamp( noise_fac*artifact_amplifier, 0.0,1.0 ) * vec3(1.0,1.0,1.0);"),
)

GAME_LOVE_EXCLUDE = {"smali", ".pyc", "__pycache__", ".git", ".gitignore", ".bak", ".build_cache.json"}

READABLETRO_LUA_PATCHES = {
    "game.lua": [
        (
            '{file = "resources/fonts/m6x11plus.ttf", render_scale = self.TILESIZE*10, TEXT_HEIGHT_SCALE = 0.83, TEXT_OFFSET = {x=10,y=-20}, FONTSCALE = 0.1, squish = 1, DESCSCALE = 1}',
            '{file = "resources/fonts/TypoQuik-Bold.ttf", render_scale = self.TILESIZE*10, TEXT_HEIGHT_SCALE = 0.83, TEXT_OFFSET = {x=10,y=-20}, FONTSCALE = 0.1, squish = 1, DESCSCALE = 1}',
        ),
        (
            '{file = "resources/fonts/m6x11plus.ttf", render_scale = self.TILESIZE*10, TEXT_HEIGHT_SCALE = 0.9, TEXT_OFFSET = {x=10,y=15}, FONTSCALE = 0.1, squish = 1, DESCSCALE = 1}',
            '{file = "resources/fonts/TypoQuik-Bold.ttf", render_scale = self.TILESIZE*10, TEXT_HEIGHT_SCALE = 0.83, TEXT_OFFSET = {x=10,y=-20}, FONTSCALE = 0.1, squish = 1, DESCSCALE = 1}',
        ),
    ],
    "functions/misc_functions.lua": [
        (
            'font = love.graphics.setNewFont("resources/fonts/m6x11plus.ttf", 20),',
            'font = love.graphics.setNewFont("resources/fonts/TypoQuik-Bold.ttf", 20),',
        ),
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class BuildProfiler:
    def __init__(self):
        self.steps = []
        self._wall = time.time()

    def step(self, name):
        return _Step(self, name)

    def record(self, name, duration):
        self.steps.append((name, duration))

    def report(self):
        total = sum(d for _, d in self.steps)
        wall  = time.time() - self._wall
        sep = "-" * 50
        print(f"\n{sep}")
        print("Build time breakdown:")
        for name, d in self.steps:
            pct = d / total * 100 if total else 0
            print(f"  {name:<28}  {d:>5.1f}s  ({pct:.0f}%)")
        print(f"  {'Total':<28}  {wall:>5.1f}s")
        print(sep)


class _Step:
    def __init__(self, profiler, name):
        self.p    = profiler
        self.name = name

    def __enter__(self):
        self._t = time.time()
        return self

    def __exit__(self, *_):
        self.p.record(self.name, time.time() - self._t)


def _ask(prompt, default=None):
    hint = f" [{'y' if default else 'n'}]" if default is not None else ""
    while True:
        try:
            r = input(f"{prompt}{hint}: ").strip().lower()
        except EOFError:
            if default is not None:
                print(f"{prompt}{hint}: {'y' if default else 'n'}")
                return default
            raise
        if not r and default is not None:
            return default
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_download(url, dest):
    expected = TOOL_SHA256.get(url)
    if not expected:
        return
    actual = _sha256_of(dest)
    if actual != expected:
        print(f"  ERROR: SHA-256 mismatch for {os.path.basename(dest)}")
        print(f"    expected: {expected}")
        print(f"    actual:   {actual}")
        print("  The download may be corrupted or tampered with.")
        print(f"  Delete the file and re-run: {dest}")
        os.remove(dest)
        sys.exit(1)


def _download(url, dest):
    if os.path.exists(dest):
        print(f"  Already downloaded: {os.path.basename(dest)}")
        _verify_download(url, dest)
        return
    print(f"  Downloading {os.path.basename(dest)} ...")
    tmp = dest + ".part"
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=120)
        total = int(resp.headers.get("Content-Length", 0))
        done  = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct    = min(done / total * 100, 100)
                    filled = int(30 * pct / 100)
                    bar    = "#" * filled + "-" * (30 - filled)
                    sys.stdout.write(f"\r    [{bar}] {pct:.0f}%  {done/1e6:.1f}/{total/1e6:.1f} MB")
                    sys.stdout.flush()
        sys.stdout.write("\n")
        os.rename(tmp, dest)
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"\n  ERROR: could not download {url}: {exc}")
        sys.exit(1)
    _verify_download(url, dest)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Resource extraction
# ─────────────────────────────────────────────────────────────────────────────

def _find_installed_android_balatro_apk():
    """Return the installed official Android base APK path when Termux can see it."""
    if not IS_TERMUX:
        return None

    pm = "/system/bin/pm"
    if not os.path.exists(pm):
        return None

    result = subprocess.run(
        [pm, "path", OFFICIAL_ANDROID_PACKAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        if line.startswith("package:") and line.endswith("/base.apk"):
            path = line[len("package:"):].strip()
            if os.path.exists(path):
                return path
    return None


def _find_extracted_source_folder(game_files_dir, folder):
    """Find a desktop/LÖVE or official Android APK resource folder."""
    source_options = (
        os.path.join(game_files_dir, folder),
        os.path.join(game_files_dir, "assets", folder),
    )
    for source in source_options:
        if os.path.exists(source):
            return source
    return None


def setup_resources(balatro_path=None):
    """Extract resources and localization from the Balatro game file into src/."""
    script_dir      = os.path.dirname(os.path.abspath(__file__))
    game_files_dir  = os.path.join(script_dir, "game_original_files")
    src_dir         = os.path.join(script_dir, "src")

    if not balatro_path:
        balatro_path = _find_installed_android_balatro_apk()
        if balatro_path:
            print()
            print("  Detected installed official Android Balatro - using its base APK.")

    if not balatro_path:
        print()
        print("  Path to Balatro game file:")
        print("    Windows  D:\\Steam\\steamapps\\common\\Balatro\\Balatro.exe")
        print("    Linux    ~/.steam/steam/steamapps/common/Balatro/Balatro.exe")
        print("    macOS    ~/Library/Application Support/Steam/steamapps/common/Balatro/Balatro.app/Contents/Resources/Balatro.love")
        print("             (you can also pass the .app bundle path - it will be found automatically)")
        print("    Android  official Balatro base.apk copied from the Play install")
        balatro_path = input("  > ").strip().strip('"').strip("'")

    balatro_path = os.path.expanduser(balatro_path)

    if os.path.isdir(balatro_path) and balatro_path.rstrip("/").endswith(".app"):
        love_path = os.path.join(balatro_path, "Contents", "Resources", "Balatro.love")
        if os.path.exists(love_path):
            print("  Detected macOS app bundle - using Balatro.love inside it.")
            balatro_path = love_path

    if not os.path.exists(balatro_path):
        print(f"  ERROR: File not found: {balatro_path}")
        sys.exit(1)

    print(f"  Extracting {os.path.basename(balatro_path)} ...")
    if os.path.exists(game_files_dir):
        shutil.rmtree(game_files_dir)
    os.makedirs(game_files_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(balatro_path, "r") as z:
            z.extractall(game_files_dir)
    except zipfile.BadZipFile:
        print("  ERROR: Not a valid ZIP/exe file.")
        sys.exit(1)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)

    for folder in ("resources", "localization"):
        src = _find_extracted_source_folder(game_files_dir, folder)
        dst = os.path.join(src_dir, folder)
        if not src:
            print(f"  ERROR: '{folder}' not found inside Balatro game file - wrong file?")
            sys.exit(1)
        print(f"  Copying {folder} ...")
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)

    print("  Done - resources ready.")


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Game.love build
# ─────────────────────────────────────────────────────────────────────────────

def _file_hash(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sources_changed(src_dir, output_file):
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
        except Exception:
            pass

    current = {}
    for root, _, files in os.walk(src_dir):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                current[fp] = _file_hash(fp)
            except Exception:
                current[fp] = str(os.path.getmtime(fp))

    unchanged = os.path.exists(output_file) and current == cache.get("files", {})
    return not unchanged, current


def _apply_crt_patch(src_dir, apply):
    game_lua = os.path.join(src_dir, "game.lua")
    if not os.path.exists(game_lua):
        return
    with open(game_lua, "r", encoding="utf-8") as f:
        content = f.read()
    if apply:
        if CRT_PATCH_MODIFIED in content:
            return
        if CRT_PATCH_ORIGINAL not in content:
            print("  Warning: CRT patch target not found in game.lua - skipping.")
            return
        content = content.replace(CRT_PATCH_ORIGINAL, CRT_PATCH_MODIFIED)
        print("  CRT shader disabled for all portrait modes.")
    else:
        if CRT_PATCH_ORIGINAL in content:
            return
        content = content.replace(CRT_PATCH_MODIFIED, CRT_PATCH_ORIGINAL)
    with open(game_lua, "w", encoding="utf-8") as f:
        f.write(content)


def _apply_crt_slider_mask_patch(src_dir):
    crt_shader = os.path.join(src_dir, "resources", "shaders", "CRT.fs")
    if not os.path.exists(crt_shader):
        return
    with open(crt_shader, "r", encoding="utf-8") as f:
        content = f.read()
    changed = False
    if CRT_MASK_MODIFIED not in content:
        if CRT_MASK_ORIGINAL not in content:
            print("  Warning: CRT slider mask patch target not found in CRT.fs - skipping.")
        else:
            content = content.replace(CRT_MASK_ORIGINAL, CRT_MASK_MODIFIED)
            changed = True
            print("  CRT edge mask now follows the CRT slider.")

    restored_noise = 0
    for original, replacement in CRT_NOISE_COMMENTED_LINES:
        if original in content:
            content = content.replace(original, replacement)
            restored_noise += 1

    if restored_noise:
        changed = True
        print("  Android CRT shader noise uniform restored.")

    if not changed:
        return

    if restored_noise and restored_noise != len(CRT_NOISE_COMMENTED_LINES):
        print("  Warning: Android CRT shader noise patch only partially applied.")

    with open(crt_shader, "w", encoding="utf-8") as f:
        f.write(content)


def _apply_readabletro(src_dir, apply):
    font_src        = os.path.join("patches", "readabletro", "fonts", "TypoQuik-Bold.ttf")
    font_dst        = os.path.join(src_dir, "resources", "fonts", "TypoQuik-Bold.ttf")
    shader_src_dir  = os.path.join("patches", "readabletro", "shaders")
    shader_dst_dir  = os.path.join(src_dir, "resources", "shaders")
    texture_src_dir = os.path.join("patches", "readabletro", "textures", "2x")
    texture_dst_dir = os.path.join(src_dir, "resources", "textures", "2x")

    if apply:
        for rel, pairs in READABLETRO_LUA_PATCHES.items():
            fp = os.path.join(src_dir, rel)
            if not os.path.exists(fp):
                continue
            shutil.copy2(fp, fp + ".bak")
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read()
            for orig, mod in pairs:
                content = content.replace(orig, mod)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)

        if os.path.exists(font_src):
            os.makedirs(os.path.dirname(font_dst), exist_ok=True)
            shutil.copy2(font_src, font_dst)

        os.makedirs(shader_dst_dir, exist_ok=True)
        for shader in ("background.fs", "splash.fs"):
            s_src = os.path.join(shader_src_dir, shader)
            s_dst = os.path.join(shader_dst_dir, shader)
            if os.path.exists(s_dst):
                shutil.copy2(s_dst, s_dst + ".bak")
            if os.path.exists(s_src):
                shutil.copy2(s_src, s_dst)

        tex_count = 0
        if os.path.isdir(texture_src_dir):
            os.makedirs(texture_dst_dir, exist_ok=True)
            for fn in os.listdir(texture_src_dir):
                if not fn.endswith(".png"):
                    continue
                t_src = os.path.join(texture_src_dir, fn)
                t_dst = os.path.join(texture_dst_dir, fn)
                if os.path.exists(t_dst):
                    shutil.copy2(t_dst, t_dst + ".bak")
                shutil.copy2(t_src, t_dst)
                tex_count += 1
        print(f"  Readabletro applied ({tex_count} textures).")

    else:
        for rel in READABLETRO_LUA_PATCHES:
            fp  = os.path.join(src_dir, rel)
            bak = fp + ".bak"
            if os.path.exists(bak):
                shutil.copy2(bak, fp)
                os.remove(bak)
        if os.path.exists(font_dst):
            os.remove(font_dst)
        for shader in ("background.fs", "splash.fs"):
            s_dst = os.path.join(shader_dst_dir, shader)
            bak   = s_dst + ".bak"
            if os.path.exists(bak):
                shutil.copy2(bak, s_dst)
                os.remove(bak)
        if os.path.isdir(texture_dst_dir):
            for fn in os.listdir(texture_dst_dir):
                if fn.endswith(".bak"):
                    orig = os.path.join(texture_dst_dir, fn[:-4])
                    shutil.copy2(os.path.join(texture_dst_dir, fn), orig)
                    os.remove(os.path.join(texture_dst_dir, fn))


STEAMODDED_REPO = "Steamodded/smods"


def _patch_lovely_mod_dir(apk_out):
    """Repoint Lovely's mod folder from save/ASET/Mods to save/game/Mods so it sits
    inside the LOVE save dir, which the Lua side can write to. This is what lets a
    bundled mod (build.py --steamodded) install itself on first run."""
    old, new = b"/save/ASET/Mods", b"/save/game/Mods"
    patched = 0
    for arch in ("arm64-v8a", "armeabi-v7a"):
        for subpath in ("root", ""):
            so = os.path.join(apk_out, subpath, "lib", arch, "liblove.so")
            if not os.path.exists(so):
                continue
            with open(so, "rb") as f:
                data = f.read()
            if new in data:
                patched += 1
                break
            count = data.count(old)
            if count != 1:
                print(f"  Warning: skipped mod-dir patch for {arch} (found {count} matches).")
                continue
            with open(so, "wb") as f:
                f.write(data.replace(old, new))
            patched += 1
            break
    if not patched:
        raise RuntimeError("could not repoint Lovely mod directory (liblove.so unpatched)")


def _steamodded_versions(limit=15):
    url = f"https://api.github.com/repos/{STEAMODDED_REPO}/releases?per_page={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return [rel["tag_name"] for rel in json.loads(resp.read().decode())]


def _fetch_steamodded(version):
    """Download a Steamodded release tag and return {relpath: bytes} of the mod."""
    os.makedirs(WORKDIR, exist_ok=True)
    tmp = os.path.join(WORKDIR, "steamodded.zip")
    _download(f"https://github.com/{STEAMODDED_REPO}/archive/refs/tags/{version}.zip", tmp)
    files = {}
    with zipfile.ZipFile(tmp) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            parts = name.split("/", 1)            # strip the top "smods-<version>/" folder
            if len(parts) == 2 and parts[1]:
                files[parts[1]] = z.read(name)
    os.remove(tmp)
    if not files:
        raise RuntimeError("Steamodded archive was empty")
    return files


def _resolve_steamodded(flag_value, interactive):
    """Return ('Steamodded', {relpath: bytes}) to bundle, or None."""
    version = flag_value
    if version is None and interactive:
        if not _ask("     Also bundle Steamodded (mod framework)?", default=False):
            return None
        try:
            versions = _steamodded_versions()
        except Exception as exc:
            print(f"  Could not fetch Steamodded versions: {exc}")
            return None
        print()
        for i, tag in enumerate(versions, 1):
            print(f"     {i:2}. {tag}" + ("   (latest)" if i == 1 else ""))
        try:
            sel = input("     Pick a version [1]: ").strip()
        except EOFError:
            sel = ""
        idx = int(sel) - 1 if sel.isdigit() and 1 <= int(sel) <= len(versions) else 0
        version = versions[idx]
    if not version:
        return None
    if version == "latest":
        version = _steamodded_versions()[0]
    print(f"  Steamodded: fetching {version} ...")
    try:
        files = _fetch_steamodded(version)
    except Exception as exc:
        print(f"  Steamodded fetch failed: {exc}")
        return None
    print(f"  Steamodded: bundling {len(files)} files ({version}).")
    return ("Steamodded", files)


def build_game_love(apply_crt=False, apply_readabletro=False, force=False, import_saves=None, import_mods=None):
    """Package src/ into Game.love."""
    src_dir     = "src"
    output_file = "Game.love"

    if not os.path.exists(src_dir):
        print("  ERROR: src/ not found.")
        sys.exit(1)

    if apply_crt:
        _apply_crt_patch(src_dir, apply=True)
    _apply_crt_slider_mask_patch(src_dir)
    if apply_readabletro:
        _apply_readabletro(src_dir, apply=True)

    changed, current_files = _sources_changed(src_dir, output_file)

    if not force and not changed:
        print("  No source changes - skipping rebuild.")
        if apply_crt:
            _apply_crt_patch(src_dir, apply=False)
        if apply_readabletro:
            _apply_readabletro(src_dir, apply=False)
        return

    with open(CACHE_FILE, "w") as f:
        json.dump({"files": current_files}, f, indent=2)

    if os.path.exists(output_file):
        os.remove(output_file)

    def _skip(path):
        return any(p in path for p in GAME_LOVE_EXCLUDE)

    # Lovely-injector regex patches anchor on '\n' newlines. If a Lua source has
    # CRLF (e.g. Windows autocrlf checkout), some SMODS regex patches fail to match
    # and leave behind dangling original code that creates Lua syntax errors at runtime
    # (observed: "ambiguous syntax (function call x new statement)" near the leftover
    # `(k==6 or k ==16 ...)` block in create_UIBox_your_collection_blinds).
    # Normalize all packaged Lua files to LF so patches apply correctly.
    count = 0
    with zipfile.ZipFile(output_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = [d for d in dirs if not _skip(os.path.join(root, d))]
            for fn in files:
                if _skip(fn):
                    continue
                fp = os.path.join(root, fn)
                arc = os.path.relpath(fp, src_dir)
                if fn.endswith(".lua"):
                    with open(fp, "rb") as f:
                        data = f.read()
                    if b"\r\n" in data:
                        data = data.replace(b"\r\n", b"\n")
                    zf.writestr(arc.replace(os.sep, "/"), data)
                else:
                    zf.write(fp, arc)
                count += 1

        if import_saves:
            for slot, kinds in import_saves.items():
                for kind, data in kinds.items():
                    if kind == "save":
                        continue
                    zf.writestr(f"import_save/{slot}/{kind}.jkr", data)
                    count += 1

        if import_mods:
            for modname, mfiles in import_mods.items():
                for relpath, data in mfiles.items():
                    zf.writestr(f"install_mods/{modname}/{relpath}", data)
                    count += 1

    if apply_crt:
        _apply_crt_patch(src_dir, apply=False)
    if apply_readabletro:
        _apply_readabletro(src_dir, apply=False)

    size_mb = os.path.getsize(output_file) / 1_048_576
    print(f"  Game.love built  ({count} files, {size_mb:.2f} MB)")


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — APK build
# ─────────────────────────────────────────────────────────────────────────────

def _setup_jdk():
    global JAVA_BIN
    java = shutil.which("java")
    if not java:
        print("  ERROR: Java not found in PATH.")
        print("  Please install Java (JDK 17 or newer) and ensure 'java' is added to system PATH.")
        sys.exit(1)
    JAVA_BIN = java
    print(f"  Java: {JAVA_BIN}")


def _java(jar, args):
    result = subprocess.run([JAVA_BIN, "-jar", jar] + args, cwd=WORKDIR,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR:\n{result.stderr}")
        sys.exit(1)


def _sign_apk(signer):
    """Sign the APK using uber-apk-signer. Automatically copies custom keystore into WORKDIR if needed."""
    ks_path = os.environ.get("KEYSTORE_PATH") or os.environ.get("KEYSTORE_FILE") or os.environ.get("KEYSTORE")
    if not ks_path:
        candidates = ("custom.keystore", "ks.keystore", "balatro.keystore", "release.keystore")
        for candidate in candidates:
            if os.path.exists(candidate):
                ks_path = os.path.abspath(candidate)
                break
            elif os.path.exists(os.path.join(WORKDIR, candidate)):
                ks_path = os.path.abspath(os.path.join(WORKDIR, candidate))
                break

    ks_pass = os.environ.get("KEYSTORE_PASSWORD")
    ks_alias = os.environ.get("KEYSTORE_ALIAS")

    if ks_path and os.path.exists(ks_path) and ks_pass and ks_alias:
        target_ks_in_workdir = os.path.join(WORKDIR, os.path.basename(ks_path))
        if os.path.abspath(ks_path) != os.path.abspath(target_ks_in_workdir):
            print(f"  Copying custom keystore to build directory: {os.path.basename(ks_path)}")
            shutil.copy2(ks_path, target_ks_in_workdir)

        rel_ks = os.path.basename(ks_path)
        print(f"  Signing with custom keystore: {rel_ks} (alias: {ks_alias})")

        args = [
            "-a", "balatro.apk",
            "--ks", rel_ks,
            "--ksPass", ks_pass,
            "--ksAlias", ks_alias,
        ]
        ks_key_pass = os.environ.get("KEYSTORE_KEY_PASSWORD") or ks_pass
        args.extend(["--ksKeyPass", ks_key_pass])

        _java(signer, args)
        return "balatro-portrait-android.apk"
    else:
        if ks_path or ks_pass or ks_alias:
            print("  Warning: Custom keystore details incomplete. Requires keystore path, KEYSTORE_PASSWORD, and KEYSTORE_ALIAS.")
            print("  Falling back to debug certificate.")
        print("  Signing with debug certificate (uber-apk-signer) ...")
        _java(signer, ["-a", "balatro.apk"])
        return "balatro-portrait-android.apk"


def _apkeditor(jar, args):
    """Run REAndroid/APKEditor. APKEditor compiles resources using a pure Java
    toolchain, which avoids the platform-specific aapt/aapt2 issues of apktool.
    """
    _java(jar, args)


def _patch_sdl_portrait_orientation(apk_out):
    smali_candidates = (
        os.path.join(apk_out, "smali", "classes", "org", "libsdl", "app", "SDLActivity.smali"),
        os.path.join(apk_out, "root", "smali", "classes", "org", "libsdl", "app", "SDLActivity.smali"),
        os.path.join(apk_out, "smali", "org", "libsdl", "app", "SDLActivity.smali"),
        os.path.join(apk_out, "root", "smali", "org", "libsdl", "app", "SDLActivity.smali"),
    )
    smali_path = None
    for cand in smali_candidates:
        if os.path.exists(cand):
            smali_path = cand
            break

    if not smali_path:
        raise FileNotFoundError(f"SDLActivity.smali not found in candidate paths under {apk_out}")

    with open(smali_path, "r", encoding="utf-8") as f:
        smali = f.read()

    marker = "# Balatro Portrait: force portrait orientation"
    if marker in smali:
        return

    signature = ".method public setOrientationBis(IIZLjava/lang/String;)V"
    method_start = smali.find(signature)
    if method_start == -1:
        raise RuntimeError(f"{signature} not found in {smali_path}")

    method_end = smali.find(".end method", method_start)
    if method_end == -1:
        raise RuntimeError(f"{signature} has no .end method in {smali_path}")

    method_body = smali[method_start:method_end]
    header_match = re.search(r"(?m)^(\s+\.(?:locals|registers)\s+\d+\s*)$", method_body)
    if not header_match:
        raise RuntimeError(f"{signature} has no .locals/.registers header in {smali_path}")

    insert_at = method_start + header_match.end()
    injected = (
        "\n"
        f"    {marker}\n"
        "    const/4 p1, 0x1\n"
        "    invoke-virtual {p0, p1}, Landroid/app/Activity;->setRequestedOrientation(I)V\n"
        "    return-void\n"
    )
    smali = smali[:insert_at] + injected + smali[insert_at:]

    with open(smali_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(smali)


def build_apk(profiler=None):
    """Download tools, package, and sign the always-Lovely Android APK."""
    game_love_src = os.path.abspath("Game.love")
    if not os.path.exists(game_love_src):
        print("  ERROR: Game.love not found - run the build step first.")
        sys.exit(1)

    os.makedirs(WORKDIR, exist_ok=True)
    p = profiler or BuildProfiler()

    apk_fn  = "lovely-base.apk"
    apk_url = LOVELY_APK_URL

    apkeditor = os.path.join(WORKDIR, "APKEditor.jar")
    signer    = os.path.join(WORKDIR, "uber-apk-signer.jar")
    patch_zip = os.path.join(WORKDIR, "Balatro-APK-Patch.zip")
    base_apk  = os.path.join(WORKDIR, apk_fn)

    with p.step("JDK setup"):
        _setup_jdk()

    with p.step("Download tools"):
        downloads = [(APKEDITOR_URL, apkeditor), (SIGNER_URL, signer),
                     (PATCH_URL, patch_zip), (apk_url, base_apk)]
        for url, dest in downloads:
            _download(url, dest)

    apk_out = os.path.join(WORKDIR, "balatro-apk")
    with p.step("Unpack APK"):
        if os.path.exists(apk_out):
            shutil.rmtree(apk_out)
        print("  Unpacking APK ...")
        _apkeditor(apkeditor, ["d", "-t", "xml", "-f", "-i", apk_fn, "-o", "balatro-apk"])

    with p.step("Patch manifest"):
        patch_dir = os.path.join(WORKDIR, "Balatro-APK-Patch")
        if os.path.exists(patch_dir):
            shutil.rmtree(patch_dir)
        with zipfile.ZipFile(patch_zip) as z:
            z.extractall(WORKDIR)

        manifest_path = os.path.join(apk_out, "AndroidManifest.xml")

        with open(manifest_path) as f:
            m = f.read()
        m = m.replace("systems.shorty.lmm", "com.unofficial.balatro")
        m = re.sub(r'android:label="[^"]+"',         'android:label="Balatro"',          m)
        m = re.sub(r'android:versionCode="[^"]+"',   f'android:versionCode="{int(time.time())}"', m)
        m = re.sub(r'android:versionName="[^"]+"',   f'android:versionName="{MOD_VERSION}-lovely"', m)
        m = re.sub(r'\sandroid:debuggable="[^"]+"',  "",                                  m)
        m = re.sub(r'android:screenOrientation="[^"]+"', 'android:screenOrientation="portrait"', m)
        m = re.sub(r'android:configChanges="[^"]+"',
                   'android:configChanges="orientation|screenSize|smallestScreenSize|screenLayout|uiMode|keyboard|keyboardHidden|navigation"', m)
        with open(manifest_path, "w") as f:
            f.write(m)
        print("  [Lovely] Manifest patched.")

        _patch_sdl_portrait_orientation(apk_out)
        print("  [Lovely] SDL orientation patched.")

        _patch_lovely_mod_dir(apk_out)
        print("  [Lovely] Mod folder repointed to save/game/Mods.")

        # Icons
        for density in ["hdpi","mdpi","xhdpi","xxhdpi","xxxhdpi"]:
            src = os.path.join(WORKDIR, "res", f"drawable-{density}", "love.png")
            for subpath in ("root", ""):
                dst = os.path.join(apk_out, subpath, "res", f"drawable-{density}", "love.png")
                if os.path.exists(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy(src, dst)

        # Game.love
        game_dst = os.path.join(apk_out, "root", "assets", "game.love") if os.path.exists(os.path.join(apk_out, "root")) else os.path.join(apk_out, "assets", "game.love")
        os.makedirs(os.path.dirname(game_dst), exist_ok=True)
        shutil.copy(game_love_src, game_dst)

    with p.step("Repack APK"):
        print("  Repacking APK ...")
        _apkeditor(apkeditor, ["b", "-f", "-i", "balatro-apk", "-o", "balatro.apk"])

    with p.step("Sign APK"):
        print("  Signing APK ...")
        signed_apk = _sign_apk(signer)

    p.report()
    print(f"\n{'=' * 60}")
    print("  Build complete - MODDED (Lovely)")
    print(f"  APK: balatro-mobile-maker/{signed_apk}")
    print(f"{'=' * 60}")

    print()
    print("  Mod installation:")
    print("  1. Launch the game once")
    print("  2. Put mod folders in game/Mods/")
    print("  3. Restart the game")
    print("  See docs/MODDING.md for no-root, root, and ADB paths.")

    return signed_apk


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — iOS IPA build (experimental)
# ─────────────────────────────────────────────────────────────────────────────

def build_ipa(profiler=None):
    """Package Game.love into an unsigned, portrait-locked iOS .ipa.

    The base is a prebuilt LOVE iOS app shell (no game data). We rewrite the
    archive instead of appending so Info.plist can be replaced: orientation is
    locked to portrait and the bundle version is set to MOD_VERSION. The IPA is
    unsigned by design — Sideloadly/AltStore re-sign it at install time.
    """
    game_love_src = os.path.abspath("Game.love")
    if not os.path.exists(game_love_src):
        print("  ERROR: Game.love not found - run the build step first.")
        sys.exit(1)

    os.makedirs(WORKDIR, exist_ok=True)
    p = profiler or BuildProfiler()

    base_ipa  = os.path.join(WORKDIR, "balatro-base.ipa")
    out_ipa   = "balatro-portrait.ipa"
    plist_arc = "Payload/Balatro.app/Info.plist"
    love_arc  = "Payload/Balatro.app/game.love"

    with p.step("Download iOS base"):
        _download(IOS_BASE_URL, base_ipa)

    with p.step("Pack IPA"):
        print("  Packing IPA (portrait-locked Info.plist + game.love) ...")
        if os.path.exists(out_ipa):
            os.remove(out_ipa)
        with zipfile.ZipFile(base_ipa, "r") as zin, \
             zipfile.ZipFile(out_ipa, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in (plist_arc, love_arc):
                    continue
                # passing the original ZipInfo preserves unix permissions on
                # the Balatro executable inside the .app bundle
                zout.writestr(item, zin.read(item.filename))

            plist = plistlib.loads(zin.read(plist_arc))
            plist["UISupportedInterfaceOrientations"] = ["UIInterfaceOrientationPortrait"]
            plist["UISupportedInterfaceOrientations~ipad"] = ["UIInterfaceOrientationPortrait"]
            plist["CFBundleShortVersionString"] = MOD_VERSION
            plist["CFBundleVersion"] = MOD_VERSION
            zout.writestr(plist_arc, plistlib.dumps(plist))

            zout.write(game_love_src, love_arc)

    p.report()
    size_mb = os.path.getsize(out_ipa) / 1_048_576
    print(f"\n{'=' * 60}")
    print("  iOS build complete - EXPERIMENTAL (untested by maintainer)")
    print(f"  IPA: {out_ipa}  ({size_mb:.2f} MB)")
    print(f"{'=' * 60}")
    print()
    print("  Sideload with Sideloadly or AltStore (signs with your Apple ID).")
    print("  Lovely mod support is Android-only; the IPA is always vanilla.")
    print("  See docs/IOS.md for instructions - and please report results!")


# ─────────────────────────────────────────────────────────────────────────────
# CLI flag parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="Balatro Portrait Mobile - unified build script "
                    "(resource extraction, Game.love creation, APK packaging).",
    )
    # The flag polarity used to be a trap: "--crt" DISABLED the CRT shader.
    # The explicit names are canonical now; the old ones stay as aliases so
    # existing scripts and docs keep working.
    crt = parser.add_mutually_exclusive_group()
    crt.add_argument("--disable-crt", "--crt", dest="crt", action="store_true", default=None,
                     help="disable the CRT shader in portrait (--crt is a deprecated alias)")
    crt.add_argument("--keep-crt", "--no-crt", dest="crt", action="store_false",
                     help="keep the CRT shader enabled (default; --no-crt is a deprecated alias)")

    rdb = parser.add_mutually_exclusive_group()
    rdb.add_argument("--readabletro",    dest="readabletro", action="store_true", default=None,
                     help="apply Readabletro font and high-res texture patch (default)")
    rdb.add_argument("--no-readabletro", dest="readabletro", action="store_false",
                     help="skip Readabletro patch")

    ios = parser.add_mutually_exclusive_group()
    ios.add_argument("--ios",    dest="ios", action="store_true", default=None,
                     help="also build an iOS .ipa for sideloading (EXPERIMENTAL)")
    ios.add_argument("--no-ios", dest="ios", action="store_false",
                     help="skip the iOS build (default)")

    parser.add_argument("--balatro", dest="balatro_path", metavar="PATH",
                        help="path to the Balatro game file (skips the interactive prompt)")
    parser.add_argument("--skip-setup", action="store_true",
                        help="skip resource extraction (if src/resources already exists)")
    parser.add_argument("--skip-apk", action="store_true",
                        help="only build Game.love, skip APK packaging")
    parser.add_argument("--force", action="store_true",
                        help="force Game.love rebuild even if sources are unchanged")
    parser.add_argument("--import-save", dest="import_save", metavar="PATH",
                        help="bake a desktop Balatro save folder or Takeout zip into the APK")
    parser.add_argument("--steamodded", dest="steamodded", metavar="VERSION", nargs="?", const="latest",
                        help="bundle Steamodded into the APK (optional version tag; default latest)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {MOD_VERSION}")

    ns = parser.parse_args()
    flags = {k: v for k, v in vars(ns).items() if v is not None}
    return flags


def _load_collect_saves():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "import_save.py")
    spec = importlib.util.spec_from_file_location("import_save", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect_saves


def _resolve_import_save(flag_path, interactive):
    """Return {slot: {kind: bytes}} of progression saves to bake in, or None."""
    path = flag_path
    if path is None and interactive:
        print()
        print("  Import an existing save (optional)")
        print("     Bring your unlocks and progression from desktop Balatro or the")
        print("     official Play Store app (via Google Takeout). Leave blank to skip.")
        guess = os.path.join(os.environ["APPDATA"], "Balatro") if os.environ.get("APPDATA") else None
        if guess and os.path.isdir(guess):
            print(f"     Detected desktop save: {guess}")
        try:
            path = input("     Save folder or Takeout zip (blank = skip): ").strip().strip('"')
        except EOFError:
            path = ""
    if not path:
        return None

    try:
        saves = _load_collect_saves()(path)
    except Exception as exc:
        print(f"  Save import skipped: {exc}")
        return None

    # Progression only (meta/profile/unlock_notify); an in-progress run is not
    # carried over, matching the documented transfer.
    cleaned = {}
    for slot, kinds in saves.items():
        keep = {k: v for k, v in kinds.items() if k != "save"}
        if keep:
            cleaned[slot] = keep
    if not cleaned:
        print("  Save import: no profile data found in that source.")
        return None
    print("  Save import: baking " + ", ".join(f"profile {s}" for s in sorted(cleaned)) + " into the build.")
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  BALATRO PORTRAIT MOBILE - BUILD")
    print("=" * 60)

    cli = _parse_args()
    all_cli_set = all(k in cli for k in ("crt", "readabletro", "ios"))

    # ── Load or collect config ──────────────────────────────────────────────
    config = {}
    if not all_cli_set:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                print()
                print("  Saved settings:")
                print(f"    Disable CRT shader:            {'yes' if config.get('crt') else 'no'}")
                print(f"    Readabletro:                   {'yes' if config.get('readabletro') else 'no'}")
                print("    Lovely mod support:            yes (always on for Android)")
                print(f"    iOS .ipa (experimental):       {'yes' if config.get('ios') else 'no'}")
                print()
                if not _ask("  Use these settings?", default=True):
                    config = {}
            except Exception:
                config = {}

        if not config:
            print()
            print("  -- Build options --------------------------------------")
            print()
            print("  1. CRT Shader Patch")
            print("     On some devices the CRT shader causes visual artifacts in")
            print("     portrait mode: a black ellipse or a thin colored sliver at")
            print("     the bottom of the screen. Enable this to disable CRT and")
            print("     fix those issues. If your game looks fine, skip it.")
            config["crt"] = _ask("     Disable the CRT shader?", default=DEFAULT_BUILD_CONFIG["crt"])
            print()
            print("  2. Readabletro")
            print("     Replaces the pixel font with TypoQuik-Bold and adds")
            print("     high-resolution card and UI textures.")
            config["readabletro"] = _ask("     Apply Readabletro?", default=DEFAULT_BUILD_CONFIG["readabletro"])
            print()
            print("  3. iOS Build (EXPERIMENTAL)")
            print("     Also produces balatro-portrait.ipa for sideloading with")
            print("     Sideloadly or AltStore. Untested by the maintainer -")
            print("     feedback welcome. Lovely is not available on iOS.")
            config["ios"] = _ask("     Build iOS .ipa?", default=DEFAULT_BUILD_CONFIG["ios"])
            print()
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            print("  Settings saved to .buildconfig.json")

    apply_crt         = cli.get("crt",          config.get("crt",         DEFAULT_BUILD_CONFIG["crt"]))
    apply_readabletro = cli.get("readabletro",   config.get("readabletro", DEFAULT_BUILD_CONFIG["readabletro"]))
    build_ios         = cli.get("ios",           config.get("ios",         DEFAULT_BUILD_CONFIG["ios"]))
    balatro_path      = cli.get("balatro_path",  None)
    force             = cli.get("force",         False)
    import_saves      = _resolve_import_save(
        cli.get("import_save"),
        interactive=("import_save" not in cli and not all_cli_set),
    )
    steamodded        = _resolve_steamodded(
        cli.get("steamodded"),
        interactive=("steamodded" not in cli and not all_cli_set),
    )
    import_mods       = dict([steamodded]) if steamodded else None

    total = 4 if build_ios else 3

    # ── Step 1 — Resources ──────────────────────────────────────────────────
    needs_setup = not os.path.exists(os.path.join("src", "resources"))
    print()
    if cli.get("skip_setup"):
        print(f"[1/{total}] Skipping resource setup (--skip-setup).")
    elif needs_setup:
        print(f"[1/{total}] Game resources not found - extracting from Balatro.exe ...")
        setup_resources(balatro_path)
    else:
        print(f"[1/{total}] Resources already present.")

    # ── Step 2 — Game.love ─────────────────────────────────────────────────
    print()
    print(f"[2/{total}] Building Game.love ...")
    build_game_love(apply_crt=apply_crt, apply_readabletro=apply_readabletro,
                    force=force or bool(import_saves) or bool(import_mods),
                    import_saves=import_saves, import_mods=import_mods)

    # ── Step 3 — APK ───────────────────────────────────────────────────────
    if cli.get("skip_apk"):
        print()
        print(f"[3/{total}] Skipping APK build (--skip-apk).")
    else:
        print()
        print(f"[3/{total}] Building APK ...")
        signed_apk = build_apk(profiler=BuildProfiler())
    # ── Step 4 — iOS IPA (experimental) ────────────────────────────────────
    if build_ios:
        print()
        print(f"[4/{total}] Building iOS IPA (experimental) ...")
        build_ipa(profiler=BuildProfiler())


if __name__ == "__main__":
    main()
