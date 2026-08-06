#!/usr/bin/env python3
"""
Balatro Portrait Lite - Unified Build Script

Handles resource extraction and Game.love creation.
Runs on Windows, macOS, Linux, and Termux on Android.

Usage:
    python build.py [options]

Options:
    --disable-crt         Disable the CRT shader in portrait (fixes black-ellipse
                          artifacts on some devices; --crt is a deprecated alias)
    --keep-crt            Keep the CRT shader enabled (default; --no-crt is a
                          deprecated alias)
    --readabletro         Apply Readabletro font and high-res texture patch (default)
    --no-readabletro      Skip Readabletro patch
    --balatro PATH        Path to Balatro game file (skips the interactive prompt)
    --skip-setup          Skip resource extraction (if src/resources already exists)
    --force               Force Game.love rebuild even if sources are unchanged
    --version             Print the mod version and exit
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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
    try:
        with open(config_path, encoding="utf-8") as fh:
            match = re.search(r'^\s*version\s*=\s*"([^"]+)"', fh.read(), re.MULTILINE)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


MOD_VERSION = _read_mod_version()

CONFIG_FILE = ".buildconfig.json"
CACHE_FILE = ".build_cache.json"
OFFICIAL_ANDROID_PACKAGE = "com.playstack.balatro.android"
DEFAULT_BUILD_CONFIG = {
    "crt": False,
    "readabletro": True,
}

IS_TERMUX = "TERMUX_VERSION" in os.environ or os.path.exists("/data/data/com.termux")

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
    script_dir = os.path.dirname(os.path.abspath(__file__))
    game_files_dir = os.path.join(script_dir, "game_original_files")
    src_dir = os.path.join(script_dir, "src")

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
        for chunk in iter(lambda: f.read(65536), b""):
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
        print("  CRT shader noise uniform restored.")

    if not changed:
        return

    if restored_noise and restored_noise != len(CRT_NOISE_COMMENTED_LINES):
        print("  Warning: CRT shader noise patch only partially applied.")

    with open(crt_shader, "w", encoding="utf-8") as f:
        f.write(content)


def _apply_readabletro(src_dir, apply):
    font_src = os.path.join("patches", "readabletro", "fonts", "TypoQuik-Bold.ttf")
    font_dst = os.path.join(src_dir, "resources", "fonts", "TypoQuik-Bold.ttf")
    shader_src_dir = os.path.join("patches", "readabletro", "shaders")
    shader_dst_dir = os.path.join(src_dir, "resources", "shaders")
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
            fp = os.path.join(src_dir, rel)
            bak = fp + ".bak"
            if os.path.exists(bak):
                shutil.copy2(bak, fp)
                os.remove(bak)
        if os.path.exists(font_dst):
            os.remove(font_dst)
        for shader in ("background.fs", "splash.fs"):
            s_dst = os.path.join(shader_dst_dir, shader)
            bak = s_dst + ".bak"
            if os.path.exists(bak):
                shutil.copy2(bak, s_dst)
                os.remove(bak)
        if os.path.isdir(texture_dst_dir):
            for fn in os.listdir(texture_dst_dir):
                if fn.endswith(".bak"):
                    orig = os.path.join(texture_dst_dir, fn[:-4])
                    shutil.copy2(os.path.join(texture_dst_dir, fn), orig)
                    os.remove(os.path.join(texture_dst_dir, fn))


def build_game_love(apply_crt=False, apply_readabletro=False, force=False):
    """Package src/ into Game.love."""
    src_dir = "src"
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

    if apply_crt:
        _apply_crt_patch(src_dir, apply=False)
    if apply_readabletro:
        _apply_readabletro(src_dir, apply=False)

    size_mb = os.path.getsize(output_file) / 1_048_576
    print(f"  Game.love built  ({count} files, {size_mb:.2f} MB)")


# ─────────────────────────────────────────────────────────────────────────────
# CLI flag parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        prog="build.py",
        description="Balatro Portrait Lite - unified build script "
                    "(resource extraction, Game.love creation).",
    )
    crt = parser.add_mutually_exclusive_group()
    crt.add_argument("--disable-crt", "--crt", dest="crt", action="store_true", default=None,
                     help="disable the CRT shader in portrait (--crt is a deprecated alias)")
    crt.add_argument("--keep-crt", "--no-crt", dest="crt", action="store_false",
                     help="keep the CRT shader enabled (default; --no-crt is a deprecated alias)")

    rdb = parser.add_mutually_exclusive_group()
    rdb.add_argument("--readabletro", dest="readabletro", action="store_true", default=None,
                     help="apply Readabletro font and high-res texture patch (default)")
    rdb.add_argument("--no-readabletro", dest="readabletro", action="store_false",
                     help="skip Readabletro patch")
    parser.add_argument("--balatro", dest="balatro_path", metavar="PATH",
                        help="path to the Balatro game file (skips the interactive prompt)")
    parser.add_argument("--skip-setup", action="store_true",
                        help="skip resource extraction (if src/resources already exists)")
    parser.add_argument("--force", action="store_true",
                        help="force Game.love rebuild even if sources are unchanged")
    parser.add_argument("--version", action="version", version=f"%(prog)s {MOD_VERSION}")

    ns = parser.parse_args()
    flags = {k: v for k, v in vars(ns).items() if v is not None}
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  BALATRO PORTRAIT LITE - BUILD")
    print("=" * 60)

    cli = _parse_args()
    all_cli_set = all(k in cli for k in ("crt", "readabletro"))

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
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            print("  Settings saved to .buildconfig.json")

    apply_crt = cli.get("crt", config.get("crt", DEFAULT_BUILD_CONFIG["crt"]))
    apply_readabletro = cli.get("readabletro", config.get("readabletro", DEFAULT_BUILD_CONFIG["readabletro"]))
    balatro_path = cli.get("balatro_path", None)
    force = cli.get("force", False)

    total = 2

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
    build_game_love(apply_crt=apply_crt, apply_readabletro=apply_readabletro, force=force)


if __name__ == "__main__":
    main()
