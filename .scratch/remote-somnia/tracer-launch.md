# Remote Session Tracer: Local Launch

This tracer runs four processes. It is intentionally unauthenticated and must
remain on loopback during ticket 02 development.

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

## Start the tracer

Run each command in a separate terminal from the repository root.

1. Start the loopback-only Sidecar for the Project workspace:

   ```powershell
   somnia-sidecar --workspace D:\path\to\project --host 127.0.0.1 --port 8765
   ```

2. Start the stateless Relay:

   ```powershell
   somnia-relay --host 127.0.0.1 --port 8787
   ```

3. Start the outbound Connector:

   ```powershell
   somnia-connector --relay ws://127.0.0.1:8787 --device local-device --project default-project --sidecar http://127.0.0.1:8765
   ```

4. Serve the hosted web build:

   ```powershell
   cd desktop/ui
   npm run preview
   ```

Open <http://127.0.0.1:4173/?remote=1>. Keep the default Relay, Device, and
Project values, then select **Connect**. Create a Session and submit a prompt;
assistant output should appear incrementally before the completed Session is
reloaded from the Sidecar.

The `--device` and `--project` Connector values must match the values entered
in the browser. The Relay stores only live WebSocket references and does not
queue conversation content while a Device is offline.

## Automated tracer check

```powershell
python -m unittest tests.test_remote_tracer_e2e
cd desktop/ui
npm run test:e2e
```
