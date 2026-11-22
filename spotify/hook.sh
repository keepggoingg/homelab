#!/bin/bash
# spotifyd song-change hook: dump the player event + track metadata for screenctl (/api/spotify).
exec python3 -I - <<"PY"
import json, os, time
keep = {k: v for k, v in os.environ.items()
        if k in ("PLAYER_EVENT", "TRACK_ID", "POSITION_MS", "DURATION_MS", "VOLUME")
        or k.startswith(("TRACK_", "ARTIST", "ALBUM", "COVER", "NAME", "COVERS"))}
keep["ts"] = time.time()
path = f"/run/user/{os.getuid()}/spotify.json"
with open(path + ".tmp", "w") as f:
    json.dump(keep, f)
os.replace(path + ".tmp", path)
with open(f"/run/user/{os.getuid()}/spotify-events.log", "a") as f:
    f.write(json.dumps(keep) + "\n")
PY
