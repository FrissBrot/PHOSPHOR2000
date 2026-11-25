"""Deployment scripts and NetworkManager adapter against fakes (no real Pi touched)."""
import os
import subprocess
import time
from pathlib import Path

import pytest

from backend.config import NetworkConfig
from backend.network.base import EventKind
from backend.network.nmcli import NmcliAdapter, parse_scan, split_terse, write_keyfiles

ROOT = Path(__file__).resolve().parent.parent
CONFIG_TXT = """# For more options and information see
# http://rptl.io/configtxt
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1

# Enable DRM VC4 V3D driver
dtoverlay=vc4-kms-v3d
max_framebuffers=2

disable_fw_kms_setup=1
arm_64bit=1
disable_overscan=1
arm_boost=1

[cm4]
otg_mode=1

[all]
"""
CMDLINE = "console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 rootfstype=ext4 fsck.repair=yes rootwait quiet splash plymouth.ignore-serial-consoles cfg80211.ieee80211_regdom=DE\n"


@pytest.fixture()
def fakepi(tmp_path):
    boot = tmp_path / "boot" / "firmware"
    boot.mkdir(parents=True)
    (boot / "config.txt").write_text(CONFIG_TXT)
    (boot / "cmdline.txt").write_text(CMDLINE)
    osr = tmp_path / "os-release"
    osr.write_text('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_CODENAME=bookworm\n')
    model = tmp_path / "model"
    model.write_bytes(b"Raspberry Pi 3 Model B Plus Rev 1.3\x00")
    env = dict(os.environ, CS2K_BOOT_DIR=str(boot), CS2K_BACKUP_DIR=str(tmp_path / "backup"),
               CS2K_OS_RELEASE=str(osr), CS2K_MODEL_FILE=str(model), CS2K_ALLOW_NONROOT="1")
    return boot, env, osr


def crt(env, *args):
    p = subprocess.run(["bash", str(ROOT / "scripts" / "configure-crt.sh"), *args], env=env,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_crt_kms_apply_idempotent_and_rollback(fakepi):
    boot, env, _ = fakepi
    crt(env)
    cfg = (boot / "config.txt").read_text()
    cmd = (boot / "cmdline.txt").read_text()
    assert "dtoverlay=vc4-kms-v3d,composite" in cfg
    assert "#cs2k# dtoverlay=vc4-kms-v3d" in cfg
    assert [ln for ln in cfg.splitlines() if ln.startswith("dtoverlay=vc4")] == ["dtoverlay=vc4-kms-v3d,composite"]
    assert "enable_tvout" not in cfg  # Pi 3 does not need it
    assert cmd.count("\n") == 1 and "video=Composite-1:720x576@50ie" in cmd and "vc4.tv_norm=PAL" in cmd
    assert "consoleblank=0" in cmd and cmd.startswith("console=serial0")
    crt(env)  # second run: identical result
    assert (boot / "config.txt").read_text() == cfg
    assert (boot / "cmdline.txt").read_text() == cmd
    crt(env, "--norm", "NTSC")
    assert "720x480@60ie" in (boot / "cmdline.txt").read_text()
    assert (boot / "cmdline.txt").read_text().count("video=") == 1
    crt(env, "--rollback")  # back to pre-CYBERSPACE state
    assert (boot / "config.txt").read_text() == CONFIG_TXT
    assert (boot / "cmdline.txt").read_text() == CMDLINE


def test_crt_remove_without_backup(fakepi):
    boot, env, _ = fakepi
    crt(env)
    crt(env, "--remove")
    assert (boot / "config.txt").read_text().rstrip() == CONFIG_TXT.rstrip()
    assert (boot / "cmdline.txt").read_text().split() == CMDLINE.split()


def test_crt_legacy_mode(fakepi):
    boot, env, osr = fakepi
    osr.write_text("VERSION_CODENAME=bullseye\n")
    crt(env)
    cfg = (boot / "config.txt").read_text()
    assert "sdtv_mode=2" in cfg and "sdtv_aspect=1" in cfg and "#cs2k# dtoverlay=vc4-kms-v3d" in cfg
    assert "video=Composite-1" not in (boot / "cmdline.txt").read_text()


def test_crt_status_runs(fakepi):
    _, env, _ = fakepi
    out = crt(env, "--status")
    assert "bookworm" in out


def test_scripts_parse():
    for f in (ROOT / "scripts").glob("*.sh"):
        subprocess.run(["bash", "-n", str(f)], check=True)


def test_keyfiles_secure(tmp_path):
    cfg = NetworkConfig()
    paths = write_keyfiles(cfg, "s3cretpassw0rd", tmp_path)
    for p in paths:
        assert oct(p.stat().st_mode & 0o777) == "0o600"
    ap = (tmp_path / "cyberspace-ap.nmconnection").read_text()
    assert "mode=ap" in ap and "method=shared" in ap and "address1=192.168.50.1/24" in ap
    assert "key-mgmt=wpa-psk" in ap and "proto=rsn" in ap and "pairwise=ccmp" in ap
    assert "autoconnect=false" in ap
    with pytest.raises(ValueError):
        write_keyfiles(cfg, "short", tmp_path)


def test_nmcli_parsing():
    assert split_terse(r"CYBERSPACE-2000:B8\:27\:EB\:00\:00\:01:72") == ["CYBERSPACE-2000", "B8:27:EB:00:00:01", "72"]
    res = parse_scan("CYBERSPACE-2000:B8\\:27\\:EB\\:00\\:00\\:01:72\nOther:AA\\:BB\\:CC\\:DD\\:EE\\:FF:40\n")
    assert res[0].bssid == "B8:27:EB:00:00:01" and res[0].signal == 72 and len(res) == 2


def test_nmcli_adapter_fixed_argv_and_events(tmp_path):
    calls = []

    def runner(argv, timeout):
        calls.append(list(argv))
        if "wifi" in argv and "list" in argv and "--rescan" in argv and argv[argv.index("--rescan") + 1] == "yes":
            return 0, "CYBERSPACE-2000:B8\\:27\\:EB\\:00\\:00\\:01:72\n", ""
        if argv[-4:-2] == ["connection", "up"] or "up" in argv:
            return 0, "", ""
        if "ACTIVE,BSSID" in argv:
            return 0, "yes:B8\\:27\\:EB\\:00\\:00\\:01\n", ""
        if "IP4.ADDRESS" in argv:
            return 0, "IP4.ADDRESS[1]:192.168.50.23/24\n", ""
        return 0, "", ""

    sysfs = tmp_path / "net"
    (sysfs / "wlan0").mkdir(parents=True)
    (sysfs / "wlan0" / "address").write_text("b8:27:eb:00:00:02\n")
    ad = NmcliAdapter(NetworkConfig(), runner=runner, monitor_interval=999, sysfs=sysfs)
    try:
        ad.request_scan()
        ad.connect("B8:27:EB:00:00:01")
        ad.start_hotspot()
        evs = []
        deadline = time.time() + 5
        while len(evs) < 3 and time.time() < deadline:
            evs += ad.poll_events()
            time.sleep(0.02)
    finally:
        ad.close()
    kinds = [e.kind for e in evs]
    assert kinds[0] == EventKind.SCAN_RESULT and evs[0].data[0].bssid == "B8:27:EB:00:00:01"
    assert EventKind.CONNECTED in kinds or EventKind.HOTSPOT_UP in kinds
    for argv in calls:
        assert argv[0] == "nmcli"
        assert not any("psk" in a.lower() for a in argv)  # secrets never on the command line
    ups = [a for a in calls if "up" in a]
    assert any("cyberspace-ap" in a for a in ups)
