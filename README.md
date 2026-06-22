# Auto-Root AVD + Burp TLS Interception

`auto_root_avd.py` automates rooting a **Google Play** Android emulator (AVD) with
**Magisk** (via [rootAVD](https://gitlab.com/newbit/rootAVD)) and wiring it up for
**Burp Suite** TLS interception. It installs the `ZygiskNext`, `NoHello`, and
`AdguardCert` Magisk modules, where the AdGuard module is repurposed to mount your
**Burp CA** into the system trust store so Burp can intercept all HTTPS traffic on
the emulator.

> Platform: **Windows**. Everything runs against an emulator you control, for app
> analysis / security testing.

---

## Requirements

- **Android Studio** with the SDK — must include **platform-tools** (`adb`),
  **Android SDK Command-line Tools** (`sdkmanager`, `avdmanager`), and the
  **Android Emulator**. Install the command-line tools via
  *Android Studio → SDK Manager → SDK Tools → "Android SDK Command-line Tools"*.
- **JDK 17+** — auto-detected from Android Studio's bundled JBR
  (`<Android Studio>\jbr`); a stale Java 8 on `PATH` is **not** enough.
- **OpenSSL** — auto-installed via `winget` if missing.
- **Burp Suite** running with its proxy on `127.0.0.1:8080` (the script can fetch
  the CA automatically), or export the CA yourself as `root_tooling\burpca.der`.

The script detects the SDK from `adb` and pins a consistent environment
(`JAVA_HOME`, `ANDROID_HOME`, `ANDROID_AVD_HOME`) for all SDK calls, so it adapts
to wherever your SDK lives.

---

## Usage

```powershell
python "auto_root_avd.py" [--manual] [--create-avd] [--clean]
python "auto_root_avd.py" -h        # full help
```

See `-h` for the authoritative list of options. Summary:

| Flag | What it does |
|------|--------------|
| *(none)* | **Autonomous** (default): installs tooling, downloads root tooling, fetches the Burp CA, provisions + launches the AVD, runs rootAVD, installs modules, verifies. Minimal prompts. |
| `--manual` | Interactive mode: restores the `y/n` prompts and waits for **you** to create/open the AVD (does **not** auto-create one unless `--create-avd` is also given). |
| `--create-avd` | In `--manual` mode, create the default AVD instead of waiting for you. |
| `--clean` | Delete the AVD (frees its multi-GB userdata/snapshots), restore any patched stock ramdisk(s), clean-delete the API 35 SDK system image, then exit. Use **between full re-tests**. |

### Typical loop

```powershell
python auto_root_avd.py --clean     # clean-delete the AVD and API 35 system image
python auto_root_avd.py             # root from scratch
```

---

## ⚠️ Expect to run it a couple of times

Magisk on the emulator usually needs **two or three reboots** before root is fully
set up. During a run you will have to do a few **on-device** steps that can't be
automated, and if you miss a timing window the script will tell you to re-run:

1. **Patch fakeboot.img** — when rootAVD auto-launches Magisk (~60 s window):
   *Install → Select and Patch a File → `/sdcard/Download/fakeboot.img` → Let's Go.*
2. **Requires Additional Setup** — after the cold boot, open the Magisk app; if it
   says *Requires Additional Setup* → **Direct Install** → let it **reboot**
   (repeat if it asks again). This is what enables `su`/`magiskd`.
3. **Grant Superuser** — when the script checks root, tap **Grant** on the Magisk
   *Superuser Request* for `shell` and tick **Remember**.

The script waits/polls through these and re-confirms root before installing
modules, but if Magisk is still mid-setup or a window was missed, just run it
again — it's idempotent (skips downloads / cert / image when already present and
reuses the AVD).

---

## Current limitations

- **API level 35 only.** The script provisions a fixed default AVD:
  **Pixel 8, API 35** (`system-images;android-35;google_apis_playstore;x86_64`).
  Other API levels are not yet supported by the automated path.
- **One inherent manual step:** patching `fakeboot.img` in Magisk (rootAVD's
  FAKEBOOTIMG method requires the Magisk UI on Play Store images).
- **Proxy is manual:** the script enables networking but does **not** set the
  proxy. After it finishes, set it on the AVD:
  *Settings → Proxy → Manual proxy configuration* (point it at your Burp listener).

---

## Roadmap / future work

- **Multiple API levels** — let the user pick/create AVDs at different API levels
  (e.g. `--api 34`), not just API 35.
- **Configurable AVD creation**, e.g.:
  - device profile (currently `pixel_8`)
  - internal storage size / partition size
  - SDK image type (Google APIs vs Play Store) and ABI
  - AVD name and storage/output location
- `--modules-only` mode to (re)install just the modules on an already-rooted AVD.
- Optional automatic proxy configuration (was removed by request).

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `adb not found` | Install Android Studio (bundles platform-tools); see Requirements. |
| `sdkmanager/avdmanager not found` | Install **Android SDK Command-line Tools** in SDK Manager. |
| `Java version 17 or higher is required` (and AVD not created) | Old Java on `PATH`; the script now pins Android Studio's JBR automatically. |
| Emulator boots but **keyboard doesn't work** | `avdmanager` AVDs default to `hw.keyboard=no`; the script sets `hw.keyboard=yes` in `config.ini` before launch. |
| Stuck at *Waiting for root to come up* | Do the Magisk **Additional Setup** + **Grant** steps on the AVD (see above). |
| Root **lost after reboot** | The AVD must **cold boot** to load the patched ramdisk; the script cold-boots for you, and `--clean` restores stock ramdisks for re-tests. |
| Modules not installed | Magisk wasn't fully set up; the script now waits + verifies and prints what landed. Manual fallback: Magisk app → Modules → Install from storage → `/sdcard/Download/`. |

---

## How it works (6 steps)

1. **Tooling** — detect `adb` / SDK tools / JDK; install OpenSSL if needed.
2. **Download** — fetch rootAVD + the 3 modules into `root_tooling\` (skips if present).
3. **Burp CA** — reuse `root_tooling\<hash>.0`, else fetch from Burp / wait for `burpca.der`.
4. **Root** — provision + launch the AVD, run `rootAVD FAKEBOOTIMG`, cold-boot, confirm `uid=0`
   (Burp CA is converted with OpenSSL and baked into `adguardcert.zip` in parallel).
5. **Modules** — wait for Magisk to be ready, then install `ZygiskNext → NoHello → adguardcert`, reboot, verify.
6. **Verify** — confirm root + the Burp CA in the system store, re-enable networking, final reboot.

A single progress bar runs across all six steps. Press **Ctrl+C** at any time for a clean cancel.
