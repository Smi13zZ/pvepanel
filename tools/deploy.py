#!/usr/bin/env python3
"""Install (or reinstall) the fn-pvepanel fpk on a fnOS box and wait until it serves.

Three fnOS behaviours are baked in, each one cost a debugging session before:

  * install-fpk on an already-installed app returns code 18000 -- there is no
    in-place overwrite.  The only reliable order is stop -> uninstall -> install.
  * after install-fpk the app sits in 'starting'; appcenter-cli start often
    returns 10500 even though the service does come up.  So we never trust the
    return code, we poll until the port actually answers.
  * uninstall keeps @appdata (that is why reinstall inherits the config), so a
    plain reinstall is the normal iteration loop.

Usage
    python tools/deploy.py
    python tools/deploy.py --env wizard_backend_port=8899
    python tools/deploy.py --fpk output/fn-pvepanel-26.11.2-fnos-all.fpk
    python tools/deploy.py --no-uninstall      # first install on a clean box

At the end it prints the raw /api/health body, so a failed deploy is visible.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402


# Positional args (bash -s -- ...): 1=app 2=fpk 3=envfile 4=do_uninstall 5=do_install
REMOTE_INSTALL = r"""set -u
APP="$1"; FPK="$2"; ENVFILE="$3"; DO_UNINSTALL="$4"; DO_INSTALL="$5"

echo "--- before ---"
sudo appcenter-cli status "$APP" 2>&1 | tail -2 || true

if [ "$DO_UNINSTALL" = "1" ]; then
    echo "--- stop ---"
    sudo appcenter-cli stop "$APP" 2>&1 | tail -2 || true
    sleep 1
    echo "--- uninstall ---"
    sudo appcenter-cli uninstall "$APP" 2>&1 | tail -3 || true
    sleep 1
fi

if [ "$DO_INSTALL" = "1" ]; then
    echo "--- install-fpk ---"
    sudo appcenter-cli install-fpk "$FPK" -e "$ENVFILE" -v 1 2>&1 | tail -5
    echo "install-fpk done"
    sleep 1
    echo "--- start ---"
    # 10500 here means 'already starting' as often as it means a real failure.
    sudo appcenter-cli start "$APP" 2>&1 | tail -3
    echo "start done (10500 is common and harmless -- the port poll below decides)"
fi

echo "--- status after ---"
sudo appcenter-cli status "$APP" 2>&1 | tail -2 || true
"""


# Positional args: 1=pkgvar  -- the port the lifecycle script remembered
# @appdata is 0700 root:root, so an unprivileged test -r says nothing - the
# same trap the log file had. Read it through sudo and let the emptiness speak.
PORT_PROBE = r"""set -u
VAR="$1"
f="$VAR/backend-port"
if sudo test -r "$f"; then sudo cat "$f" | tr -dc '0-9'; fi
echo ""
"""


# Positional args: 1=app 2=port 3=pkgvar
PROBE = r"""set -u
APP="$1"; PORT="$2"; VAR="$3"
code=$(curl -s -m 3 -o /tmp/pvepanel-health.body -w '%{http_code}' "http://127.0.0.1:$PORT/api/health" 2>/dev/null || echo 000)
echo "HTTP $code"
if [ "$code" != "000" ]; then cat /tmp/pvepanel-health.body; fi
echo ""
echo "--- extra ---"
if curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
    echo "root page: reachable"
else
    echo "root page: unreachable"
fi
# The log is 700 root:root inside a 770 root:root dir, so the SSH user
# cannot read it without sudo -- an unprivileged [ -r ] check just lies.
if sudo test -r "$VAR/pvepanel.log"; then
    echo "--- log tail ---"
    sudo tail -n 25 "$VAR/pvepanel.log"
else
    echo "(no log at $VAR/pvepanel.log)"
fi
echo "--- port ---"
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    echo "port $PORT open"
else
    echo "port $PORT closed"
fi
"""


# Positional args: 1=app  -- exercises the CGI shim the desktop iframe uses
CGI_PROBE = r"""set -u
APP="$1"
sudo env REQUEST_METHOD=GET \
  REQUEST_URI="/cgi/ThirdParty/$APP/index.cgi/api/health" \
  cgiName="$APP" GATEWAY_INTERFACE=CGI/1.1 SERVER_PROTOCOL=HTTP/1.1 \
  /var/apps/"$APP"/target/ui/index.cgi 2>&1 | tail -6
"""


def probe_remembered_port(host, var_dir):
    """The port the lifecycle script saved, or 0 when there is none yet."""
    res = C.ssh(host, PORT_PROBE, args=(var_dir,))
    text = (res.out or "").strip().splitlines()
    for line in text:
        line = line.strip()
        if line.isdigit():
            value = int(line)
            if 1 <= value <= 65535:
                return value
    return 0


def main():
    C.setup_stdio()
    ap = argparse.ArgumentParser(description="Deploy fn-pvepanel fpk to fnOS")
    ap.add_argument("--host", default=C.HOST_DEFAULT)
    ap.add_argument("--fpk", default=None, help="fpk path (default: newest in output/)")
    ap.add_argument("--app", default=None, help="appname (default: read from the fpk manifest)")
    ap.add_argument("--port", type=int, default=None,
                    help="port to poll (default: the port the app remembered, "
                         "falling back to service_port in the manifest)")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="wizard env, repeatable (default: wizard_backend_port=<port>)")
    ap.add_argument("--no-uninstall", action="store_true", help="do not stop/uninstall first (clean box)")
    ap.add_argument("--no-install", action="store_true", help="only restart + probe the installed app")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for the port")
    args = ap.parse_args()

    fpk = os.path.abspath(args.fpk) if args.fpk else C.newest_fpk()
    if not fpk or not os.path.isfile(fpk):
        C.fail("no fpk to deploy (build one first, or pass --fpk)")

    man = C.fpk_manifest(fpk)
    app = args.app or man.get("appname") or "fn-pvepanel"
    # service_port in the manifest is a port-conflict hint for the app centre,
    # not the port the panel listens on (checkport=false, and the port is picked
    # at first start). Never use it as the thing to poll.
    port = args.port or 0
    paths = C.app_paths(app)

    env_pairs = []
    for item in args.env:
        if "=" not in item:
            C.fail("--env expects KEY=VALUE, got %r" % item)
        env_pairs.append(tuple(item.split("=", 1)))
    # The panel now chooses a random free port unless the wizard is given one.
    # For a reinstall we therefore reuse whatever the running app remembered,
    # otherwise every deploy would move the port and the probe would fail.
    explicit_port = args.port is not None
    # An explicit BACKEND_PORT (or --port) always wins: that is the whole point
    # of pinning. Otherwise reuse whatever the running app remembered, and if
    # there is nothing remembered let the app pick.
    pinned = any(k == "BACKEND_PORT" for k, _ in env_pairs)
    if pinned:
        for k, v in env_pairs:
            if k == "BACKEND_PORT":
                try:
                    port = int(v)
                except ValueError:
                    C.fail("BACKEND_PORT must be a number, got %r" % v)
        print("port   %d (pinned via --env BACKEND_PORT)" % port)
    elif explicit_port:
        print("port   %d (pinned via --port)" % port)
        env_pairs.insert(0, ("BACKEND_PORT", str(port)))
    else:
        remembered = probe_remembered_port(args.host, paths["var"])
        if remembered:
            port = remembered
            print("port   %d (remembered by the running app)" % port)
        else:
            print("port   random (the app will pick a free one)")
            port = None

    print("deploy %s -> %s" % (os.path.basename(fpk), args.host))
    print("app    %s   port %s" % (app, port if port else "auto"))
    print("env    " + " ".join("%s=%s" % kv for kv in env_pairs))
    print("")

    timer = C.Timer()

    remote_fpk = "/tmp/" + os.path.basename(fpk)
    C.scp_to(args.host, fpk, remote_fpk)
    timer.lap("upload fpk")

    env_body = "".join("%s=%s\n" % kv for kv in env_pairs)
    res = C.ssh(args.host, "cat > /tmp/pvepanel.env <<'PVE_ENV_EOF'\n" + env_body + "PVE_ENV_EOF\n")
    if res.rc != 0:
        sys.stderr.write(res.err)
        C.fail("could not write the wizard env file")

    res = C.ssh(
        args.host, REMOTE_INSTALL,
        args=(app, remote_fpk, "/tmp/pvepanel.env",
              "0" if args.no_uninstall else "1",
              "0" if args.no_install else "1"),
    )
    print(C.clean_appcenter_log(res.out))
    if res.err.strip():
        sys.stderr.write(res.err)
    timer.lap("install")

    # Poll the port ourselves: appcenter-cli's rc is not a health signal.
    # When no port was pinned the app picks one itself, so read it back first.
    if port is None:
        print("")
        print("waiting for the app to publish its port ...")
        for _ in range(40):
            port = probe_remembered_port(args.host, paths["var"])
            if port:
                break
            time.sleep(0.5)
        if not port:
            C.fail("the app did not publish a port under %s/backend-port" % paths["var"])
        print("       picked port %d" % port)
    print("")
    print("waiting for http://127.0.0.1:%d/api/health ..." % port)
    deadline = time.time() + args.timeout
    started = time.time()
    last = None
    ready = False
    full = ""
    while time.time() < deadline:
        probe = C.ssh(args.host, PROBE, args=(app, port, paths["var"]))
        full = probe.out
        first = full.splitlines()[0].strip() if full.strip() else "HTTP 000"
        if first != last:
            print("      %-12s (+%.1fs)" % (first, time.time() - started))
            last = first
        if first.startswith("HTTP 2"):
            ready = True
            break
        time.sleep(1.5)
    timer.lap("probe")

    print("")
    print(full.strip())
    print("")

    if not ready:
        print(timer.total())
        C.fail("%s did not answer on port %d within %.0fs" % (app, port, args.timeout))

    # Cross-check through the CGI shim the fnOS desktop actually uses.
    cgi = C.ssh(args.host, CGI_PROBE, args=(app,))
    print("--- via index.cgi (the path the desktop iframe uses) ---")
    print(cgi.out.strip() or "(empty)")

    print("")
    print(timer.total())
    print("DEPLOYED %s %s on port %d" % (app, man.get("version", "?"), port))
    return 0


if __name__ == "__main__":
    sys.exit(main())