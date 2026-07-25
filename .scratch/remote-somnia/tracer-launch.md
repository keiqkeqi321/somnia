# Remote Somnia: Local Launch

The authenticated local stack keeps the Sidecar on loopback and persists only
administrator and Device identity metadata in the Relay database.

## Prerequisites

From the repository root:

```powershell
pip install -e .
cd desktop/ui
npm install
npx playwright install chromium
npm run build
cd ../..
```

The selected workspace must have a working Somnia provider configuration.

## Start and pair

Run long-lived commands in separate terminals from the repository root.

1. Start the authenticated Relay with a local metadata database:

   ```powershell
   $env:SOMNIA_ADMIN_USERNAME = "admin"
   $env:SOMNIA_ADMIN_PASSWORD = "replace-with-a-long-password"
   $env:SOMNIA_RELAY_DATABASE_URL = "sqlite:///.scratch/remote-somnia/relay.db"
   somnia-relay --host 127.0.0.1 --port 8787
   ```

2. Serve the hosted web build:

   ```powershell
   cd desktop/ui
   npm run preview
   ```

3. Open <http://127.0.0.1:4173/?remote=1>, sign in, enter the new Device name,
   and create a pairing code.

4. Pair this computer before the code expires:

   ```powershell
   somnia-connector pair --relay http://127.0.0.1:8787 --code ABCDEFG234
   ```

5. Start the loopback-only Sidecar for the Project workspace:

   ```powershell
   somnia-sidecar --workspace D:\path\to\project --host 127.0.0.1 --port 8765
   ```

6. Start the outbound authenticated Connector:

   ```powershell
   somnia-connector run --project default-project --sidecar http://127.0.0.1:8765
   ```

Refresh the Device list by signing in again, select the paired Device, connect,
and create a Session. Assistant output should stream before the authoritative
completed Session is reloaded from the Sidecar.

The default Device identity is stored at
`~/.open_somnia/remote/device-identity.json`. It contains the Device private key
and must remain on that computer. Revoking the Device in the Web interface
disconnects it immediately and permanently rejects that key.

For the production deployment, use a PostgreSQL URL such as
`postgresql+psycopg://user:password@database/somnia` and pass
`somnia-relay --web-origin https://somnia.top --secure-cookies`. DNS and TLS
for `somnia.top` must terminate at the reverse proxy, which forwards the Web
application, HTTP API, and WSS traffic to loopback services.

Set `SOMNIA_ENV=production` and inject a generated 32-byte URL-safe Base64
`SOMNIA_RELAY_SECRET_KEY` through the service secret manager. For example:

```powershell
$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:SOMNIA_RELAY_SECRET_KEY = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
```

Do not place the secret in command-line arguments, repository files, or logs.

## Automated checks

```powershell
python -m unittest tests.test_remote_auth tests.test_remote_tracer_e2e
cd desktop/ui
npm run test:e2e
```
