# Windows Installer

We use [msys2](https://www.msys2.org/) for creating the Windows installer and development on Windows.

## Ship an installer (for someone who just wants to double-click)

On a Windows 10/11 PC:

1. Install [MSYS2](https://www.msys2.org/) and finish its update steps.
2. Open **MSYS2 UCRT64** from the Start menu (not MinGW64).
   Or: `C:\msys64\msys2_shell.cmd -ucrt64`
3. Clone **this** repository (not upstream Gajim), then:

```bash
cd gajim
./win/build.sh
```

If pacman fails with “Operation too slow” or a dropped mirror, just run `./win/build.sh` again. It resumes.

The installers are created **at the end**. You should see:

```
==> NSIS installers (send one of these):
```

`Gajim.exe` / `Gajim-Portable.exe` live in `win/_build_root/`, not inside `win/_build_root/ucrt64`. Hook messages like `execv failed` during pacman are noisy and can be ignored.

4. Send the friend one of the files in `win/_build_root`:
   - `Gajim.exe` — normal installer (Start Menu, needs admin)
   - `Gajim-Portable.exe` — no admin, unpacks under their user folder

They should quit any existing Gajim first, then run that file. They do not need Python, MSYS2, or extra codecs.

## Development

Download [msys2](https://www.msys2.org/) (`msys2-x86_64-xxx.exe`) and follow the install instructions on the [msys2](https://www.msys2.org/) startpage (**Important!**)

* Fork the master branch on dev.gajim.org
* Execute `C:\msys64\msys2_shell.cmd -ucrt64` (Start Menu: **MSYS2 UCRT64**, not MinGW64)
* Run `pacman -S git` to install git
* Run `git clone https://dev.gajim.org/USERNAME/gajim.git`
* Run `cd gajim`
* Create a virtual environment with access to MSYS packages: `python -m venv .venv --system-site-packages`
* Activate the newly created environment: `source .venv/bin/activate`
* Execute `./win/dev_env.sh` to install all the needed dependencies
* Launch Gajim `./launch.py`

### GTK Inspector

For GTK Inspector to work, add the following registry key

```text
HKEY_CURRENT_USER\Software\GSettings\org\gtk\gtk4\settings\debug
DWORD (32 bits) enable-inspector-keybinding = 1
```

Afterwards press CTRL + SHIFT + I to  activate GTK Inspector

## Build Gajim / Create an Installer

Follow the steps in the Development section, but instead of `./dev_env.sh` execute `./build.sh`.

Both the installer and the portable installer should appear in `C:\msys64\home\USER\gajim\win\_build_root`.

## Register Development App from msixbundle

To test Gajim's Microsoft Store msix bundle, the following steps are necessary:

1. Either build the msixbundle locally by running `./build.sh` as described above, or download a nightly build and place it in `C:\msys64\home\USER\gajim\win\_build_root\Gajim.msixbundle`
2. Run `./unpack_msixbundle.sh`, which unpacks the bundle to `C:\msys64\home\USER\gajim\win\_build_root\unpack\Gajim`
3. Open `C:\msys64\home\USER\gajim\win\_build_root\unpack\Gajim` in a PowerShell
4. For easier debugging, change `bin\Gajim.exe` to `bin\Gajim-Debug.exe` in `AppxManifest.xml`, like this: `<Application Id="Gajim" Executable="bin\Gajim-Debug.exe" EntryPoint="Windows.FullTrustApplication">`
5. Now register the app by running `Add-AppxPackage –Register AppxManifest.xml` from a PowerShell
6. Registering the app again requires a version bump in `AppxManifest.xml` (or uninstalling the Gajim app)

To modify code, you can replace `.pyc` files by their equivalent `.py` files from this repo. Gajim's code within the App installation can be found in `C:\msys64\home\USER\gajim\win\_build_root\unpack\Gajim\lib\python3.11\site-packages\gajim`. Code changes do not require to re-register the app.
