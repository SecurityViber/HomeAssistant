"""Export the current Home Assistant configuration into this repo.

Writes config/ (YAML files from the config folder), storage/ (UI-managed settings)
and private/ (git-ignored personal data). Credentials are never exported.
"""

import asyncio
import re
import shutil
from pathlib import PurePosixPath

from ha import HAError, HomeAssistant, redact
from snapshot import (
    CONFIG_DIR, CONFIG_EXCLUDE, CONFIG_TEXT_SUFFIXES, HELPER_DOMAINS, PRIVATE_DIR, STORAGE_DIR, compact, dump,
)

AREA_KEYS = ["area_id", "name", "floor_id", "icon", "aliases", "labels", "picture", "temperature_entity_id", "humidity_entity_id"]
DEVICE_KEYS = ["id", "name", "name_by_user", "manufacturer", "model", "integrations", "area_id", "labels", "disabled_by",
               "identifiers", "connections", "via_device_id"]
ENTITY_KEYS = ["entity_id", "id", "platform", "unique_id", "device_id", "original_name", "name", "icon", "aliases",
               "area_id", "labels", "categories", "device_class", "hidden_by", "disabled_by", "options"]
SECRET_LINE = re.compile(r"^\s*[\w-]*(password|token|api_key|secret|webhook_id)[\w-]*\s*:\s*(?!\s*!secret)\S", re.I | re.M)


async def export_config_files(ha):
    """Copy text files from the config folder, skipping CONFIG_EXCLUDE."""
    copied, warnings = [], []

    async def walk(rel):
        for entry in await ha.list_dir(rel):
            path = f"{rel}/{entry['name']}".lstrip("/")
            if path in CONFIG_EXCLUDE or entry["name"] in CONFIG_EXCLUDE:
                continue
            if entry["type"] == "dir":
                await walk(path)
            elif PurePosixPath(path).suffix in CONFIG_TEXT_SUFFIXES:
                text = await ha.read_file(path)
                if SECRET_LINE.search(text):
                    warnings.append(f"{path} contains a value that looks like a credential; move it to secrets.yaml")
                target = CONFIG_DIR / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
                copied.append(path)

    await walk("")
    return copied, warnings


async def export_storage(ha):
    cfg = await ha.call("get_config")
    unit_system = "metric" if cfg["unit_system"]["temperature"] == "°C" else "us_customary"
    core = compact(cfg, ["location_name", "country", "currency", "language", "time_zone", "elevation", "radius",
                         "internal_url", "external_url"])
    dump(STORAGE_DIR / "core.yaml", {**core, "unit_system": unit_system})
    dump(PRIVATE_DIR / "core.yaml", {"latitude": cfg["latitude"], "longitude": cfg["longitude"]})

    entries = await ha.call("config_entries/get")
    domain_of_entry = {e["entry_id"]: e["domain"] for e in entries}
    dump(STORAGE_DIR / "integrations.yaml", sorted(
        (compact(e, ["domain", "title", "entry_id", "source", "state", "disabled_by", "pref_disable_new_entities",
                     "pref_disable_polling"]) for e in entries),
        key=lambda e: (e["domain"], e.get("title", ""))))

    dump(STORAGE_DIR / "floors.yaml", [compact(f, ["floor_id", "name", "level", "icon", "aliases"])
                                       for f in await ha.call("config/floor_registry/list")])
    dump(STORAGE_DIR / "labels.yaml", [compact(l, ["label_id", "name", "color", "icon", "description"])
                                       for l in await ha.call("config/label_registry/list")])
    categories = {}
    for scope in ("automation", "script", "scene", "helpers"):
        items = await ha.call("config/category_registry/list", scope=scope)
        if items:
            categories[scope] = [compact(c, ["category_id", "name", "icon"]) for c in items]
    dump(STORAGE_DIR / "categories.yaml", categories)
    dump(STORAGE_DIR / "areas.yaml", sorted((compact(a, AREA_KEYS) for a in await ha.call("config/area_registry/list")),
                                            key=lambda a: a["name"]))

    devices = []
    for d in await ha.call("config/device_registry/list"):
        d["integrations"] = sorted({domain_of_entry[e] for e in d["config_entries"] if e in domain_of_entry})
        devices.append(compact(d, DEVICE_KEYS))
    dump(STORAGE_DIR / "devices.yaml", sorted(devices, key=lambda d: (d.get("integrations", [""])[0], d.get("name") or "")))

    entity_ids = [e["entity_id"] for e in await ha.call("config/entity_registry/list")]
    details = await ha.call("config/entity_registry/get_entries", entity_ids=entity_ids)
    dump(STORAGE_DIR / "entities.yaml", sorted((compact(e, ENTITY_KEYS) for e in details.values() if e),
                                               key=lambda e: e["entity_id"]))

    helpers = {}
    for domain in HELPER_DOMAINS:
        items = await ha.call(f"{domain}/list")
        if items:
            helpers[domain] = items
    # Zones are coordinates of places you visit, so they stay out of the public repo.
    dump(PRIVATE_DIR / "zones.yaml", helpers.pop("zone", []))
    dump(STORAGE_DIR / "helpers.yaml", helpers)
    dump(STORAGE_DIR / "persons.yaml", (await ha.call("person/list"))["storage"])
    users = [compact(u, ["id", "name", "username", "is_owner", "is_active", "local_only", "group_ids"])
             for u in await ha.call("config/auth/list") if not u["system_generated"]]
    dump(PRIVATE_DIR / "users.yaml", users)

    dashboards = await ha.call("lovelace/dashboards/list")
    dump(STORAGE_DIR / "dashboards.yaml", [compact(d, ["url_path", "title", "icon", "mode", "require_admin",
                                                       "show_in_sidebar"]) for d in dashboards])
    for url_path in [None] + [d["url_path"] for d in dashboards if d["mode"] == "storage"]:
        try:
            config = await ha.call("lovelace/config", url_path=url_path)
        except HAError as err:
            if err.code != "config_not_found":
                raise
            continue  # auto-generated dashboard, nothing to store
        dump(STORAGE_DIR / "dashboards" / f"{url_path or 'default'}.yaml", config)
    dump(STORAGE_DIR / "dashboard_resources.yaml", [compact(r, ["type", "url"]) for r in await ha.call("lovelace/resources")])

    exposed = (await ha.call("homeassistant/expose_entity/list"))["exposed_entities"]
    pipelines = await ha.call("assist_pipeline/pipeline/list")
    dump(STORAGE_DIR / "voice.yaml", {
        "exposed_entities": {e: exposed[e] for e in sorted(exposed)},
        "preferred_pipeline": pipelines["preferred_pipeline"],
        "pipelines": pipelines["pipelines"],
    })

    try:
        energy = await ha.call("energy/get_prefs")
        dump(STORAGE_DIR / "energy.yaml", energy)
    except HAError as err:
        if err.code != "not_found":
            raise

    hacs = await ha.call("hacs/repositories/list")
    dump(STORAGE_DIR / "hacs.yaml", sorted(({"repository": r["full_name"], "category": r["category"],
                                             "version": r["installed_version"]} for r in hacs if r.get("installed")),
                                           key=lambda r: r["repository"]))


async def export_supervisor(ha):
    core, supervisor, os_info = [await ha.supervisor(p) for p in ("/core/info", "/supervisor/info", "/os/info")]
    dump(STORAGE_DIR / "system.yaml", {
        "core": core["version"],
        "supervisor": supervisor["version"],
        "channel": supervisor["channel"],
        "os": os_info["version"],
        "board": os_info["board"],
    })
    repos = (await ha.supervisor("/store/repositories"))
    addons = []
    for addon in (await ha.supervisor("/addons"))["addons"]:
        info = await ha.supervisor(f"/addons/{addon['slug']}/info")
        addons.append(compact(redact(info), ["name", "slug", "repository", "version", "state", "boot", "auto_update",
                                             "watchdog", "network", "options"]))
    dump(STORAGE_DIR / "addons.yaml", {
        "repositories": [compact(r, ["name", "slug", "source"]) for r in repos if r["source"] not in ("core", "local")],
        "addons": addons,
    })


async def export_images(ha):
    for image in await ha.call("image/list"):
        data = ha.download(f"/api/image/serve/{image['id']}/original")
        target = PRIVATE_DIR / "images" / f"{image['id']}-{image.get('name') or 'original'}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


async def main():
    for folder in (CONFIG_DIR, STORAGE_DIR, PRIVATE_DIR):
        shutil.rmtree(folder, ignore_errors=True)
    async with HomeAssistant() as ha:
        print(f"Connected to Home Assistant {ha.version} at {ha.url}")
        await export_storage(ha)
        await export_supervisor(ha)
        await export_images(ha)
        copied, warnings = await export_config_files(ha)
    print(f"Copied {len(copied)} config files: {', '.join(copied)}")
    for warning in warnings:
        print(f"WARNING: {warning}")


if __name__ == "__main__":
    asyncio.run(main())
