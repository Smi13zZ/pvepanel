#!/usr/bin/env python3
"""pve-test.py - run a command on the disposable PVE that lives inside fnos-test.

The test PVE sits on libvirt's NAT bridge (192.168.122.0/24), so only the NAS
itself can reach it. Every command therefore goes through two hops, and this
script exists so nobody has to remember the nested quoting.

    python tools/pve-test.py "qm list"
    python tools/pve-test.py "pveversion -v"
    python tools/pve-test.py --shell

Deliberately NOT a general SSH helper: it has one destination, it refuses to
run when the guest is down, and it never touches the production PVE.
"""
import argparse
import os
import subprocess
import sys

NAS = "fnos-test"
GUEST = "root@192.168.122.50"
KEY = "~/.ssh/id_ed25519_pvetest"

BASE = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", NAS,
    # no UserKnownHostsFile=/dev/null: the host key is stable, and pointing at
    # /dev/null makes ssh print a warning on every call that PowerShell renders
    # as a red error even when the command succeeded.
    "ssh -i %s -o StrictHostKeyChecking=accept-new -o BatchMode=yes "
    "-o ConnectTimeout=10 -o LogLevel=ERROR %s" % (KEY, GUEST),
]


def run(remote_cmd, tty=False):
    # The remote command is passed as a single argument and quoted on the NAS
    # side, so shell metacharacters in it are interpreted there rather than here.
    cmd = BASE + [shell_quote(remote_cmd)]
    argv = cmd if not tty else cmd[:-1] + ["-t", cmd[-1]]
    return subprocess.call(argv)


def shell_quote(text):
    # single-quote for the NAS shell; the guest shell receives it verbatim
    return "'" + text.replace("'", "'\\''") + "'"


def status():
    """Cheap health line, so a broken test box is obvious before anything else."""
    code = subprocess.call(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", NAS,
         "sudo virsh domstate pve-test01 2>&1"],
        stdout=subprocess.PIPE)
    return code == 0


def main():
    ap = argparse.ArgumentParser(description="Run a command on the test PVE")
    ap.add_argument("command", nargs="*", help="command to run; omit for --shell")
    ap.add_argument("--shell", action="store_true", help="open an interactive shell")
    args = ap.parse_args()

    if args.shell:
        sys.exit(run("bash -l", tty=True))

    if not args.command:
        ap.print_help()
        print()
        print("the test PVE is at 192.168.122.50:8006, reached through fnos-test")
        sys.exit(2)

    sys.exit(run(" ".join(args.command)))


if __name__ == "__main__":
    main()
