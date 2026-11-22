#!/usr/bin/env python3
"""HomeKit TV accessory for the homelab built-in display.

Shows up in the Apple Home app as a TV:
  power        -> screen on/off
  inputs       -> Dashboard / Fireplace / YouTube
  Remote (Control Centre) -> arrows/select/back/play-pause via screenctl
Talks to screenctl on 127.0.0.1:8090. Pairing state lives in ~/.local/state/homekit.
"""
import json, logging, os, signal, urllib.request
from pathlib import Path

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_TELEVISION

API = "http://127.0.0.1:8090/api/"
STATE_DIR = Path.home() / ".local/state/homekit"
INPUTS = [("Dashboard", "dashboard"), ("Fireplace", "fireplace"), ("YouTube", "youtube")]
# HAP RemoteKey values -> screenctl keys
REMOTE = {4: "up", 5: "down", 6: "left", 7: "right", 8: "select", 9: "back", 10: "home", 11: "playpause", 15: "home"}

log = logging.getLogger("homekit")


def api(path, method="POST"):
    req = urllib.request.Request(API + path, method=method, data=b"" if method == "POST" else None)
    return json.load(urllib.request.urlopen(req, timeout=10))


class Display(Accessory):
    category = CATEGORY_TELEVISION

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.set_info_service(manufacturer="homelab", model="Mi Laptop Pro 15",
                              serial_number=os.environ.get("HL_HOST", "homelab"), firmware_revision="1.0")
        tv = self.add_preload_service("Television", ["Name", "ConfiguredName", "Active",
                                                     "ActiveIdentifier", "RemoteKey", "SleepDiscoveryMode"])
        tv.configure_char("Name", value="Laptop")
        tv.configure_char("ConfiguredName", value="Laptop")
        tv.configure_char("SleepDiscoveryMode", value=1)
        self.active = tv.configure_char("Active", value=1, setter_callback=self.set_active)
        self.input = tv.configure_char("ActiveIdentifier", value=1, setter_callback=self.set_input)
        tv.configure_char("RemoteKey", setter_callback=self.remote_key)
        for i, (name, _) in enumerate(INPUTS, 1):
            src = self.add_preload_service("InputSource", ["Name", "Identifier"])
            src.configure_char("Name", value=name)
            src.configure_char("Identifier", value=i)
            src.configure_char("ConfiguredName", value=name)
            src.configure_char("InputSourceType", value=10)  # application
            src.configure_char("IsConfigured", value=1)
            src.configure_char("CurrentVisibilityState", value=0)
            tv.add_linked_service(src)

    def set_active(self, value):
        api("screen/" + ("on" if value else "off"))

    def set_input(self, value):
        api("mode/" + INPUTS[value - 1][1])

    def remote_key(self, value):
        if value in REMOTE:
            api("key/" + REMOTE[value])

    @Accessory.run_at_interval(5)
    async def run(self):
        """Mirror changes made elsewhere (dashboard buttons, web remote) into Home."""
        try:
            s = api("stats", "GET")["state"]
        except Exception as e:
            log.debug("stats failed: %s", e)
            return
        active = 1 if s["screen"] == "on" else 0
        ident = next((i for i, (_, m) in enumerate(INPUTS, 1) if m == s["mode"]), 1)
        if self.active.value != active:
            self.active.set_value(active)
        if self.input.value != ident:
            self.input.set_value(ident)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    driver = AccessoryDriver(port=51826, persist_file=str(STATE_DIR / "accessory.state"))
    driver.add_accessory(Display(driver, "Laptop"))
    signal.signal(signal.SIGTERM, driver.signal_handler)
    driver.start()
