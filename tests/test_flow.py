"""End-to-End-Tests gegen die simulierte FRITZ!Box (benötigt Rechte für Port 80)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fritzconf import netutil, runner as runner_mod  # noqa: E402
from fritzconf.boxinfo import version_tuple  # noqa: E402
from fritzconf.config import JobConfig  # noqa: E402
from fritzconf.firmware import FirmwareRepo, inspect_image, parse_firmwarecfg_response  # noqa: E402
from fritzconf.runner import Callbacks, Runner, pick_provider  # noqa: E402
from fritzconf.session import calc_challenge_response  # noqa: E402
from tests.mockbox import PW, MockBox, make_image  # noqa: E402

netutil.BOOT_SETTLE_SECONDS = 0.2
netutil.IP_CHANGE_SETTLE_SECONDS = 0.5


class Base(unittest.TestCase):
    box_kwargs: dict = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fwdir = os.path.join(self.tmp, "firmware")
        os.makedirs(self.fwdir)
        runner_mod.logs_dir = lambda: self.tmp
        self.box = MockBox(**self.box_kwargs)
        self.logs = []
        self.steps = {}

    def tearDown(self):
        self.box.close()
        time.sleep(0.2)

    def cfg(self, **kw) -> JobConfig:
        c = JobConfig(host="127.0.0.1", password=PW, firmware_online=False)
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def run_job(self, cfg: JobConfig, twofactor=None):
        cb = Callbacks(log=lambda l, m: self.logs.append((l, m)),
                       step=lambda s, st, d: self.steps.__setitem__(s, (st, d)),
                       twofactor=twofactor)
        r = Runner(cfg, cb, threading.Event(), repo=FirmwareRepo(self.fwdir, lambda l, m: None),
                   reboot_timeouts=(8, 30), fw_timeouts=(15, 30))
        return r.run()

    def dump(self):
        return "\n".join(f"{l}: {m}" for l, m in self.logs)


class TestDslFull(Base):
    def test_full_run(self):
        make_image(os.path.join(self.fwdir, "new.image"), "256", "08.50")
        make_image(os.path.join(self.fwdir, "wronghw.image"), "999", "09.99")
        cfg = self.cfg(set_network=True, lan_ip="127.0.0.2", lan_mask="255.255.255.0",
                       dhcp_start="127.0.0.20", dhcp_end="127.0.0.200", update_firmware=True)
        res = self.run_job(cfg)
        self.assertTrue(res.ok, self.dump())
        v = self.box.vars
        self.assertEqual(v["language:settings/current_language"], "de")
        self.assertEqual(v["country:settings/current_country"], "039")
        self.assertEqual(v["connections:settings/connection0/providername"], "raiffeisen")
        self.assertEqual(v["connections:settings/connection0/username"], "nome@xdsl.rol")
        self.assertEqual(v["connections:settings/connection0/disconnect_prevention_enable"], "1")
        self.assertEqual(v["connections:settings/connection0/disconnect_prevention_hour"], "3")
        self.assertEqual(self.box.last_cred.get("medium"), "dsl")
        self.assertEqual(self.box.last_cred.get("connmode"), "always_online")
        self.assertEqual(v["interfaces:settings/lan0/ipaddr"], "127.0.0.2")
        self.assertEqual(v["interfaces:settings/lan0/dhcpstart"], "127.0.0.20")
        self.assertEqual(v["dnscfg:settings/ipv4_dhcpserver_lan_dns"], "127.0.0.2")
        self.assertEqual(self.box.last_boxnet["lease_time"], "10")
        self.assertEqual(self.box.version, "256.08.50")
        self.assertEqual(res.box_after.fw_display, "8.50")
        self.assertEqual({k: s for k, (s, _) in self.steps.items()},
                         {"connect": "ok", "locale": "ok", "provider": "ok", "network": "ok", "firmware": "ok",
                          "infoled": "ok"})
        self.assertEqual(v["box:settings/infoled_reason"], "4")
        self.assertEqual(self.box.rest_calls, [("PUT", "/api/v0/generic/box", {"infoled_reason": 4})])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "protokoll.csv")))
        # zweiter Lauf: alles schon erledigt -> übersprungen
        self.logs.clear()
        cfg2 = self.cfg(host="127.0.0.2", set_network=True, lan_ip="127.0.0.2", lan_mask="255.255.255.0",
                        dhcp_start="127.0.0.20", dhcp_end="127.0.0.200", update_firmware=True)
        res2 = self.run_job(cfg2)
        self.assertTrue(res2.ok, self.dump())
        self.assertEqual(self.steps["locale"][0], "skip")
        self.assertEqual(self.steps["network"][0], "skip")
        self.assertEqual(self.steps["firmware"][0], "skip")
        self.assertEqual(self.steps["provider"][0], "ok")
        self.assertEqual(self.steps["infoled"][0], "skip")


class TestTwoFactorConfirm(Base):
    box_kwargs = {"require_2fa": True, "require_confirm": True}

    def test_2fa(self):
        seen = []

        def tfa(req, poll):
            seen.append(req)
            for _ in range(20):
                r = poll()
                if r is not None:
                    return r
                time.sleep(0.05)
            return False

        res = self.run_job(self.cfg(), twofactor=tfa)
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(seen[0].methods, ["button", "dtmf"])
        self.assertEqual(seen[0].code, "5918")
        self.assertIn("*15918", seen[0].describe())
        self.assertEqual(self.box.vars["connections:settings/connection0/providername"], "raiffeisen")

    def test_2fa_denied(self):
        res = self.run_job(self.cfg(), twofactor=lambda req, poll: False)
        self.assertFalse(res.ok)
        self.assertEqual(self.steps["provider"][0], "error")
        self.assertEqual(self.box.vars["connections:settings/connection0/providername"], "")


class TestAtaBox(Base):
    box_kwargs = {"dsl": False, "hw": "261", "name": "FRITZ!Box 4060"}

    def test_ata_auto_provider(self):
        res = self.run_job(self.cfg(custom_credentials=True, ppp_user="kunde@xdsl.rol", ppp_password="x1"))
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(self.box.vars["connections:settings/connection0/providername"], "raiffeisen_lan1")
        self.assertEqual(self.box.vars["connections:settings/connection0/username"], "kunde@xdsl.rol")
        self.assertEqual(self.box.vars["connections:settings/connection0/disconnect_prevention_hour"], "4")
        self.assertNotIn("medium", self.box.last_cred)


class TestFiberBox(Base):
    box_kwargs = {"fiber": True, "hw": "298", "name": "FRITZ!Box 5690"}

    def test_fiber_auto_provider(self):
        res = self.run_job(self.cfg())
        self.assertTrue(res.ok, self.dump())
        v = self.box.vars
        self.assertEqual(v["connections:settings/connection0/providername"], "raiffeisen_fiber")
        self.assertEqual(v["connections:settings/connection0/username"], "nome@xdsl.rol")
        self.assertEqual(v["tr069:settings/url"], "http://acs.raiffeisen.net")
        self.assertNotIn("medium", self.box.last_cred)
        self.assertNotIn("useprevention", self.box.last_cred)
        self.assertTrue(any(p.get("page") == "fiberSet" and "apply" in p for p in self.box.posts))


class TestFiber5530Legacy(Base):
    box_kwargs = {"fiber": True, "hw": "262", "name": "FRITZ!Box 5530 Fiber", "version": "262.07.81",
                  "legacy_fiber_ids": True}

    def _check(self, provider):
        res = self.run_job(self.cfg(provider=provider))
        self.assertTrue(res.ok, self.dump())
        v = self.box.vars
        self.assertEqual(v["connections:settings/connection0/providername"], "raiffeisen")
        self.assertEqual(v["connections:settings/connection0/username"], "nome@xdsl.rol")
        self.assertEqual(v["connections:settings/connection0/password"], "pass22xdsl")
        self.assertTrue(any("verwende 'raiffeisen'" in m for _, m in self.logs), self.dump())

    def test_saved_fiber_profile(self):
        self._check("raiffeisen_fiber")   # in settings.json gespeichert (wie beim Fehler)

    def test_auto_profile(self):
        self._check("auto")


class TestMultiwan5690Pro(Base):
    """5690 Pro mit FRITZ!OS 7.62: nur 'multiwanSet', cred/fiberSet/ataSet liefern die Übersicht."""
    box_kwargs = {"multiwan": True, "hw": "305", "name": "FRITZ!Box 5690 Pro", "version": "305.07.62"}

    def _run(self, provider):
        res = self.run_job(self.cfg(provider=provider))
        self.assertTrue(res.ok, self.dump())
        post = next(p for p in self.box.posts if p.get("page") == "multiwanSet" and "apply" in p)
        return self.box.vars, post

    def test_fiber_profile(self):
        v, post = self._run("raiffeisen_fiber")   # wie in settings.json
        self.assertEqual(post["multiwan_page"], "fiber")
        self.assertEqual(v["connections:settings/connection0/providername"], "raiffeisen_fiber")
        self.assertEqual(v["connections:settings/connection0/username"], "nome@xdsl.rol")
        self.assertEqual(v["connections:settings/connection0/password"], "pass22xdsl")
        self.assertEqual(self.box.last_cred.get("useprevention"), "on")
        self.assertNotIn("medium", self.box.last_cred)

    def test_auto_is_fiber(self):
        _, post = self._run("auto")
        self.assertEqual(post["provider"], "raiffeisen_fiber")
        self.assertEqual(post["multiwan_page"], "fiber")

    def test_dsl_profile(self):
        v, post = self._run("raiffeisen")
        self.assertEqual(post["multiwan_page"], "dsl")
        self.assertEqual(self.box.last_cred.get("medium"), "dsl")
        self.assertEqual(v["connections:settings/connection0/providername"], "raiffeisen")

    def test_wan_profile(self):
        _, post = self._run("raiffeisen_lan1")
        self.assertEqual(post["multiwan_page"], "dsl")
        self.assertNotIn("medium:raiffeisen_lan1", post)


class TestInfoLed(Base):
    def test_infoled_only(self):
        res = self.run_job(self.cfg(set_locale=False, set_provider=False))
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(self.steps["infoled"][0], "ok")
        self.assertIn("Internetverbindung besteht", self.steps["infoled"][1])
        self.assertEqual(self.box.vars["box:settings/infoled_reason"], "4")

    def test_infoled_off(self):
        res = self.run_job(self.cfg(set_locale=False, set_provider=False, set_infoled=False))
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(self.steps["infoled"][0], "off")
        self.assertEqual(self.box.vars["box:settings/infoled_reason"], "0")

    def test_infoled_dry_run(self):
        res = self.run_job(self.cfg(set_locale=False, set_provider=False, dry_run=True))
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(self.steps["infoled"][0], "skip")
        self.assertEqual(self.box.vars["box:settings/infoled_reason"], "0")

    def test_csv_migration(self):
        import csv
        p = os.path.join(self.tmp, "protokoll.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Zeit", "Ergebnis", "Modell", "MAC/Serial", "FW vorher", "FW nachher", "Box-IP",
                        "Sprache/Land", "Anbieter", "Heimnetz", "Firmware", "Logdatei"])
            w.writerow(["2026-11-11 15:16:28", "OK", "FRITZ!Box 5690", "0CC5", "8.19", "8.50", "192.168.178.1",
                        "skip", "ok", "off", "ok", "alt.log"])
        self.run_job(self.cfg(set_locale=False, set_provider=False))
        with open(p, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh, delimiter=";"))
        self.assertEqual(rows[0][11:], ["Info-LED", "Logdatei"])
        self.assertEqual(rows[1][11:], ["", "alt.log"])
        self.assertEqual(rows[2][11], "ok")


class TestInfoLedLegacy(Base):
    box_kwargs = {"rest_api": False}

    def test_infoled_fallback(self):
        res = self.run_job(self.cfg(set_locale=False, set_provider=False))
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(self.box.vars["box:settings/infoled_reason"], "4")
        self.assertTrue(any("Info-Anzeige" in m for _, m in self.logs))
        self.assertTrue(any(p.get("led_change_state_event") == "4" for p in self.box.posts))


class TestHostConfig(unittest.TestCase):
    def test_fritz_box_removed_from_saved_hosts(self):
        c = JobConfig.from_json({"hosts": ["192.168.178.1", "fritz.box", "169.254.1.1"]})
        self.assertEqual(c.hosts, ["192.168.178.1", "169.254.1.1"])
        self.assertNotIn("fritz.box", JobConfig().hosts)

    def test_new_settings_defaults(self):
        c = JobConfig.from_json({"set_locale": True})  # alte settings.json ohne neue Felder
        self.assertTrue(c.set_infoled)
        self.assertEqual(c.infoled_reason, "4")
        self.assertEqual(c.ui_theme, "system")
        c = JobConfig.from_json({"infoled_reason": 99, "ui_theme": "lila"})
        self.assertEqual((c.infoled_reason, c.ui_theme), ("4", "system"))


class TestErrors(Base):
    def test_wrong_password(self):
        res = self.run_job(self.cfg(password="falsch"))
        self.assertFalse(res.ok)
        self.assertEqual(self.steps["connect"][0], "error")
        self.assertIn("Kennwort", self.steps["connect"][1])
        self.assertEqual(self.steps["locale"][0], "skip")

    def test_provider_without_country(self):
        res = self.run_job(self.cfg(set_locale=False))
        self.assertFalse(res.ok)
        self.assertIn("KONVERTO-Profil in der Anbieterliste", self.steps["provider"][1])

    def test_invalid_network(self):
        res = self.run_job(self.cfg(set_locale=False, set_provider=False, set_network=True, lan_ip="127.0.0.1",
                                    lan_mask="255.255.255.0", dhcp_start="127.0.0.1", dhcp_end="127.0.0.50"))
        self.assertFalse(res.ok)
        self.assertIn("DHCP-Pool", self.steps["network"][1])

    def test_dry_run(self):
        res = self.run_job(self.cfg(dry_run=True, set_network=True, lan_ip="127.0.0.2", dhcp_start="127.0.0.20",
                                    dhcp_end="127.0.0.200"))
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(self.box.vars["country:settings/current_country"], "099")
        self.assertEqual(self.box.reboots, 0)

    def test_firmware_missing_is_warning(self):
        res = self.run_job(self.cfg(set_locale=False, set_provider=False, update_firmware=True))
        self.assertTrue(res.ok)
        self.assertEqual(self.steps["firmware"][0], "warn")

    def test_manual_wrong_hw(self):
        p = make_image(os.path.join(self.tmp, "x.image"), "185", "07.62", "FRITZ!Box 7490")
        res = self.run_job(self.cfg(set_locale=False, set_provider=False, update_firmware=True, firmware_file=p))
        self.assertFalse(res.ok)
        self.assertIn("Hardware", self.steps["firmware"][1])


class TestFallbacks(Base):
    box_kwargs = {"web_locale_broken": True}

    def test_tr064_locale_fallback(self):
        res = self.run_job(self.cfg(set_provider=False))
        self.assertTrue(res.ok, self.dump())
        self.assertIn("TR-064", self.steps["locale"][1])
        self.assertEqual(self.box.vars["country:settings/current_country"], "039")


class TestFirmwareTr064(Base):
    box_kwargs = {"reject_web_upload": True}

    def test_tr064_firmware_fallback(self):
        make_image(os.path.join(self.fwdir, "new.image"), "256", "08.50")
        res = self.run_job(self.cfg(set_locale=False, set_provider=False, update_firmware=True))
        self.assertTrue(res.ok, self.dump())
        self.assertEqual(self.box.version, "256.08.50")
        self.assertTrue(any("TR-064" in m for _, m in self.logs))


class TestUnits(unittest.TestCase):
    def test_pick_provider(self):
        def lst(*ids, medium=""):
            return [{"Id": i, "providername": i, "medium": medium} for i in ids]
        pick = lambda l, pid, kind: (pick_provider(l, pid, kind) or {}).get("Id")  # noqa: E731
        full = lst("tim_vdsl", "raiffeisen", "raiffeisen_lan1", "raiffeisen_fiber", "other")
        self.assertEqual(pick(full, "raiffeisen_fiber", "fiber"), "raiffeisen_fiber")
        self.assertEqual(pick(full, "gibtsnicht", "fiber"), "raiffeisen_fiber")
        self.assertEqual(pick(full, "gibtsnicht", "dsl"), "raiffeisen")
        self.assertEqual(pick(full, "gibtsnicht", "ata"), "raiffeisen_lan1")
        legacy = lst("bruneck_fiber", "conv_fiber", "other_fiber", "raiffeisen", "tim_fiber", medium="FIBER")
        self.assertEqual(pick(legacy, "raiffeisen_fiber", "fiber"), "raiffeisen")
        self.assertEqual(pick([{"Id": "xy_glas", "providername": "KONVERTO Glasfaser"}], "raiffeisen_fiber", "fiber"),
                         "xy_glas")
        self.assertIsNone(pick(lst("tim_fiber", "other_fiber"), "raiffeisen_fiber", "fiber"))

    def test_versions(self):
        self.assertEqual(version_tuple("256.08.50"), (8, 50))
        self.assertEqual(version_tuple("07.57"), (7, 57))
        self.assertEqual(version_tuple("256.08.50-123456"), (8, 50, 123456))

    def test_md5_response(self):
        # Beispiel aus der AVM-Dokumentation
        self.assertEqual(calc_challenge_response("1234567z", "äbc"), "1234567z-9e224a41eeefa284df7bb0f26c2913e2")

    def test_pbkdf2_response(self):
        ch = "2$10000$5A1711$2000$5A1722"
        self.assertEqual(calc_challenge_response(ch, "1example!"),
                         "5A1722$1798a1672bca7c6463d6b245f82b53703b0f50813401b03e4045a5861e689adb")

    def test_lan_validation(self):
        v = netutil.validate_lan_settings("192.168.10.1", "255.255.255.0", "192.168.10.20", "192.168.10.200")
        self.assertEqual(v, ("192.168.10.1", "255.255.255.0", "192.168.10.20", "192.168.10.200"))
        for bad in [("192.168.10.1", "255.255.255.0", "192.168.11.20", "192.168.10.200"),
                    ("192.168.10.1", "255.255.255.0", "192.168.10.200", "192.168.10.20"),
                    ("192.168.10.50", "255.255.255.0", "192.168.10.20", "192.168.10.200"),
                    ("192.168.10.0", "255.255.255.0", "192.168.10.20", "192.168.10.200"),
                    ("8.8.8.8", "255.255.255.0", "8.8.8.20", "8.8.8.200")]:
            with self.assertRaises(ValueError):
                netutil.validate_lan_settings(*bad)
        self.assertEqual(netutil.suggest_pool("10.0.5.1"), ("10.0.5.20", "10.0.5.200"))
        self.assertEqual(netutil.suggest_pool("10.0.5.100"), ("10.0.5.20", "10.0.5.99"))

    def test_firmwarecfg_parse(self):
        self.assertEqual(parse_firmwarecfg_response('{"reboot": 1 }')[0], True)
        ok, msg = parse_firmwarecfg_response('{"reboot":0,"timeToReboot":0,"errors":[{"code":20002,"message":"x"}]}')
        self.assertFalse(ok)
        self.assertIn("Hardware", msg)

    def test_inspect_image(self):
        d = tempfile.mkdtemp()
        img = inspect_image(make_image(os.path.join(d, "a.image"), "256", "08.50"))
        self.assertEqual((img.hw, img.vtuple, img.product), ("256", (8, 50), "FRITZ!Box 7530 AX"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
