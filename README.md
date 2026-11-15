# Android Debloater

Inspect the apps on your Android phone and save personal app details.
Runs locally in your browser on macOS or Linux.

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

Other options: `--no-browser`, `--port`, `--adb-path`, and `--data-dir`.
Stop the server with Ctrl+C. Closing the browser tab leaves it running.

## Use

Your connected phone opens automatically. The app shows a phone selector only
when multiple phones are connected. With no phone connected, the app shows
**Search for device**. Saved app details return when you reconnect.

The phone's name appears above its apps. Change it in **Settings > Phone name**.
Search for an app, choose a category beside search, or open **Filters** to narrow
the list further. The six categories are Communication, Money & shopping,
Entertainment, Tools & daily use, System, and Other. Older saved categories combine
into these groups automatically. Package IDs remain searchable and appear in app details.
Click an app's name for
purpose, protection, and editable details. Technical information
is under **Technical details** or **Settings > Advanced settings**.
Sharing edited details also applies those shared values to the current phone.

Error messages have a **Dismiss** button. Background checks keep the same dismissed
error hidden; a manual retry or a different error can show a new message.

## Saved data

The default location is `~/Library/Application Support/Android Debloater` on macOS,
or the XDG data directory, usually `~/.local/share/android-debloater`, on Linux.
Use `--data-dir` to choose a different folder. Settings shows the exact location.
Keep this folder to preserve saved app details when moving the app.

Each phone has a validated JSON record with atomic writes, a process lock, and a
last-known-good `.json.bak` backup. Corrupt records stay intact and block changes.
To recover one, stop the app, copy the original and backup somewhere safe, inspect
the backup, and restore it deliberately.

USB and Wi-Fi connections to the same verified phone share its history. Different
phones stay separate. Ambiguous identity or a reset requires confirmation before
using old history. The server binds to loopback and checks Host, origin, and a
session token. All UI assets and the bundled package catalog are local.

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
app details, and the local HTTP boundary. Linux runtime still needs hardware validation.

See [asset notices](src/android_debloater/resources/NOTICE.md) for catalog and font sources.
