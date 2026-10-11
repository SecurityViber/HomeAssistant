# HomeAssistant

Configuration snapshot of my Home Assistant (Home Assistant Green, `homeassistant.local:8123`), plus scripts to refresh it and to apply it again, to this instance or to a freshly installed one.

> **This repo is public.** Credentials are never exported, and personal data (home coordinates, zones, users, uploaded photos) goes to `private/`, which is git-ignored. Check `git diff` before every commit anyway.

## What's in here

| Path | Contents |
| --- | --- |
| `config/` | Copy of the YAML files in the Home Assistant config folder: `configuration.yaml`, `automations.yaml`, `scenes.yaml`, `scripts.yaml`, blueprints, themes. `secrets.yaml` is never copied. |
| `storage/` | Settings managed through the UI, exported through the API: core settings, areas, floors, labels, categories, devices, entities (names, icons, areas, labels, aliases), helpers and tags, persons, dashboards and their resources, voice-assistant exposure, the list of integrations, add-ons (credentials redacted), HACS repositories and system versions. |
| `private/` | Git-ignored. Home coordinates, zones, users and uploaded images (dashboard pictures). Only exists on the machine that ran the export. |
| `tools/` | `export.py` (Home Assistant to repo), `apply.py` (repo to Home Assistant) and their shared helpers. |

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export HA_TOKEN=...   # long-lived access token (Profile → Security), already in ~/.zshrc
export HA_URL=http://homeassistant.local:8123   # optional, this is the default
```

The **File editor** add-on must be installed and running. The scripts use it to read and write the files in the config folder.

## Refresh the snapshot

```sh
.venv/bin/python tools/export.py
git diff   # review, then commit
```

The export replaces `config/`, `storage/` and `private/` completely, so anything deleted in Home Assistant disappears here too. It warns if a copied file seems to contain a credential that should live in `secrets.yaml`.

## Apply the snapshot

```sh
.venv/bin/python tools/apply.py                      # dry run: lists what would change
.venv/bin/python tools/apply.py --apply              # make the changes
.venv/bin/python tools/apply.py --only files,dashboards --apply
```

Sections, applied in this order: `core`, `images`, `registries` (floors, labels, categories, areas), `helpers`, `devices`, `entities`, `files`, `dashboards`, `voice`, `energy`.

- Device, entity and image IDs are different on a fresh install. Devices are matched by their hardware/integration identifiers and entities by their unique ID, and every reference in automations, scenes and dashboards is rewritten to the new IDs.
- Before reloading, the `files` section runs Home Assistant's configuration check. If the check fails, it puts the previous files back.
- Run it again after setting up more integrations. Devices and entities that don't exist yet are skipped and reported.

## Not covered: set these up by hand

`apply.py` lists which of these are missing at the end of every run.

1. **Integrations** (`storage/integrations.yaml`). They need logins or pairing, so add them in Settings → Devices & services. Most are discovered automatically: Hue, Sonos, Apple TV/HomePods, the HP printer, Synology DSM, Matter and Thread. The HomeKit bridges, MQTT and HACS have to be added manually. Their options (for example, which entities each HomeKit bridge includes) are not exported.
2. **Add-ons** (`storage/addons.yaml`). Add the repositories listed there, install the add-ons and copy the options. Fill in the `<redacted>` values yourself.
3. **HACS** (`storage/hacs.yaml`). Install HACS, then the repositories listed.
4. **Users and persons**. Create the users, then link them to the persons in `storage/persons.yaml`.
5. **`secrets.yaml`, integration credentials and the Zigbee network** (Zigbee2MQTT/ZHA coordinator and paired devices). These are only in Home Assistant's own backups (Settings → System → Backups). Keep an up-to-date backup somewhere safe and **never in this repo**. A backup restores everything at once; this repo is for seeing what changed, reviewing config, and rebuilding selectively.
