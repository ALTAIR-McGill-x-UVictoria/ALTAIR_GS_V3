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
