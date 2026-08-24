#!/bin/sh
# Run the DingTalk listener in a virtualenv it manages itself.
#
# The listener is the only part of this plugin with a pip dependency, so it is
# kept out of whatever interpreter the rest of the tooling uses. Everything else
# — `loop ask`, `loop report`, the AskUserQuestion hook — works with none of this
# installed; what you lose is the ability to *answer* in DingTalk.
#
#   run-bot.sh --selftest    check credentials, allow-list, approver, repo
#   run-bot.sh               run in the foreground
#   run-bot.sh --daemon      run in the background, logging to bot.log
#   run-bot.sh --status      is it running
#   run-bot.sh --stop        stop it

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
venv="$here/.venv"
pidfile="$here/bot.pid"
logfile="$here/bot.log"
repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

ensure_venv() {
    if [ ! -x "$venv/bin/python" ]; then
        echo "creating $venv"
        # A specific interpreter, not whatever `python3` resolves to: a GUI
        # launcher can leak PYTHONHOME into the environment and break the rest.
        base=$(command -v python3 || echo /usr/bin/python3)
        env -u PYTHONHOME -u PYTHONPATH "$base" -m venv "$venv"
    fi
    if [ ! -f "$venv/.deps-ok" ] || [ "$here/requirements.txt" -nt "$venv/.deps-ok" ]; then
        env -u PYTHONHOME -u PYTHONPATH "$venv/bin/pip" install -q -r "$here/requirements.txt"
        touch "$venv/.deps-ok"
    fi
}

running() {
    [ -f "$pidfile" ] || return 1
    pid=$(cat "$pidfile" 2>/dev/null || echo)
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

case "${1:-}" in
    --status)
        if running; then echo "running (pid $(cat "$pidfile"))"; exit 0
        else echo "not running"; exit 1; fi
        ;;
    --stop)
        if running; then kill "$(cat "$pidfile")" && rm -f "$pidfile" && echo stopped
        else echo "not running"; fi
        exit 0
        ;;
    --daemon)
        ensure_venv
        if running; then echo "already running (pid $(cat "$pidfile"))"; exit 0; fi
        # One bot, one Stream connection. Two would each receive every message and
        # act on it twice.
        env -u PYTHONHOME -u PYTHONPATH "$venv/bin/python" "$here/bot.py" \
            --repo-root "$repo_root" >>"$logfile" 2>&1 &
        echo $! > "$pidfile"
        echo "started (pid $(cat "$pidfile")), logging to $logfile"
        exit 0
        ;;
    *)
        ensure_venv
        exec env -u PYTHONHOME -u PYTHONPATH "$venv/bin/python" "$here/bot.py" \
            --repo-root "$repo_root" "$@"
        ;;
esac
