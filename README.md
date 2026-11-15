# Android Debloater

Inspect the apps on your Android phone, review an uninstall, and restore eligible
system apps later. Android Debloater runs locally in your browser on macOS or Linux.

## Run

Install Python 3.14, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and [Android SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools).
Then run:

```sh
uv run android-debloater
```

Connect your phone with USB debugging enabled and accept its connection prompt.
The app also recognizes phones already connected through Wi-Fi ADB.

To inspect apps without allowing changes to the phone:

```sh
uv run android-debloater --read-only --data-dir .data/personal
```

The launcher also accepts `--no-browser`, `--port`, `--adb-path`, and `--data-dir`.
Stop the server with Ctrl+C. Closing the browser tab leaves it running.

## Phone compatibility

Phone changes require Android 10 or later, authorized ADB access, and a verified
main personal profile. Support depends on the commands and metadata exposed by the
firmware. A manufacturer name alone does not establish compatibility.

The app handles older role commands, optional roles and modules, single-segment
OEM package names, retained records after uninstall, and hidden or archived apps.
Hidden and archived apps stay protected. A phone in recovery or awaiting
authorization does not prevent another connected phone from loading.

If a required safety check fails, changes stay blocked with a reason. A readable
inventory remains available when command-support or protection checks fail.
Unknown package metadata never falls back to an older factory copy.

## Use

The app opens your connected phone's app list. If you connect more than one phone,
use the phone selector to switch between them. With no phone connected, choose
Search for device. Saved app details and history return when you reconnect.

Change the phone's name in Settings > Phone name. Search by app name or package ID,
choose a category beside search, or open Filters to narrow the list. The categories
are Communication, Money & shopping, Entertainment, Tools & daily use, System,
and Other. The app maps older saved categories to these groups.

Click an app's name to see what removal would affect, how to recover it, and its
package ID. You can edit its details for this phone or share them across phones.
Shared edits also apply to the current phone. Find technical information under
Technical details or Settings > Advanced settings.

Dismiss hides an error until you retry manually or a different error occurs.
Background checks do not bring back the same dismissed error.

### Update app info

Choose Update app info beside Refresh to look up the current phone's saved packages.
The updater fetches names, descriptions, and categories from Google Play. It uses
UAD-ng descriptions as a fallback for system apps.

The updater checks up to four packages at a time. Results appear as it saves them.
You can keep browsing or reviewing apps and stop the update at any time. Your
edits to names, categories, usefulness, and notes take precedence. Packages without
a listing keep their existing details.

Updates run only when you request them. The updater sends package IDs to Google
Play and downloads the system-app catalog from GitHub. It does not send device
identifiers, history, or personal notes. App details show source links and fetch
dates. Some packages have no public listing, and Google Play may limit requests
or change its pages. The updater does not change removal protections or import
removal advice.

### Uninstall and restore

Select apps and choose Review uninstall. The review uses the inventory already
on screen, so it opens without another scan. Check the app count and confirm to begin.
The server checks the phone, profile, firmware, protections, and app state before
any change. Refresh if the review is out of date. Unconfirmed reviews expire after
five minutes.

Removed shows recovery options. History records each attempt and its result.
Uninstalls and restores run one batch at a time. Stop after this app cancels the
remaining work. A failure also stops the batch. Reconnecting never retries an operation.

## Recovery and protection

- You can restore system apps while their packages remain on the phone. Restore
  also recovers their previous enabled or disabled setting.
- You must reinstall downloaded apps yourself. The app shows a Google Play link
  only if Google Play was the recorded installer.
- Uninstall asks Android to keep app data and cache. This is not a backup or a
  guarantee of recovery. Android Debloater does not save APKs or restore exact
  versions, credentials, permissions, or component settings.
- The app protects core packages, system modules, active defaults, administrators,
  and critical services. It blocks changes if it cannot complete a required check.
  Changing a category cannot remove protection. Check unfamiliar apps before
  removing them, even if the app reports no protection.

## Saved data

The default location is `~/Library/Application Support/Android Debloater` on macOS,
or the XDG data directory, usually `~/.local/share/android-debloater`, on Linux.
Use `--data-dir` to choose a different folder. Settings shows the exact location.
Keep this folder to preserve recovery history when moving the app.

The app stores each phone's data in a validated JSON record. It replaces files
atomically and keeps the previous valid record as a `.json.bak` backup. A process
lock prevents two servers from using the same data folder.

If a record is corrupt, the app preserves it and blocks changes. To recover it,
stop the app and copy the original and backup somewhere safe. Inspect the backup
before using it to replace the corrupt record.

USB and Wi-Fi connections to the same verified phone share its history. Different
phones have separate records. If the app cannot verify a phone's identity, or the
phone was reset, you must confirm before reusing its saved history.

The server listens only on localhost. It checks request Host and Origin headers
and requires a session token for changes. All interface assets and the bundled
package catalog are local. The app saves downloaded information separately in
`app-info.json`, so you can use it offline and after a restart. Updates do not
resume after shutdown.

## Development

Keep caches, temporary files, and test data inside the checkout:

```sh
mkdir -p .cache/tmp
export UV_CACHE_DIR="$PWD/.cache/uv"
export TMPDIR="$PWD/.cache/tmp"
uv sync --locked
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
node --check src/android_debloater/static/app.js
node tests/test_frontend.cjs
```

Node runs the syntax and frontend checks. The app has no Node build step.
Tests use a stateful ADB simulator and cover persistence, identity, protection,
stale reviews, cancellation, interruptions, recovery, and the local HTTP boundary.
The compatibility tests exercise scan, review, uninstall, restore, and restart
across Android API 29, 30, 31, 33, 34, 35, and 36 protocol variants, including all
five enabled settings and a main user other than user 0. These are simulations,
not hardware certification. GitHub Actions runs the checks and build on macOS and Linux.

Real-phone uninstall and restore, Linux runtime use with ADB, and firmware beyond
the recorded fixtures still need testing. No test suite guarantees every OEM
firmware or future Android release. Add a sanitized output fixture and a regression
test when a new device exposes a format difference.

Parser references: Android's [module command](https://android.googlesource.com/platform/frameworks/base/+/android11-release/services/core/java/com/android/server/pm/PackageManagerShellCommand.java#335),
[role state](https://android.googlesource.com/platform/prebuilts/fullsdk/sources/android-30/+/refs/heads/main/com/android/server/role/RoleUserState.java#479),
[package dependencies](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/core/java/com/android/server/pm/DumpHelper.java#300),
and [package visibility](https://android.googlesource.com/platform/prebuilts/fullsdk/sources/+/88c7ff1cd72d6305ec59f97aadc2198cc2dc3592/android-34/com/android/server/pm/pkg/PackageUserStateUtils.java).

See [asset notices](src/android_debloater/resources/NOTICE.md) for catalog and font sources.
