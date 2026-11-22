# ALTAIR_GS_V3

## Backend:

```
cd ALTAIR_GS_V3
pip install -r requirements.txt
copy .env.example .env
python -m backend.main
```

Edit `.env` once with the ground-station ID and Grafana OTLP credentials.
The backend loads it automatically on every start. The real `.env` file is
ignored by Git, and values explicitly set in the shell take precedence.

## Frontend:
```
cd ALTAIR_GS_V3
npm install
npm run dev
```

## Sharing live telemetry with other ground stations (ZeroTier)

Every running GS streams the telemetry it decodes to other GS instances on
the same ZeroTier network — no public website needed.

- **Host** (the GS with the LR-900p radio): nothing to configure. Make sure
  TCP port 8000 is reachable on its ZeroTier interface, and share its
  ZeroTier IP (`zerotier-cli listnetworks`).
- **Viewers**: run the normal backend + frontend, then in **Settings → Peer
  Relay** press **AUTO**. The GS scans every address on its ZeroTier subnet
  for other ground stations and follows whichever one has live telemetry,
  rescanning if that host drops or goes quiet. **SCAN NETWORK** lists every
  GS found so you can pick one, or type an IP. The choice is remembered
  across restarts; `ALTAIR_PEER_HOST=auto` (or an IP) in `.env` sets the
  default.

Viewers run alarms, events, CSV logging, the map and telescope tracking
locally on the relayed packets. Telescope captures from the host (manual and
auto-capture) are mirrored into each viewer's gallery as
`<host-station>_<filename>.fits` — newest first, so if the link is slower than
auto-capture, viewers always see the latest frame and the backlog fills in
as bandwidth allows. The header shows `VIA <host>` while the relay
is the live source; a viewer's own radio or tunnel always takes priority if
it has one. Commands are never relayed back to the host.
