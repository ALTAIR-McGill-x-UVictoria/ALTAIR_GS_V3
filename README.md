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
- **Viewers**: run the normal backend + frontend, then either set
  `ALTAIR_PEER_HOST=<host-zerotier-ip>` in `.env`, or enter the IP under
  **Settings → Peer Relay** and press SUBSCRIBE (remembered across restarts).

Viewers run alarms, events, CSV logging, the map and telescope tracking
locally on the relayed packets. The header shows `VIA <host>` while the relay
is the live source; a viewer's own radio or tunnel always takes priority if
it has one. Commands are never relayed back to the host.
