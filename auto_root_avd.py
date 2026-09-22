#!/usr/bin/env python3
"""
auto_root_avd.py — Auto-rooting + Burp TLS-interception setup for an Android emulator.

Rooting rig: a Google Play AVD rooted via rootAVD (FAKEBOOTING / Magisk) with the
ZygiskNext, NoHello and AdguardCert Magisk modules, where the AdGuard module is
repurposed to mount the Burp Suite CA into the system trust store so Burp can
intercept all TLS traffic on the emulator.

Default run is AUTONOMOUS (no prompts). Pass --manual to restore the y/n prompts
and wait-for-user steps. Pass --create-avd to force AVD creation in --manual mode.

The only inherently manual step is opening Magisk on the AVD and installing
fakeboot.img (this is how rootAVD's FAKEBOOTING method works on Play images).

Target platform: Windows. Requires Android Studio / SDK (adb, sdkmanager,
avdmanager, emulator) and OpenSSL.
"""

import argparse
import configparser
import ctypes 
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
TOOLING_DIR = SCRIPT_DIR / "root_tooling"
CONFIG_PATH = SCRIPT_DIR / "avd_config.ini"   # AVD settings (see load_avd_config)

TOTAL_STEPS = 6
AVD_NAME = "auto_root_avd"      # default; overridden by avd_config.ini [avd] name
DEVICE = "pixel_8"             # default device profile
DEFAULT_API = 35              # default API level
IMAGE_TMPL = "system-images;android-{lvl};google_apis_playstore;x86_64"
BURP_CERT_URL = "http://127.0.0.1:8080/cert"
OPENSSL_WINGET_ID = "ShiningLight.OpenSSL.Light"

CANCEL_MSG = "[!] Root script canceled, thank you for using"
FINAL_MSG = ("[+] Script thành công bật mạng và proxy tại emulator > `...` > "
             "Settings > Proxy > Manual proxy configuration để bắt proxy qua Burp")

# Downloadable root tooling: local filename -> URL
TOOLING_URLS = {
    "rootAVD.zip":
        "https://github.com/trongngk/tmp/releases/download/checklist/rootAVD.zip",
    "NoHello.zip":
        "https://github.com/MhmRdd/NoHello/releases/download/0.0.7/"
        "Nohello-v0.0.7-53-4d53ecf-release.zip",
    "ZygiskNext.zip":
        "https://github.com/LSPosed/ZygiskNext/releases/download/1.5.0/"
        "Zygisk-Next-1.5.0-843-5217106-release.zip",
    "adguardcert.zip":
        "https://github.com/trongngk/tmp/releases/download/checklist/"
        "adguardcert-v2.1_2.zip",
}

# Modules pushed to the device, in Magisk install order (framework before module).
MODULE_INSTALL_ORDER = ["ZygiskNext.zip", "NoHello.zip", "adguardcert.zip"]

# Module install robustness: after first root, Magisk/network may not be ready.
MAGISK_SETTLE_SEC = 20       # pause after Magisk is ready, before installing
MODULE_RETRIES = 3           # attempts per push / install
MODULE_RETRY_DELAY = 15      # seconds between attempts

# --------------------------------------------------------------------------- #
# Globals (resolved / set at runtime)
# --------------------------------------------------------------------------- #
MANUAL = False
CREATE_AVD = False

# AVD settings — populated from avd_config.ini in main() (defaults below).
API_LEVEL = DEFAULT_API      # int
DEVICE_PROFILE = DEVICE      # str, avdmanager -d
RAM_MB = None                # int MB or None (image default)
STORAGE = None               # str size e.g. "8G" or None (data partition)
SDCARD = None                # str size e.g. "512M" or None
AVD_HOME = None              # str dir or None → Android default (~/.android/avd)

ADB = None
SDK_ROOT = None             # Android SDK root (parent of platform-tools)
SDKMANAGER = None
AVDMANAGER = None
ANDROID_CLI = None          # new 'android' CLI (replaces deprecated sdkmanager)
EMULATOR = None
OPENSSL = None
ROOTAVD_BAT = None          # Path to rootAVD.bat if already present on the system
BURP_HASH = None            # subject_hash_old of the Burp CA (set by cert thread)
RUN_ENV = None              # env for subprocesses (JAVA_HOME / ANDROID_AVD_HOME pinned)

CANCEL = threading.Event()
EMULATOR_PROC = None        # Popen handle for the launched emulator
_active_procs = set()       # tracked child processes (terminated on cancel)
_procs_lock = threading.Lock()
_print_lock = threading.Lock()


class Cancel(Exception):
    """Raised to abort the run cleanly (user 'no' or internal cancel)."""


class MissingTool(Exception):
    """Raised when a required prerequisite is absent (instructions already printed)."""


# --------------------------------------------------------------------------- #
# Logging / progress
# --------------------------------------------------------------------------- #
def log(msg=""):
    with _print_lock:
        print(msg, flush=True)


def err(msg=""):
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001
        pass


def alert(msg):
    """Prominent banner for a manual / attention step."""
    bar = "=" * 70
    yellow = "\033[33m"
    reset = "\033[0m"
    with _print_lock:
        print(f"\n{yellow}{bar}\n>>> {msg}\n{bar}{reset}", flush=True)


class Progress:
    """Single universal progress bar across all phases."""
    WIDTH = 20

    def __init__(self):
        self.current = 0

    def _render(self, name):
        filled = round(self.current / TOTAL_STEPS * self.WIDTH)
        bar = "#" * filled + "-" * (self.WIDTH - filled)
        pct = round(self.current / TOTAL_STEPS * 100)
        remaining = TOTAL_STEPS - self.current
        step_no = min(self.current + 1, TOTAL_STEPS)
        with _print_lock:
            print(f"[{bar}] {pct}% | Step {step_no}/{TOTAL_STEPS} | "
                  f"Remaining: {remaining} | {name}", flush=True)

    def start(self, i, name):
        self.current = i - 1
        self._render(name)

    def done(self):
        self.current += 1
        with _print_lock:
            filled = round(self.current / TOTAL_STEPS * self.WIDTH)
            bar = "#" * filled + "-" * (self.WIDTH - filled)
            pct = round(self.current / TOTAL_STEPS * 100)
            remaining = TOTAL_STEPS - self.current
            print(f"[{bar}] {pct}% | Remaining: {remaining} | done", flush=True)


progress = Progress()


# --------------------------------------------------------------------------- #
# Subprocess helpers (tracked for clean cancellation)
# --------------------------------------------------------------------------- #
def run(cmd, cwd=None, input_data=None, capture=True, timeout=None):
    """Run a command, tracking the child so cleanup() can terminate it.

    Uses RUN_ENV (JAVA_HOME / ANDROID_AVD_HOME pinned) once tooling is resolved.
    `timeout` (seconds) kills the child and returns returncode 124 if exceeded.
    """
    if CANCEL.is_set():
        raise Cancel()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=RUN_ENV,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with _procs_lock:
        _active_procs.add(proc)
    try:
        try:
            out, errout = proc.communicate(input=input_data, timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out, errout = proc.communicate()
            rc = 124
    finally:
        with _procs_lock:
            _active_procs.discard(proc)
    return types.SimpleNamespace(
        returncode=rc,
        stdout=out or "",
        stderr=errout or "",
    )


def bat(batpath, *args, input_data=None, capture=True):
    """Run a .bat file from its own directory (avoids quoting/relative-path issues).

    capture=False streams the child's output live to the console (used for the
    long, interactive rootAVD FAKEBOOTIMG step).
    """
    batpath = Path(batpath)
    return run(["cmd", "/c", batpath.name, *map(str, args)],
               cwd=str(batpath.parent), input_data=input_data, capture=capture)


def adb(*args, capture=True, timeout=None):
    return run([ADB, *map(str, args)], capture=capture, timeout=timeout)


def su(cmd):
    """Run a command as root on the device via 'su -c'."""
    return adb("shell", f"su -c '{cmd}'")


def is_rooted(timeout=25):
    """Quick, bounded check whether the running AVD already has working root.
    Returns True if `su -c id` reports uid=0. Bounded so a Magisk grant prompt
    (rooted-but-not-granted) can't hang the pre-check."""
    try:
        r = adb("shell", "su", "-c", "id", timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
    return "uid=0" in (r.stdout or "")


# --------------------------------------------------------------------------- #
# Device wait helpers (cancellation-aware polling)
# --------------------------------------------------------------------------- #
def _check_cancel():
    if CANCEL.is_set():
        raise Cancel()


def _interruptible_sleep(seconds):
    """Sleep in 1s slices so Ctrl+C / cancel stays responsive."""
    for _ in range(int(seconds)):
        _check_cancel()
        time.sleep(1)


def wait_for_device(timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_cancel()
        r = adb("devices")
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return True
        time.sleep(2)
    raise TimeoutError("Timed out waiting for an emulator device.")


def wait_for_boot(timeout=900):
    wait_for_device(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_cancel()
        r = adb("shell", "getprop", "sys.boot_completed")
        if r.stdout.strip() == "1":
            time.sleep(2)
            return True
        time.sleep(3)
    raise TimeoutError("Timed out waiting for the emulator to finish booting.")


def wait_for_no_device(timeout=120):
    """Poll until no emulator device is connected (after a shutdown)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_cancel()
        r = adb("devices")
        active = [ln for ln in r.stdout.splitlines()[1:]
                  if ln.split() and ln.split()[-1] in ("device", "offline")]
        if not active:
            return True
        time.sleep(2)
    return False


def wait_for_root(timeout=900):
    """Poll until 'su -c id' reports uid=0, allowing for a reboot in between.

    The first shell su triggers a Magisk Superuser prompt on the AVD that must
    be granted; we poll (re-prompting) and remind the user periodically.
    """
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        _check_cancel()
        try:
            wait_for_device(timeout=60)
            # Mirror exactly what works manually: `adb shell su -c id`.
            r = adb("shell", "su", "-c", "id")
            if "uid=0" in r.stdout:
                return True
        except TimeoutError:
            pass
        n += 1
        if n % 3 == 0:
            log("    ...still waiting for root. If a Magisk 'Superuser Request' "
                "is on the AVD, tap GRANT (tick 'Remember').")
        time.sleep(6)
    return False


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #
def ask_yes_no(prompt):
    """Prompt for y/n. Returns True for y, False for n."""
    while True:
        _check_cancel()
        try:
            with _print_lock:
                ans = input(f"{prompt} ").strip().lower()
        except EOFError:
            err(f"{prompt}  [no interactive input — aborting]")
            abort()
        if ans == "y":
            return True
        if ans == "n":
            return False
        log("    Please answer 'y' or 'n'.")


def pause(prompt, fallback_wait=12):
    """Wait for Enter. If stdin isn't interactive (EOFError), don't crash —
    print the prompt and continue after a short delay so the user can still act
    on the AVD."""
    try:
        input(prompt)
    except EOFError:
        log(prompt)
        log(f"    (no interactive stdin; continuing in {fallback_wait}s — get "
            "ready to patch fakeboot.img in Magisk on the AVD)")
        time.sleep(fallback_wait)


def abort():
    raise Cancel()


def winget_install(package_id):
    log(f"[*] Installing {package_id} via winget ...")
    r = run(["winget", "install", "-e", "--id", package_id,
             "--accept-source-agreements", "--accept-package-agreements"],
            capture=True)
    if r.returncode != 0:
        err(r.stdout)
        err(r.stderr)
        raise MissingTool(f"winget failed to install {package_id}.")


def download(name, url, dest_dir):
    """Download url -> dest_dir/name atomically (temp .part then os.replace)."""
    _check_cancel()
    final = dest_dir / name
    part = dest_dir / (name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "auto_root_avd"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as f:
        shutil.copyfileobj(resp, f)
    os.replace(part, final)
    log(f"[+] Downloaded {name} -> {final}")
    return final


# --------------------------------------------------------------------------- #
# Step 1 — Ensure tooling
# --------------------------------------------------------------------------- #
def _resolve_sdk_bat(sdk_root, name):
    """Find sdkmanager/avdmanager across known SDK layouts and PATH.

    Searches: cmdline-tools/latest/bin, cmdline-tools/<ver>/bin (newest first),
    legacy tools/bin, then PATH.
    """
    candidates = [sdk_root / "cmdline-tools" / "latest" / "bin" / f"{name}.bat"]
    versioned = sorted((sdk_root / "cmdline-tools").glob("*/bin"), reverse=True) \
        if (sdk_root / "cmdline-tools").exists() else []
    candidates += [d / f"{name}.bat" for d in versioned]
    candidates.append(sdk_root / "tools" / "bin" / f"{name}.bat")
    for c in candidates:
        if c.exists():
            return c
    which = shutil.which(name) or shutil.which(f"{name}.bat")
    return Path(which) if which else None


def _resolve_android_cli(sdk_root):
    """Find the new 'android' CLI (android.exe) that ships in cmdline-tools.

    Since 2025 the command-line tools deprecate sdkmanager in favour of this
    binary for installing/removing SDK packages; sdkmanager now just redirects
    to it. Searches cmdline-tools/latest/bin, cmdline-tools/<ver>/bin, then PATH.
    """
    candidates = [sdk_root / "cmdline-tools" / "latest" / "bin" / "android.exe"]
    versioned = sorted((sdk_root / "cmdline-tools").glob("*/bin"), reverse=True) \
        if (sdk_root / "cmdline-tools").exists() else []
    candidates += [d / "android.exe" for d in versioned]
    for c in candidates:
        if c.exists():
            return c
    which = shutil.which("android")
    return Path(which) if which else None


def _resolve_emulator(sdk_root):
    cand = sdk_root / "emulator" / "emulator.exe"
    if cand.exists():
        return cand
    which = shutil.which("emulator")
    return Path(which) if which else None


def _resolve_jdk():
    """Return a JDK 17+ home (prefer Android Studio's bundled JBR), or None.

    sdkmanager/avdmanager require JDK 17+. A stale Java 8 on PATH (or a JDK so
    new the tools' version check mis-parses it) makes them refuse to run, and on
    Windows they exit 0 anyway. The JBR shipped with Android Studio is a known
    good 17+ runtime, so we prefer it explicitly.
    """
    candidates = []
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Android Studio") as k:
                    path, _ = winreg.QueryValueEx(k, "Path")
                    candidates.append(Path(path) / "jbr")
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    lad = os.environ.get("LOCALAPPDATA", "")
    candidates += [
        Path(pf) / "Android" / "Android Studio" / "jbr",
        Path(lad) / "Programs" / "Android Studio" / "jbr",
    ]
    for c in candidates:
        if (c / "bin" / "java.exe").exists():
            return c
    jh = os.environ.get("JAVA_HOME")
    if jh and (Path(jh) / "bin" / "java.exe").exists():
        return Path(jh)
    return None


def resolve_sdk_tooling():
    """Resolve adb / SDK cmdline-tools / emulator / JDK and build RUN_ENV.

    Shared by ensure_tooling() and clean_avd(); no OpenSSL, no progress bar.
    """
    global ADB, SDK_ROOT, SDKMANAGER, AVDMANAGER, ANDROID_CLI, EMULATOR, RUN_ENV

    # --- Android Studio + adb (detect only) ---
    adb_path = shutil.which("adb")
    if not adb_path:
        cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" \
            / "platform-tools" / "adb.exe"
        if cand.exists():
            adb_path = str(cand)
    if not adb_path:
        err("[x] adb not found.")
        err("    Install Android Studio (it bundles the SDK + platform-tools/adb)")
        err("    from https://developer.android.com/studio, then re-run this script.")
        raise MissingTool("adb")
    ADB = adb_path
    log(f"[=] adb: {ADB}")

    # --- SDK tools for auto-provisioning (derive from adb's SDK root) ---
    sdk_root = Path(ADB).resolve().parent.parent
    SDK_ROOT = sdk_root
    SDKMANAGER = _resolve_sdk_bat(sdk_root, "sdkmanager")
    AVDMANAGER = _resolve_sdk_bat(sdk_root, "avdmanager")
    ANDROID_CLI = _resolve_android_cli(sdk_root)
    EMULATOR = _resolve_emulator(sdk_root)

    if not SDKMANAGER or not AVDMANAGER:
        err("[x] Android SDK Command-line Tools (sdkmanager / avdmanager) not found.")
        err(f"    SDK root: {sdk_root}")
        err("    Android Studio installs platform-tools (adb) and the emulator by")
        err("    default, but NOT the command-line tools. Install them once:")
        err("      Android Studio > Settings > Languages & Frameworks > Android SDK")
        err("        > SDK Tools tab > check 'Android SDK Command-line Tools (latest)'")
        err("        > Apply.   (Also tick 'Android Emulator' if missing.)")
        err("    Then re-run this script.")
        raise MissingTool("sdk-cmdline-tools")
    if not EMULATOR:
        err("[x] Android Emulator not found.")
        err(f"    SDK root: {sdk_root}")
        err("    In Android Studio's SDK Manager (SDK Tools tab) tick 'Android")
        err("    Emulator', Apply, then re-run.")
        raise MissingTool("emulator")
    log(f"[=] sdkmanager: {SDKMANAGER}")
    log(f"[=] avdmanager: {AVDMANAGER}")
    if ANDROID_CLI:
        log(f"[=] android CLI: {ANDROID_CLI}")
    log(f"[=] emulator:   {EMULATOR}")

    # --- JDK 17+ (sdkmanager/avdmanager require it; pin a known-good one) ---
    jdk = _resolve_jdk()
    if not jdk:
        err("[x] No JDK 17+ found for sdkmanager/avdmanager.")
        err("    These tools require JDK 17+ (a stale Java 8 on PATH won't do).")
        err("    Easiest: install Android Studio (it bundles a JDK at <Studio>\\jbr),")
        err("    or set JAVA_HOME to a JDK 17+. Then re-run.")
        raise MissingTool("jdk-17")
    log(f"[=] JDK (JAVA_HOME): {jdk}")

    # Pin a consistent environment for ALL SDK subprocesses:
    #  - JAVA_HOME + PATH so avd/sdkmanager use the good JDK (not Java 8 on PATH)
    #  - ANDROID_AVD_HOME so avdmanager (writer) and emulator (reader) agree,
    #    regardless of whether HOME is defined in the shell.
    #  - SKIP_JDK_VERSION_CHECK as a belt-and-suspenders for very-new JDKs.
    #  - ANDROID_HOME/ANDROID_SDK_ROOT so rootAVD resolves the SDK + system-image
    #    ramdisk path deterministically (else it guesses ~/Android/Sdk).
    avd_home = Path(AVD_HOME) if AVD_HOME else (Path.home() / ".android" / "avd")
    avd_home.mkdir(parents=True, exist_ok=True)
    RUN_ENV = os.environ.copy()
    RUN_ENV["JAVA_HOME"] = str(jdk)
    RUN_ENV["PATH"] = (str(jdk / "bin") + os.pathsep
                       + str(sdk_root / "platform-tools") + os.pathsep
                       + RUN_ENV.get("PATH", ""))
    RUN_ENV["ANDROID_AVD_HOME"] = str(avd_home)
    RUN_ENV["ANDROID_HOME"] = str(sdk_root)
    RUN_ENV["ANDROID_SDK_ROOT"] = str(sdk_root)
    RUN_ENV["SKIP_JDK_VERSION_CHECK"] = "1"
    log(f"[=] ANDROID_HOME: {sdk_root}")
    log(f"[=] AVD home: {avd_home}")


def ensure_tooling():
    global OPENSSL
    progress.start(1, "tooling")
    resolve_sdk_tooling()

    # --- OpenSSL (auto-install in autonomous mode, gated in --manual) ---
    openssl_path = shutil.which("openssl")
    if not openssl_path:
        git_ssl = Path(r"C:\Program Files\Git\usr\bin\openssl.exe")
        if git_ssl.exists():
            openssl_path = str(git_ssl)
    if not openssl_path:
        if MANUAL and not ask_yes_no("Install OpenSSL? (y/n)"):
            abort()
        winget_install(OPENSSL_WINGET_ID)
        openssl_path = shutil.which("openssl")
        if not openssl_path:
            # ShiningLight installs under Program Files; probe common locations.
            for base in (r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
                         r"C:\Program Files\OpenSSL\bin\openssl.exe"):
                if Path(base).exists():
                    openssl_path = base
                    break
    if not openssl_path:
        raise MissingTool("openssl (installed but not found on PATH; reopen shell)")
    OPENSSL = openssl_path
    log(f"[=] openssl: {OPENSSL}")

    progress.done()


# --------------------------------------------------------------------------- #
# Step 2 — Download root tooling
# --------------------------------------------------------------------------- #
def _find_existing_rootavd():
    """Return a rootAVD.bat path if one already exists on the system."""
    hits = list(TOOLING_DIR.glob("**/rootAVD.bat"))
    if hits:
        return hits[0]
    which = shutil.which("rootAVD.bat")
    return Path(which) if which else None


def download_tooling():
    global ROOTAVD_BAT
    progress.start(2, "download")
    TOOLING_DIR.mkdir(parents=True, exist_ok=True)

    # rootAVD: if rootAVD.bat exists anywhere, it's fully satisfied (no download).
    ROOTAVD_BAT = _find_existing_rootavd()
    if ROOTAVD_BAT:
        log(f"[=] rootAVD.bat found: {ROOTAVD_BAT} (skipping rootAVD download)")

    needed = {}
    for name, url in TOOLING_URLS.items():
        if name == "rootAVD.zip" and ROOTAVD_BAT:
            continue
        if (TOOLING_DIR / name).exists():
            continue
        needed[name] = url

    if not needed:
        log("[=] Root tooling already present, skipping download")
        progress.done()
        return

    if MANUAL and not ask_yes_no("Download Root tooling? (yes or no)"):
        abort()

    log(f"[*] Downloading {len(needed)} item(s) ...")
    errors = []
    with ThreadPoolExecutor(max_workers=len(needed)) as ex:
        futures = {ex.submit(download, n, u, TOOLING_DIR): n
                   for n, u in needed.items()}
        for fut in futures:
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                errors.append((futures[fut], e))
    if errors:
        for name, e in errors:
            err(f"[x] Failed to download {name}: {e}")
        raise MissingTool("root tooling download failed")

    progress.done()


# --------------------------------------------------------------------------- #
# Step 3 — Ensure Burp CA
# --------------------------------------------------------------------------- #
def ensure_burp_ca():
    progress.start(3, "burp-ca")
    dest = TOOLING_DIR / "burpca.der"

    # Autonomous: always pull from the running proxy so we bake in the CURRENT
    # CA. A rotated Burp CA reuses the same subject_hash filename (9a5ba575.0)
    # with different bytes, so a cached burpca.der would silently go stale.
    if not MANUAL:
        try:
            log(f"[*] Fetching Burp CA from {BURP_CERT_URL} ...")
            req = urllib.request.Request(BURP_CERT_URL,
                                         headers={"User-Agent": "auto_root_avd"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
            tmp = TOOLING_DIR / "burpca.der.part"
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            log("[+] Fetched current Burp CA from running proxy")
            progress.done()
            return
        except Exception as e:  # noqa: BLE001
            if dest.exists():
                log(f"[!] Could not reach Burp ({e}); using existing burpca.der.")
                progress.done()
                return
            log(f"[!] Could not reach Burp ({e}); falling back to manual drop.")

    if dest.exists():
        log("[=] Burp CA already present")
        progress.done()
        return

    alert(f"Place your Burp Suite CA (DER) at:\n    {dest}\n"
          f"(Burp: Proxy > Proxy settings > Import/export CA certificate > "
          f"Certificate in DER format)")
    while not dest.exists():
        _check_cancel()
        time.sleep(2)
    log("[+] Burp CA detected.")
    progress.done()


# --------------------------------------------------------------------------- #
# Step 4b — Cert prep (background thread)
# --------------------------------------------------------------------------- #
def prepare_burp_cert(cert_error):
    """Convert the Burp CA and bake it into adguardcert.zip. Sets BURP_HASH.

    Always regenerates <hash>.0 from the freshly fetched burpca.der when it is
    present, so a rotated Burp CA can't leave a stale cert behind: Burp's subject
    is constant ('PortSwigger'), so subject_hash_old stays the same (9a5ba575)
    even after the key changes — the filename is identical but the bytes differ.
    Only when no burpca.der exists do we fall back to reusing an existing
    <hash>.0. The cert is written into the zip whenever the baked bytes differ
    from the current ones (not merely when the entry name is missing).
    """
    global BURP_HASH
    try:
        burp_der = TOOLING_DIR / "burpca.der"
        cacert_pem = TOOLING_DIR / "cacert.pem"

        if burp_der.exists():
            # 1) DER -> PEM
            _check_cancel()
            r = run([OPENSSL, "x509", "-inform", "der", "-in", str(burp_der),
                     "-out", str(cacert_pem)])
            if r.returncode != 0:
                raise RuntimeError(f"openssl der->pem failed: {r.stderr.strip()}")

            # 2) subject_hash_old
            _check_cancel()
            r = run([OPENSSL, "x509", "-inform", "PEM", "-subject_hash_old",
                     "-noout", "-in", str(cacert_pem)])
            if r.returncode != 0 or not r.stdout.strip():
                raise RuntimeError(f"openssl hash failed: {r.stderr.strip()}")
            BURP_HASH = r.stdout.strip().splitlines()[0].strip()
            log(f"[*] Burp CA subject_hash_old = {BURP_HASH}")

            # 3) (over)write cacert.pem -> <hash>.0 (refresh any stale copy)
            _check_cancel()
            hashed = TOOLING_DIR / f"{BURP_HASH}.0"
            shutil.copyfile(cacert_pem, hashed)
        else:
            existing = sorted(TOOLING_DIR.glob("*.0"))
            if not existing:
                raise RuntimeError(
                    "no burpca.der and no <hash>.0 to bake into adguardcert.zip")
            hashed = existing[0]
            BURP_HASH = hashed.stem
            log(f"[=] No burpca.der; reusing existing CA hash file "
                f"{hashed.name} (skipping OpenSSL).")

        # 4) bake into adguardcert.zip — replace whenever the baked cert is
        #    missing OR its bytes differ from the current one.
        _check_cancel()
        zip_path = TOOLING_DIR / "adguardcert.zip"
        entry = f"system/etc/security/cacerts/{BURP_HASH}.0"
        if _zip_entry_bytes(zip_path, entry) == hashed.read_bytes():
            log(f"[=] adguardcert.zip already has the current {BURP_HASH}.0.")
        else:
            _inject_cert_into_zip(zip_path, hashed, BURP_HASH)

        # 5) done
        log("[+] Burp Suite certificate is ready to be imported!")
    except Exception as e:  # noqa: BLE001
        cert_error.append(e)


def _zip_entry_bytes(zip_path, entry):
    """Return the bytes of `entry` inside zip_path, or None if absent/unreadable."""
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            if entry in z.namelist():
                return z.read(entry)
    except Exception:  # noqa: BLE001
        pass
    return None


def _inject_cert_into_zip(zip_path, hashed_file, hash_name):
    """Rewrite zip_path, replacing any *.0 under system/etc/security/cacerts/
    with hashed_file (named <hash>.0). Atomic via temp + os.replace."""
    cacerts_dir = "system/etc/security/cacerts/"
    new_entry = f"{cacerts_dir}{hash_name}.0"
    cert_bytes = hashed_file.read_bytes()
    tmp_path = zip_path.with_suffix(".zip.tmp")

    with zipfile.ZipFile(zip_path, "r") as zin:
        infos = zin.infolist()
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in infos:
                nm = info.filename
                # Drop existing CA cert(s) in the cacerts dir.
                if nm.startswith(cacerts_dir) and nm.endswith(".0"):
                    continue
                zout.writestr(info, zin.read(nm))
            zout.writestr(new_entry, cert_bytes)
    os.replace(tmp_path, zip_path)
    log(f"[*] Injected {hash_name}.0 into {zip_path.name} ({new_entry})")


# --------------------------------------------------------------------------- #
# --clean : delete the AVD and installed system image (for repeat testing)
# --------------------------------------------------------------------------- #
def _default_system_image_package():
    return IMAGE_TMPL.format(lvl=API_LEVEL)


def _default_system_image_dir():
    return (SDK_ROOT / "system-images" / f"android-{API_LEVEL}"
            / "google_apis_playstore" / "x86_64")


def _path_is_under(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _prune_empty_system_image_dirs(image_dir):
    for parent in (image_dir.parent, image_dir.parent.parent):
        try:
            parent.rmdir()
            log(f"[+] Removed empty SDK directory: {parent.relative_to(SDK_ROOT)}")
        except OSError:
            pass


def _remove_default_system_image():
    """Clean-delete the SDK system image provisioned by this script."""
    image = _default_system_image_package()
    image_dir = _default_system_image_dir()
    sysimg_root = SDK_ROOT / "system-images"

    if not _path_is_under(image_dir, sysimg_root):
        raise MissingTool(f"Refusing to remove unexpected SDK path: {image_dir}")

    if not image_dir.exists():
        log(f"[=] SDK system image not present: {image}")
        _prune_empty_system_image_dirs(image_dir)
        return

    log(f"[*] Uninstalling SDK system image: {image}")
    r = _sdk_remove(image)
    if image_dir.exists():
        if r.returncode != 0:
            err(r.stdout)
            err(r.stderr)
        log(f"[*] Removing SDK image directory tree: "
            f"{image_dir.relative_to(SDK_ROOT)}")
        shutil.rmtree(image_dir)
    else:
        log(f"[+] Uninstalled SDK system image: {image}")

    if image_dir.exists():
        raise MissingTool(f"SDK system image still exists after clean: {image_dir}")
    log(f"[+] SDK system image removed: {image}")
    _prune_empty_system_image_dirs(image_dir)


def clean_avd():
    """Delete the AVD (reclaims its multi-GB userdata/snapshots) and restore EVERY
    rootAVD-patched image across the SDK, so the next root test starts clean. Works
    on any environment: it derives the SDK from adb and restores all *.backup files
    rootAVD created (any API level / image type / ABI). The default SDK system
    image installed by this script is clean-deleted too."""
    resolve_sdk_tooling()

    # Stop a running emulator first so files aren't locked.
    try:
        adb("emu", "kill")
    except Exception:  # noqa: BLE001
        pass
    wait_for_no_device(timeout=30)

    # Delete the AVD (removes the .avd folder + .ini → frees userdata/snapshots).
    if _avd_exists(AVD_NAME):
        log(f"[*] Deleting AVD '{AVD_NAME}' ...")
        r = bat(AVDMANAGER, "delete", "avd", "-n", AVD_NAME)
        if _avd_exists(AVD_NAME):
            err(r.stdout)
            err(r.stderr)
            err(f"[!] avdmanager couldn't delete '{AVD_NAME}'; removing files.")
        else:
            log(f"[+] Deleted AVD '{AVD_NAME}'.")
    else:
        log(f"[=] AVD '{AVD_NAME}' not present.")

    # Fallback / belt-and-suspenders: remove any leftover AVD files directly.
    avd_home = Path(RUN_ENV.get("ANDROID_AVD_HOME") or (Path.home() / ".android" / "avd"))
    for leftover in (avd_home / f"{AVD_NAME}.avd", avd_home / f"{AVD_NAME}.ini"):
        if leftover.exists():
            try:
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink()
                log(f"[+] Removed leftover {leftover.name}")
            except Exception:  # noqa: BLE001
                pass

    # Restore ALL rootAVD backups anywhere under the SDK's system-images
    # (rootAVD makes <file>.backup of ramdisk.img and kernel-ranchu when patching).
    restored = 0
    sysimg = SDK_ROOT / "system-images"
    if sysimg.exists():
        for bak in sysimg.rglob("*.backup"):
            orig = bak.with_suffix("")  # strip ".backup"
            try:
                shutil.copyfile(bak, orig)
                log(f"[+] Restored {orig.relative_to(SDK_ROOT)}")
                restored += 1
            except Exception as e:  # noqa: BLE001
                err(f"[!] Could not restore {orig}: {e}")
    if restored == 0:
        log("[=] No rootAVD .backup files found; images already stock.")

    _remove_default_system_image()

    log("[+] Clean complete (AVD and API SDK system image clean-deleted).")


# --------------------------------------------------------------------------- #
# Step 4a — Rooting (main thread)
# --------------------------------------------------------------------------- #
def _avd_exists(name):
    r = run([str(EMULATOR), "-list-avds"])
    return name in [ln.strip() for ln in r.stdout.splitlines()]


def _avd_dir():
    """Path to the AVD's .avd folder (honoring the .ini 'path=' if present)."""
    avd_home = Path(RUN_ENV.get("ANDROID_AVD_HOME")
                    or (Path.home() / ".android" / "avd"))
    ini = avd_home / f"{AVD_NAME}.ini"
    if ini.exists():
        for line in ini.read_text(errors="replace").splitlines():
            if line.lower().startswith("path="):
                p = Path(line.split("=", 1)[1].strip())
                if p.exists():
                    return p
    return avd_home / f"{AVD_NAME}.avd"


def _set_avd_config(updates):
    """Set/overwrite key=value pairs in the AVD's config.ini.

    A value of None removes the key entirely (used to drop a stray
    disk.dataPartition.path=<temp>, which makes /data ephemeral).
    """
    cfg = _avd_dir() / "config.ini"
    lines = cfg.read_text(errors="replace").splitlines() if cfg.exists() else []
    out = [ln for ln in lines
           if "=" not in ln or ln.split("=", 1)[0].strip() not in updates]
    out += [f"{k}={v}" for k, v in updates.items() if v is not None]
    try:
        cfg.write_text("\n".join(out) + "\n")
    except Exception as e:  # noqa: BLE001
        err(f"[!] Could not update {cfg}: {e}")


def _sdk_install(image):
    """Install an SDK package (e.g. a system image), preferring the new 'android'
    CLI and falling back to the legacy sdkmanager.

    Command-line tools shipped since 2025 deprecate sdkmanager in favour of a new
    'android' binary: `sdkmanager --install "system-images;android-35;..."` now
    only prints 'Package ... not found' and redirects. The new CLI takes
    slash-separated coordinates instead, e.g.
    `android sdk install system-images/android-35/google_apis_playstore/x86_64`.
    Output streams live because the image download is large and slow.
    """
    if ANDROID_CLI and ANDROID_CLI.exists():
        pkg = image.replace(";", "/")
        log(f"[*] Installing system image via android CLI: {pkg}")
        # --no-metrics skips the first-run analytics prompt; the piped y's accept
        # the SDK license on a machine where it hasn't been accepted yet (a no-op
        # when Android Studio already accepted it).
        return run([str(ANDROID_CLI), "sdk", "install", pkg, "--no-metrics"],
                   input_data="y\n" * 20, capture=False)
    log("[*] Accepting SDK licenses ...")
    bat(SDKMANAGER, "--licenses", input_data="y\n" * 30)
    log(f"[*] Installing system image via sdkmanager: {image}")
    return bat(SDKMANAGER, "--install", image, input_data="y\n" * 10)


def _sdk_remove(image):
    """Remove an SDK package, preferring the new 'android' CLI (slash-separated
    coordinates) and falling back to legacy sdkmanager --uninstall."""
    if ANDROID_CLI and ANDROID_CLI.exists():
        pkg = image.replace(";", "/")
        return run([str(ANDROID_CLI), "sdk", "remove", pkg, "--no-metrics"],
                   input_data="y\n" * 5)
    return bat(SDKMANAGER, "--uninstall", image, input_data="y\n" * 5)


def provision_avd():
    """Install image, create the default AVD (if needed) and launch it."""
    global EMULATOR_PROC
    image = _default_system_image_package()
    image_dir = _default_system_image_dir()

    if image_dir.exists():
        log(f"[=] System image already installed, skipping install: {image}")
    else:
        r = _sdk_install(image)
        # The installer can exit non-zero on Windows even when the download
        # succeeded, so trust whether the image actually landed on disk rather
        # than the exit code. On failure the streamed output above shows why.
        if not image_dir.exists():
            raise MissingTool(
                f"failed to install system image {image} — see output above.")
        if r.returncode != 0:
            log(f"[!] installer exit code {r.returncode}, but the image is "
                f"present — continuing.")

    if _avd_exists(AVD_NAME):
        log(f"[=] AVD '{AVD_NAME}' already exists, reusing it.")
    else:
        log(f"[*] Creating AVD '{AVD_NAME}' ({DEVICE_PROFILE}, API {API_LEVEL}) ...")
        r = bat(AVDMANAGER, "create", "avd", "-n", AVD_NAME, "-k", image,
                "-d", DEVICE_PROFILE, input_data="no\n")
        # avdmanager can exit 0 even when it refuses to run (e.g. JDK check),
        # so verify the AVD actually exists rather than trusting returncode.
        if not _avd_exists(AVD_NAME):
            err(r.stdout)
            err(r.stderr)
            raise MissingTool(
                f"avdmanager did not create '{AVD_NAME}' (see output above).")
        log(f"[+] AVD '{AVD_NAME}' created.")

    # Apply config.ini settings before launch (read at boot). hw.keyboard=yes is
    # always set (avdmanager AVDs default to hw.keyboard=no → host keyboard
    # ignored); RAM / data-partition / sdcard come from avd_config.ini when set.
    cfg = {
        "hw.keyboard": "yes",
        # Persistence: a stray 'disk.dataPartition.path=<temp>' (which some
        # avdmanager/emulator combos write) makes /data ephemeral — every power
        # cycle wipes the Magisk app, its su grants and /data/adb modules, even
        # though the ramdisk su binary survives on the SDK image. Drop it so the
        # emulator uses the persistent userdata-qemu.img.
        "disk.dataPartition.path": None,
        # Force a COLD boot every launch so the rootAVD-patched ramdisk (Magisk
        # su) reloads; a fast/quick boot restores a snapshot and would not.
        "fastboot.forceColdBoot": "yes",
        "fastboot.forceFastBoot": "no",
    }
    if RAM_MB:
        cfg["hw.ramSize"] = str(RAM_MB)
    if STORAGE:
        cfg["disk.dataPartition.size"] = _normalize_size(STORAGE)
    if SDCARD:
        cfg["hw.sdCard"] = "yes"
        cfg["sdcard.size"] = _normalize_size(SDCARD)
    _set_avd_config(cfg)
    log("[=] AVD config applied: "
        + ", ".join(f"{k}={v}" for k, v in cfg.items() if v is not None)
        + ", disk.dataPartition.path=removed")

    log(f"[*] Launching emulator '{AVD_NAME}' ...")
    EMULATOR_PROC = subprocess.Popen([str(EMULATOR), "-avd", AVD_NAME,
                                      "-no-snapshot"], env=RUN_ENV,
                                     stdin=subprocess.DEVNULL)


def cold_boot_avd():
    """Cold-boot the AVD so a freshly patched ramdisk is loaded.

    rootAVD powers the AVD off at the end of FAKEBOOTIMG and expects a COLD
    boot (a soft `adb reboot` won't reload ramdisk.img). If we own the emulator
    process we kill + relaunch it; otherwise we ask the user to 'Cold Boot Now'.
    """
    global EMULATOR_PROC
    log("[*] Cold-booting the AVD to load the patched ramdisk ...")
    # Tell any running instance to quit, then make sure it's gone.
    try:
        adb("emu", "kill")
    except Exception:  # noqa: BLE001
        pass
    if EMULATOR_PROC is not None:
        try:
            EMULATOR_PROC.terminate()
            EMULATOR_PROC.wait(timeout=30)
        except Exception:  # noqa: BLE001
            pass
    wait_for_no_device(timeout=60)

    if (not MANUAL) or CREATE_AVD:
        time.sleep(2)
        EMULATOR_PROC = subprocess.Popen(
            [str(EMULATOR), "-avd", AVD_NAME, "-no-snapshot", "-no-snapshot-load"],
            env=RUN_ENV, stdin=subprocess.DEVNULL)
    else:
        alert("rootAVD powered the AVD off. COLD BOOT it now:\n"
              "  Device Manager > (your AVD) > dropdown > 'Cold Boot Now'\n"
              "  (a normal start/reboot will NOT load the patched ramdisk)")
    wait_for_boot()
    log("[+] AVD cold-booted.")


def _magisk_app_installed():
    r = adb("shell", "pm", "list", "packages", "com.topjohnwu.magisk")
    return "com.topjohnwu.magisk" in (r.stdout or "")


def _find_magisk_apk():
    """Locate the Magisk app APK shipped with rootAVD (Apps/Magisk.apk)."""
    bases = []
    if ROOTAVD_BAT:
        bases.append(Path(ROOTAVD_BAT).parent)
    bases.append(TOOLING_DIR)
    for base in bases:
        cand = base / "Apps" / "Magisk.apk"
        if cand.exists():
            return cand
        hits = list(base.glob("**/Magisk.apk"))
        if hits:
            return hits[0]
    return None


def ensure_magisk_app():
    """Reinstall the Magisk app if it vanished after the FAKEBOOTING cold boot.

    rootAVD uses a *temporary* Magisk app to patch the fake boot image; on the
    cold boot that temp app removes itself, so com.topjohnwu.magisk is gone even
    though root works (the su binary in the patched ramdisk is present — `su`
    returns 'Permission denied', not 'not found'). Reinstall the real app from
    rootAVD's Apps/Magisk.apk so its one-time 'Additional Setup' can be done.
    """
    if _magisk_app_installed():
        return
    apk = _find_magisk_apk()
    if not apk:
        err("[!] Magisk app is missing and Apps/Magisk.apk was not found; "
            "install Magisk manually on the AVD to finish setup.")
        return
    log(f"[*] Magisk app missing after cold boot; reinstalling {apk.name} ...")
    r = adb("install", "-r", str(apk))
    if "Success" in (r.stdout or "") or _magisk_app_installed():
        log("[+] Magisk app reinstalled.")
    else:
        err(r.stdout)
        err(r.stderr)
        err(f"[!] Could not reinstall the Magisk app automatically; install "
            f"{apk} manually on the AVD.")


def do_rooting():
    """Provision/await the AVD, run rootAVD FAKEBOOTING, wait for root."""
    global ROOTAVD_BAT

    # 1) Locate rootAVD.bat (reuse if found, else extract rootAVD.zip)
    if not ROOTAVD_BAT:
        log("[*] Extracting rootAVD.zip ...")
        with zipfile.ZipFile(TOOLING_DIR / "rootAVD.zip", "r") as z:
            z.extractall(TOOLING_DIR)
        ROOTAVD_BAT = _find_existing_rootavd()
    if not ROOTAVD_BAT or not ROOTAVD_BAT.exists():
        raise MissingTool("rootAVD.bat could not be located after extraction.")
    log(f"[=] Using rootAVD: {ROOTAVD_BAT}")

    # 2) Get an AVD running
    if (not MANUAL) or CREATE_AVD:
        provision_avd()
    else:
        alert(f"Create and open a Google Play AVD (recommended: {DEVICE_PROFILE}, "
              f"API {API_LEVEL}). Waiting for the emulator to start ...")
    wait_for_boot()
    log("[+] Emulator booted.")

    # 3) Determine the running AVD's API level
    r = adb("shell", "getprop", "ro.build.version.sdk")
    level = r.stdout.strip()
    if not level.isdigit():
        raise MissingTool(f"Could not read AVD API level (got {level!r}).")
    log(f"[=] Running AVD API level: {level}")

    # 4) Fast path — if the AVD is already rooted (e.g. a re-run on a previously
    #    rooted AVD), skip rootAVD / FAKEBOOTIMG / cold boot entirely.
    if is_rooted():
        log("[=] Device already rooted (su -c id → uid=0); skipping rootAVD.")
        root_ok = True
    else:
        # 4a) Confirm rootAVD lists a Play image for this level
        r = bat(ROOTAVD_BAT, "ListAllAVDs")
        if f"android-{level}" not in r.stdout:
            log(f"[!] rootAVD ListAllAVDs did not list android-{level}; "
                f"continuing with the standard path anyway.")

        # 5) Pre-instruct the (inherent) manual step, then run rootAVD FAKEBOOTIMG.
        #    rootAVD creates /sdcard/Download/fakeboot.img, AUTO-LAUNCHES Magisk and
        #    waits ~60s for you to patch it, then flashes the ramdisk and reboots.
        #    The instruction MUST come first because the 60s window is inside the run.
        ramdisk = (f"system-images\\android-{level}\\google_apis_playstore\\"
                   f"x86_64\\ramdisk.img")
        alert("MANUAL STEP — rootAVD will now create /sdcard/Download/fakeboot.img\n"
              "and AUTO-LAUNCH the Magisk app on the AVD. When Magisk opens (~60s):\n"
              "    Install  >  Select and Patch a File  >\n"
              "    /sdcard/Download/fakeboot.img  >  Let's Go\n"
              "rootAVD then flashes the patched ramdisk and reboots the AVD.")
        pause("Press Enter to start rootAVD FAKEBOOTIMG ... ", fallback_wait=180)

        log(f"[*] Running rootAVD FAKEBOOTIMG: {ramdisk}")
        bat(ROOTAVD_BAT, ramdisk, "FAKEBOOTIMG", capture=False)  # live output

        # 6) rootAVD powered the AVD off; cold-boot it so the patched ramdisk loads
        #    (a soft reboot would not reload ramdisk.img).
        cold_boot_avd()

        # 7) Finalize Magisk. The patched ramdisk loads on cold boot, but Magisk's
        #    su/daemon need a one-time "Additional Setup" that writes its env to
        #    /data/adb/magisk (rootAVD's temporary-Magisk removal wipes it). Until
        #    that's done + a reboot, `su` returns Permission denied. The temp
        #    Magisk app is also gone after the cold boot, so reinstall it first,
        #    then bring it to the foreground and walk the user through setup.
        ensure_magisk_app()
        try:
            adb("shell", "monkey", "-p", "com.topjohnwu.magisk",
                "-c", "android.intent.category.LAUNCHER", "1")
        except Exception:  # noqa: BLE001
            pass
        alert("FINALIZE ROOT on the AVD — open the Magisk app, then:\n"
              "  1) If it says 'Requires Additional Setup' -> tap it ->\n"
              "     Method: DIRECT INSTALL -> let the AVD REBOOT when it offers to.\n"
              "  2) After the reboot, when a Magisk 'Superuser Request' for 'shell'\n"
              "     appears, tap GRANT and tick 'Remember choice' (Forever).\n"
              "The script keeps polling and continues once 'su' works.")

        # 8) Verify root (polls through the Additional-Setup reboot)
        log("[*] Waiting for root to come up ...")
        root_ok = wait_for_root(timeout=1800)

    # 9) On success, make shell root permanent/silent; else print guidance.
    if root_ok:
        log("[+] Root confirmed (uid=0).")
        try:
            su('magisk --sqlite "REPLACE INTO policies '
               '(uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"')
        except Exception:  # noqa: BLE001
            pass
    else:
        err("[x] Root was not confirmed within the timeout.")
        err("    Most common cause: Magisk still needs 'Additional Setup'.")
        err("    On the AVD open Magisk > tap 'Requires Additional Setup' >")
        err("    Direct Install > reboot, then Grant the 'shell' Superuser prompt.")
        err("    (Other causes: missed the ~60s fakeboot window.) Re-run if needed.")
    return root_ok


# --------------------------------------------------------------------------- #
# Step 5 — Module install
# --------------------------------------------------------------------------- #
def _magisk_version():
    """Return Magisk's version string via the daemon, or '' if not ready."""
    r = su("magisk -c")
    out = r.stdout.strip()
    return out if out and "Permission denied" not in out and "not found" not in out \
        else ""


def _list_modules():
    """List installed Magisk module ids under /data/adb/modules."""
    r = su("ls -1 /data/adb/modules 2>/dev/null")
    return [ln.strip() for ln in r.stdout.splitlines()
            if ln.strip() and "No such" not in ln and "Permission" not in ln]


def ensure_magisk_ready(max_reboots=3):
    """Magisk on the emulator often needs an extra reboot or two after first-time
    setup before its daemon will install modules. Reboot + re-confirm until
    `magisk -c` responds, so we never install while Magisk is mid-setup."""
    for attempt in range(max_reboots + 1):
        ver = _magisk_version()
        if ver:
            log(f"[=] Magisk daemon ready: {ver}")
            return True
        if attempt == max_reboots:
            break
        log(f"[*] Magisk not fully set up yet; rebooting to settle "
            f"(attempt {attempt + 1}/{max_reboots}) ...")
        adb("reboot")
        wait_for_boot()
        wait_for_root(timeout=600)
    return False


def install_modules(root_confirmed):
    progress.start(5, "modules")
    if not root_confirmed:
        err("[x] Root not confirmed in previous steps; cannot install modules.")
        abort()

    # 0) Make sure Magisk is fully set up before touching modules. After the first
    #    root, Magisk frequently needs another reboot ('Additional Setup') — if we
    #    install during that window the modules silently don't take.
    if not ensure_magisk_ready():
        err("[x] Magisk still isn't fully set up (it keeps asking to reboot /")
        err("    'Requires Additional Setup'). On the AVD: open Magisk > finish")
        err("    setup (Direct Install) > reboot until it's stable, then re-run.")
        abort()

    # 0b) Slow down right after first root — Magisk (and any network the module
    #     installers use) can need a moment to settle before installs are reliable.
    log(f"[*] Letting Magisk settle for {MAGISK_SETTLE_SEC}s before installing ...")
    _interruptible_sleep(MAGISK_SETTLE_SEC)

    # 1) Grant Magisk shell root so installs run unattended (autonomous only)
    if not MANUAL:
        log("[*] Granting Magisk root to shell (uid 2000) ...")
        su('magisk --sqlite "REPLACE INTO policies '
           '(uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)"')

    # 2) Turn Wi-Fi / data OFF before importing modules. Modules are pushed
    #    locally with `adb push` (not downloaded on-device), so no internet is
    #    needed — the earlier "fails to install" was Magisk not being ready, which
    #    the settle + retries above/below handle. Networking is re-enabled at the end.
    if MANUAL:
        ask_yes_no("Turn off Wi-Fi on the AVD, then continue? (y/n)")
    else:
        log("[*] Disabling Wi-Fi / mobile data ...")
        adb("shell", "svc", "wifi", "disable")
        adb("shell", "svc", "data", "disable")

    # 3) Push modules to /sdcard/Download/ (retry a couple of times)
    for name in MODULE_INSTALL_ORDER:
        local = TOOLING_DIR / name
        pushed = False
        for attempt in range(1, MODULE_RETRIES + 1):
            log(f"[*] Pushing {name} (attempt {attempt}/{MODULE_RETRIES}) ...")
            r = adb("push", str(local), "/sdcard/Download/")
            if r.returncode == 0:
                pushed = True
                break
            if r.stderr.strip():
                err(r.stderr.strip())
            _interruptible_sleep(MODULE_RETRY_DELAY)
        if not pushed:
            raise MissingTool(f"adb push failed for {name}")

    # 4) Install each module (framework -> module -> cert), retrying and verifying
    #    it actually lands in /data/adb/modules.
    for name in MODULE_INSTALL_ORDER:
        installed = False
        for attempt in range(1, MODULE_RETRIES + 1):
            before = set(_list_modules())
            log(f"[*] Installing {name} via Magisk "
                f"(attempt {attempt}/{MODULE_RETRIES}) ...")
            r = su(f"magisk --install-module /sdcard/Download/{name}")
            if r.stdout.strip():
                log(r.stdout.strip())
            if r.stderr.strip():
                log(r.stderr.strip())
            # Success = magisk reported ok, or a new module id appeared.
            if r.returncode == 0 or len(_list_modules()) > len(before):
                installed = True
                break
            log(f"[!] {name} didn't install; retrying in {MODULE_RETRY_DELAY}s ...")
            _interruptible_sleep(MODULE_RETRY_DELAY)
        if not installed:
            err(f"[!] {name} failed to install after {MODULE_RETRIES} attempts.")

    mods = _list_modules()
    log(f"[=] Module ids now present: {sorted(mods) or '(none)'}")
    if not mods:
        err("[!] No modules detected under /data/adb/modules after install.")

    # 5) Reboot to activate the modules, then re-confirm root + verify.
    log("[*] Rebooting the AVD to activate modules ...")
    adb("reboot")
    wait_for_boot()
    wait_for_root(timeout=600)
    final_mods = _list_modules()
    log(f"[=] Active modules after reboot: {final_mods or '(none)'}")
    if not final_mods:
        err("[!] Modules still not present. You can install them manually in the")
        err("    Magisk app: Modules > Install from storage > /sdcard/Download/")
        err("    (ZygiskNext.zip, NoHello.zip, adguardcert.zip).")
    log("[+] AVD back up after module install.")
    progress.done()


# --------------------------------------------------------------------------- #
# Step 6 — Verification + network/proxy + final reboot
# --------------------------------------------------------------------------- #
def verify_and_finalize():
    progress.start(6, "verify")

    # 1) Root
    r = su("id")
    if "uid=0" in r.stdout:
        log(f"[+] Root OK: {r.stdout.strip()}")
    else:
        err(f"[x] Root verification failed: {r.stdout.strip()} {r.stderr.strip()}")

    # 2) Burp CA mounted in system store
    if BURP_HASH:
        r = su(f"ls /system/etc/security/cacerts/{BURP_HASH}.0")
        if f"{BURP_HASH}.0" in r.stdout and "No such" not in r.stdout:
            log(f"[+] Burp CA present in system store: {BURP_HASH}.0")
        else:
            err(f"[x] Burp CA not found in system store: "
                f"{r.stdout.strip()} {r.stderr.strip()}")

    # 3) Final reboot
    log("[*] Final reboot ...")
    adb("reboot")
    wait_for_boot()

    # 4) Network back on (proxy is left for the user to set manually in the AVD's
    #    Settings > Proxy > Manual — see the closing message).
    if not MANUAL:
        log("[*] Enabling network ...")
        adb("shell", "svc", "wifi", "enable")
        adb("shell", "svc", "data", "enable")

    # 5) Mark the device provisioned / setup complete so the AVD is ready to use
    #    (skips the setup wizard). No root needed; persists in /data.
    log("[*] Marking device provisioned (skipping setup wizard) ...")
    adb("shell", "settings", "put", "global", "device_provisioned", "1")
    adb("shell", "settings", "put", "secure", "user_setup_complete", "1")

    # 6) Final message
    log("")
    log(FINAL_MSG)
    progress.done()


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #
def cleanup():
    """Idempotent teardown: kill children, join cert thread, remove temp files."""
    CANCEL.set()
    with _procs_lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    if EMULATOR_PROC is not None:
        try:
            EMULATOR_PROC.terminate()
        except Exception:  # noqa: BLE001
            pass
    # Remove partial / temp artifacts.
    try:
        for pattern in ("*.part", "*.tmp", "*.zip.tmp", "burpca.der.part"):
            for f in TOOLING_DIR.glob(pattern):
                try:
                    f.unlink()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


def _sigint(signum, frame):
    CANCEL.set()
    raise KeyboardInterrupt


# --------------------------------------------------------------------------- #
# AVD configuration file (avd_config.ini)
# --------------------------------------------------------------------------- #
_CONFIG_TEMPLATE = """\
# AVD configuration for auto_root_avd.py
# The script reads these to create the emulator. Blank/omitted values use the
# built-in defaults or the system image's own default. Lines starting with # are
# comments.

[avd]
# Google Play system image API level (needs a google_apis_playstore x86_64 image)
api = 35

# avdmanager hardware profile (list options with: avdmanager list device)
device = pixel_8

# AVD name
name = auto_root_avd

# RAM in MB, e.g. 4096. Blank = image default.
ram_mb =

# Internal / data-partition size, e.g. 8G or 8192M. Blank = image default.
storage =

# SD card size, e.g. 512M. Blank = none.
sdcard =

# Where to store the AVD (.avd userdata/snapshots), e.g. E:\\AVDs .
# Blank = Android default (C:\\Users\\<you>\\.android\\avd).
avd_path =
"""


def _valid_size(s):
    """True if s is empty or a size like 8G / 8192M / 8192 (bare = MB)."""
    s = str(s).strip()
    if not s:
        return True
    if s[-1].upper() in ("K", "M", "G"):
        s = s[:-1]
    return s.isdigit() and int(s) > 0


def _normalize_size(s):
    """Normalize a size: '8G'->'8G', bare '8192'->'8192M', suffix upper-cased."""
    s = str(s).strip()
    if not s:
        return s
    if s[-1].upper() in ("K", "M", "G"):
        return s[:-1] + s[-1].upper()
    return s + "M" if s.isdigit() else s


def load_avd_config(path):
    """Load AVD settings from an INI file's [avd] section. Returns a dict with
    keys name/api/device/ram_mb/storage/sdcard/avd_path. Writes a commented
    template (with defaults) if the file is missing. Invalid values → exit."""
    defaults = {"name": AVD_NAME, "api": str(DEFAULT_API), "device": DEVICE,
                "ram_mb": "", "storage": "", "sdcard": "", "avd_path": ""}
    raw = dict(defaults)

    if not path.exists():
        try:
            path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
            log(f"[=] Wrote AVD config template: {path}")
        except Exception as e:  # noqa: BLE001
            err(f"[!] Could not write config template {path}: {e}")
    else:
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except configparser.Error as e:
            err(f"[x] Could not parse {path}: {e}")
            sys.exit(1)
        if parser.has_section("avd"):
            for k in defaults:
                if parser.has_option("avd", k):
                    raw[k] = (parser.get("avd", k) or "").strip()
        log(f"[=] Loaded AVD config: {path}")

    conf = {}
    conf["name"] = raw["name"].strip() or AVD_NAME
    conf["device"] = raw["device"].strip() or DEVICE

    api_s = raw["api"].strip() or str(DEFAULT_API)
    if not api_s.isdigit():
        err(f"[x] config: 'api' must be an integer (got {api_s!r}).")
        sys.exit(1)
    conf["api"] = int(api_s)

    ram_s = raw["ram_mb"].strip()
    if ram_s and (not ram_s.isdigit() or int(ram_s) <= 0):
        err(f"[x] config: 'ram_mb' must be a positive integer MB (got {ram_s!r}).")
        sys.exit(1)
    conf["ram_mb"] = int(ram_s) if ram_s else None

    for key in ("storage", "sdcard"):
        val = raw[key].strip()
        if val and not _valid_size(val):
            err(f"[x] config: '{key}' must be like 8G / 8192M / 8192 (got {val!r}).")
            sys.exit(1)
        conf[key] = val or None

    conf["avd_path"] = raw["avd_path"].strip() or None
    return conf


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args():
    p = argparse.ArgumentParser(
        description="Auto-root an Android emulator and mount the Burp CA for "
                    "TLS interception.")
    p.add_argument("--manual", action="store_true",
                   help="Interactive mode: restore y/n prompts and "
                        "wait-for-user steps (default is fully autonomous).")
    p.add_argument("--create-avd", action="store_true", dest="create_avd",
                   help="In --manual mode, create the default AVD instead of "
                        "waiting for the user to create one.")
    p.add_argument("--clean", action="store_true",
                   help="Delete the AVD (frees its GBs of userdata/snapshots) and "
                        "clean-delete the configured API's SDK system image, then "
                        "exit. Use between full root re-tests.")
    p.add_argument("--config", default=None,
                   help="Path to the AVD config INI (default: avd_config.ini next "
                        "to the script; auto-created with a template if missing).")
    return p.parse_args()


def main():
    global MANUAL, CREATE_AVD, AVD_NAME, API_LEVEL, DEVICE_PROFILE
    global RAM_MB, STORAGE, SDCARD, AVD_HOME
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    _enable_windows_ansi()

    args = parse_args()
    MANUAL = args.manual
    CREATE_AVD = args.create_avd
    signal.signal(signal.SIGINT, _sigint)

    # Load AVD settings from the config file (creates a template if missing).
    conf = load_avd_config(Path(args.config) if args.config else CONFIG_PATH)
    AVD_NAME = conf["name"]
    API_LEVEL = conf["api"]
    DEVICE_PROFILE = conf["device"]
    RAM_MB = conf["ram_mb"]
    STORAGE = conf["storage"]
    SDCARD = conf["sdcard"]
    AVD_HOME = conf["avd_path"]
    log(f"    AVD: name={AVD_NAME} api={API_LEVEL} device={DEVICE_PROFILE} "
        f"ram={RAM_MB or 'default'} storage={STORAGE or 'default'} "
        f"sdcard={SDCARD or 'none'} path={AVD_HOME or 'default (C:)'}")

    if args.clean:
        log("=== auto_root_avd === mode: CLEAN")
        try:
            clean_avd()
        except (KeyboardInterrupt, Cancel):
            cleanup()
            log("")
            log(CANCEL_MSG)
            sys.exit(1)
        except MissingTool as e:
            err(f"[x] {e}")
            sys.exit(1)
        except Exception as e:  # noqa: BLE001
            err(f"[x] Unexpected error: {e}")
            sys.exit(1)
        return

    log(f"=== auto_root_avd === mode: {'MANUAL' if MANUAL else 'AUTONOMOUS'}")
    log(f"    tooling dir: {TOOLING_DIR}")

    try:
        ensure_tooling()
        download_tooling()
        ensure_burp_ca()

        # Step 4: cert prep (background) || rooting (main)
        progress.start(4, "root+cert")
        cert_error = []
        cert_thread = threading.Thread(target=prepare_burp_cert,
                                       args=(cert_error,), daemon=True)
        cert_thread.start()
        root_ok = do_rooting()
        cert_thread.join()
        if cert_error:
            raise cert_error[0]
        progress.done()

        install_modules(root_ok)
        verify_and_finalize()

        log("\n[+] All done.")
    except (KeyboardInterrupt, Cancel):
        cleanup()
        log("")
        log(CANCEL_MSG)
        sys.exit(1)
    except MissingTool as e:
        err(f"[x] {e}")
        cleanup()
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        err(f"[x] Unexpected error: {e}")
        cleanup()
        sys.exit(1)


if __name__ == "__main__":
    main()
