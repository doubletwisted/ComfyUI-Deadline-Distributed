# ComfyUI Deadline Distributed

Deadline-aware distributed image generation for ComfyUI. This custom node lets one ComfyUI master coordinate local, remote, and Deadline-launched worker instances, then collect the rendered results back into the master workflow.

This repository is a Deadline-specific fork of a ComfyUI distributed rendering setup. It assumes your render farm network is controlled and firewalled, and keeps security lightweight: no password screens, no login flow, and no manual token setup.

## Demo

Watch the overview: [ComfyUI x Deadline: Leverage Your GPUs](https://youtu.be/NFmIvEoEPiU)

<p align="center">
  <a href="https://youtu.be/NFmIvEoEPiU">
    <img src="https://img.youtube.com/vi/NFmIvEoEPiU/maxresdefault.jpg" alt="ComfyUI x Deadline: Leverage Your GPUs" />
  </a>
</p>

## Requirements

- ComfyUI
- Thinkbox/AWS Deadline 10 with `deadlinecommand` available on the master machine
- [ComfyUI-Deadline-Plugin](https://github.com/doubletwisted/ComfyUI-Deadline-Plugin)
- A network where workers can reach the ComfyUI master HTTP port

## Installation

Install the base Deadline submission plugin first:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/doubletwisted/ComfyUI-Deadline-Plugin.git
```

Then install this distributed plugin:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/doubletwisted/ComfyUI-Deadline-Distributed.git
```

Restart ComfyUI after installing or updating either plugin.

## What It Adds

- Deadline worker claiming from the ComfyUI distributed panel
- Automatic Deadline worker registration and heartbeat tracking
- Distributed seed and batch helpers
- Distributed collector nodes for result gathering
- Distributed Ultimate SD Upscale support
- Deadline-named node aliases for cleaner Deadline workflows
- Passwordless request guards for worker callbacks and admin actions

## Basic Workflow

1. Start ComfyUI on the master machine.
2. Open the Distributed panel and configure the master address workers should use.
3. Claim Deadline workers from the panel, or run manually configured workers.
4. Add distributed nodes to your workflow, usually `DistributedSeed` and `DistributedCollector`.
5. Queue the workflow from the master.
6. Workers receive pruned workflow segments, process their part, and submit results back to the master.

For Deadline worker bootstrap workflows, this plugin provides `DeadlineWorkerRegistration`; the Deadline worker job uses it to register the worker process with the master automatically.

## Security Model

This plugin is designed for trusted render farms and private LANs, not for exposing ComfyUI directly to the public internet.

The current guardrails are intentionally lightweight:

- UI and admin endpoints reject cross-origin browser requests and require same-origin or private-network access.
- Each distributed job gets an auto-generated job token.
- Worker task/result/heartbeat endpoints require that job token while the job is active.
- Each Deadline worker claim gets an auto-generated registration token.
- Deadline workers must present that registration token when registering, heartbeating, and unregistering.
- The local instance token is stored in `gpu_config.json` but is not returned by `/distributed/config`.

There is no password UI and no token you need to copy. Tokens are generated, injected into hidden workflow inputs or Deadline job metadata, and discarded from runtime state when the job is cleaned up.

## Why Tokens Help

Deadline may open and close worker ports per job, but the master ComfyUI port stays up. During an active distributed job, the master accepts task requests and result submissions. Without a per-job token, any process that can reach the master could try to spoof a worker result, heartbeat, or registration.

The token does not replace network isolation. It adds a narrow proof that the caller is part of the current job or the current Deadline worker claim.

## Configuration

The plugin stores settings in `gpu_config.json`.

Relevant settings include:

- `settings.worker_result_wait_timeout`: how long the master waits without receiving a worker result
- `settings.max_batch`: maximum result batch size sent back to the master
- `settings.worker_heartbeat_grace_timeout`: how long a worker can miss heartbeats before requeue/fallback handling
- `security.require_private_network`: reject no-origin admin requests from non-private clients
- `security.allow_missing_origin_from_private_network`: allow tools or same-LAN clients that omit browser origin headers

Legacy `settings.worker_job_timeout` and `settings.heartbeat_timeout` keys are still accepted for existing configs.

Most users should leave the security settings at their defaults.

## Development Checks

The repository includes a lightweight GitHub Actions workflow in `.github/workflows/checks.yml`.

Local equivalents:

```bash
python - <<'PY'
import ast
from pathlib import Path

for path in Path(".").rglob("*.py"):
    if any(part in {".git", "__pycache__"} for part in path.parts):
        continue
    ast.parse(path.read_text(encoding="utf-8"))
PY
```

```bash
node --check web/executionUtils.js
node --check web/main.js
node --check web/ui.js
```

## Operational Notes

- Keep the master ComfyUI port firewalled to your workstation, render nodes, or VPN.
- Do not expose this plugin directly through a public tunnel unless you add a real authentication layer in front of it.
- If a Deadline worker cannot register, check that the worker can reach the master host and that the submitted Deadline job contains `COMFY_DISTRIBUTED_REGISTRATION_TOKEN`.
- If workers fail during tile/image processing, the master requeues or locally completes missing work where possible.

## License

Apache-2.0. See [LICENSE](LICENSE).
