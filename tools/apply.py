"""Apply the configuration snapshot in this repo to a Home Assistant instance.

Dry run by default: prints what would change. Pass --apply to make the changes.
Integrations, add-ons, HACS, users and secrets are not applied (see README); the
script lists what is missing so you can set it up by hand first.

Device, entity and image IDs differ on a fresh install. They are matched by their
stable hardware/integration identifiers and remapped in automations, scenes and
dashboards automatically.
"""

import argparse
import asyncio
import json
import mimetypes

from ha import HAError, HomeAssistant
from snapshot import CONFIG_DIR, PRIVATE_DIR, STORAGE_DIR, compact, load

SECTIONS = ["core", "images", "registries", "helpers", "devices", "entities", "files", "dashboards", "voice", "energy"]
LIST_FIELDS = {"aliases", "labels"}
REMAPPED_FILES = {"automations.yaml", "scripts.yaml", "scenes.yaml"}


class Context:
    def __init__(self, ha, apply, only):
        self.ha = ha
        self.apply = apply
        self.only = only
        self.section = None
        self.ids = {}  # snapshot id -> live id
        self.changes = 0
        self.failures = 0

    async def step(self, description, action):
        """Print a change and, with --apply, run it. Returns the action's result."""
        if self.section not in self.only:
            return None
        self.changes += 1
        if not self.apply:
            print(f"  would {description}")
            return None
        try:
            result = await action()
        except Exception as err:  # keep going; report every failure at the end
            self.failures += 1
            print(f"  FAILED {description}: {err}")
            return None
        print(f"  {description}")
        return result

    def remap(self, value):
        """Replace snapshot IDs with the matching live IDs anywhere inside `value`."""
        if not self.ids or value is None:
            return value
        text = json.dumps(value)
        for old, new in self.ids.items():
            # Long random IDs can be replaced anywhere; short slugs only as whole values.
            text = text.replace(old, new) if len(old) >= 26 else text.replace(json.dumps(old), json.dumps(new))
        return json.loads(text)

    def remap_text(self, text):
        for old, new in self.ids.items():
            if len(old) >= 26:
                text = text.replace(old, new)
        return text


def payload(item, fields):
    return {f: item.get(f, [] if f in LIST_FIELDS else None) for f in fields}


def changed_fields(want, live, fields):
    want, live = compact(want, fields), compact(live, fields)
    return [f for f in fields if want.get(f) != live.get(f)]


async def sync_named(ctx, label, want_items, live_items, id_key, fields, create_cmd, update_cmd, **extra):
    """Create or update registry items (floors, labels, areas, ...) matched by id, then by name."""
    by_id = {x[id_key]: x for x in live_items}
    by_name = {x["name"]: x for x in live_items}
    for item in want_items:
        want = ctx.remap(payload(item, fields))
        current = by_id.get(item[id_key]) or by_name.get(item["name"])
        if current is None:
            created = await ctx.step(f"create {label} '{item['name']}'", lambda: ctx.ha.call(
                create_cmd, **extra, **{k: v for k, v in want.items() if v not in (None, [])}))
            if created:
                ctx.ids[item[id_key]] = created[id_key]
            continue
        if current[id_key] != item[id_key]:
            ctx.ids[item[id_key]] = current[id_key]
        diff = changed_fields(want, current, fields)
        if diff:
            await ctx.step(f"update {label} '{item['name']}': {', '.join(diff)}", lambda: ctx.ha.call(
                update_cmd, **extra, **{id_key: current[id_key]}, **{f: want[f] for f in diff}))


async def apply_core(ctx):
    want = {**load(STORAGE_DIR / "core.yaml", {}), **load(PRIVATE_DIR / "core.yaml", {})}
    live = await ctx.ha.call("get_config")
    live["unit_system"] = "metric" if live["unit_system"]["temperature"] == "°C" else "us_customary"
    diff = {k: v for k, v in want.items() if live.get(k) != v}
    if diff:
        await ctx.step(f"update core settings: {', '.join(diff)}", lambda: ctx.ha.call("config/core/update", **diff))


async def apply_images(ctx):
    folder = PRIVATE_DIR / "images"
    if not folder.exists():
        return
    live = {i["id"] for i in await ctx.ha.call("image/list")}
    for path in sorted(folder.iterdir()):
        old_id, _, name = path.name.partition("-")
        if old_id in live:
            continue
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        created = await ctx.step(f"upload image '{name}'", lambda: asyncio.to_thread(
            ctx.ha.upload_image, name, path.read_bytes(), content_type))
        if created:
            ctx.ids[old_id] = created["id"]


async def apply_registries(ctx):
    ha = ctx.ha
    await sync_named(ctx, "floor", load(STORAGE_DIR / "floors.yaml", []), await ha.call("config/floor_registry/list"),
                     "floor_id", ["name", "level", "icon", "aliases"],
                     "config/floor_registry/create", "config/floor_registry/update")
    await sync_named(ctx, "label", load(STORAGE_DIR / "labels.yaml", []), await ha.call("config/label_registry/list"),
                     "label_id", ["name", "color", "icon", "description"],
                     "config/label_registry/create", "config/label_registry/update")
    for scope, items in load(STORAGE_DIR / "categories.yaml", {}).items():
        await sync_named(ctx, f"{scope} category", items, await ha.call("config/category_registry/list", scope=scope),
                         "category_id", ["name", "icon"],
                         "config/category_registry/create", "config/category_registry/update", scope=scope)
    await sync_named(ctx, "area", load(STORAGE_DIR / "areas.yaml", []), await ha.call("config/area_registry/list"),
                     "area_id", ["name", "floor_id", "icon", "aliases", "labels", "picture",
                                 "temperature_entity_id", "humidity_entity_id"],
                     "config/area_registry/create", "config/area_registry/update")


async def apply_helpers(ctx):
    helpers = load(STORAGE_DIR / "helpers.yaml", {})
    helpers["zone"] = load(PRIVATE_DIR / "zones.yaml", [])
    for domain, items in helpers.items():
        live = {x["id"]: x for x in await ctx.ha.call(f"{domain}/list")}
        for item in items:
            fields = {k: v for k, v in item.items() if k != "id"}
            current = live.get(item["id"])
            if current is None:
                # Tag IDs are the physical NFC tag IDs, so they must be kept.
                keep_id = {"tag_id": item["id"]} if domain == "tag" else {}
                await ctx.step(f"create {domain} '{item.get('name', item['id'])}'",
                               lambda: ctx.ha.call(f"{domain}/create", **keep_id, **fields))
            elif {k: v for k, v in current.items() if k != "id"} != fields:
                await ctx.step(f"update {domain} '{item.get('name', item['id'])}'",
                               lambda: ctx.ha.call(f"{domain}/update", **{f"{domain}_id": item["id"]}, **fields))


def user_managed(want, live):
    """hidden_by/disabled_by can only be changed when neither side was set by an integration."""
    return want in (None, "user") and live in (None, "user")


async def apply_devices(ctx):
    live = await ctx.ha.call("config/device_registry/list")
    index = {}
    for d in live:
        for ident in d["identifiers"]:
            index[("id", *ident)] = d
        for conn in d["connections"]:
            index[("conn", *conn)] = d
    missing = []
    for item in load(STORAGE_DIR / "devices.yaml", []):
        keys = [("id", *i) for i in item.get("identifiers", [])] + [("conn", *c) for c in item.get("connections", [])]
        current = next((index[k] for k in keys if k in index), None)
        if current is None:
            missing.append(item.get("name_by_user") or item.get("name") or item["id"])
            continue
        if current["id"] != item["id"]:
            ctx.ids[item["id"]] = current["id"]
        want = ctx.remap(payload(item, ["name_by_user", "area_id", "labels", "disabled_by"]))
        fields = ["name_by_user", "area_id", "labels"]
        if user_managed(want["disabled_by"], current["disabled_by"]):
            fields.append("disabled_by")
        diff = changed_fields(want, current, fields)
        if diff:
            await ctx.step(f"update device '{current.get('name_by_user') or current['name']}': {', '.join(diff)}",
                           lambda: ctx.ha.call("config/device_registry/update", device_id=current["id"],
                                               **{f: want[f] for f in diff}))
    if missing and ctx.section in ctx.only:
        print(f"  {len(missing)} devices are not in Home Assistant yet (set up their integrations first): "
              f"{', '.join(sorted(missing))}")


async def apply_entities(ctx):
    ha = ctx.ha
    entity_ids = [e["entity_id"] for e in await ha.call("config/entity_registry/list")]
    live = [e for e in (await ha.call("config/entity_registry/get_entries", entity_ids=entity_ids)).values() if e]
    # Entities are unique per (domain, platform, unique_id); the domain never changes on rename.
    index = {(e["entity_id"].split(".")[0], e["platform"], e["unique_id"]): e for e in live}
    missing = 0
    for item in load(STORAGE_DIR / "entities.yaml", []):
        current = index.get((item["entity_id"].split(".")[0], item["platform"], item["unique_id"]))
        if current is None:
            missing += 1
            continue
        if current["id"] != item["id"]:
            ctx.ids[item["id"]] = current["id"]
        want = ctx.remap(payload(item, ["entity_id", "name", "icon", "aliases", "area_id", "labels", "categories",
                                        "device_class", "hidden_by", "disabled_by"]))
        fields = ["entity_id", "name", "icon", "aliases", "area_id", "labels", "device_class"]
        fields += [f for f in ("hidden_by", "disabled_by") if user_managed(want[f], current[f])]
        diff = changed_fields(want, current, fields)
        update = {f: want[f] for f in diff}
        if "entity_id" in update:
            update["new_entity_id"] = update.pop("entity_id")
        want_categories = want["categories"] or {}
        live_categories = current.get("categories") or {}
        if want_categories != live_categories:
            diff.append("categories")
            update["categories"] = {scope: want_categories.get(scope) for scope in {*want_categories, *live_categories}}
        if diff:
            await ctx.step(f"update entity {current['entity_id']}: {', '.join(diff)}",
                           lambda: ha.call("config/entity_registry/update", entity_id=current["entity_id"], **update))
        # Entity options set by the user (display precision, units, ...). Exposure is handled in "voice".
        entity_id = want["entity_id"]
        for domain, options in (item.get("options") or {}).items():
            if "." in domain or domain == "conversation" or (current.get("options") or {}).get(domain) == options:
                continue
            await ctx.step(f"update {domain} options of {entity_id}",
                           lambda: ha.call("config/entity_registry/update", entity_id=entity_id,
                                           options_domain=domain, options=options))
    if missing and ctx.section in ctx.only:
        print(f"  {missing} entities are not in Home Assistant yet (they appear once their integrations are set up)")


async def apply_files(ctx):
    ha = ctx.ha
    previous = {}
    for path in sorted(p for p in CONFIG_DIR.rglob("*") if p.is_file()):
        rel = path.relative_to(CONFIG_DIR).as_posix()
        text = path.read_text()
        if rel in REMAPPED_FILES:
            text = ctx.remap_text(text)
        live = await ha.read_file(rel)
        if live == text:
            continue
        if await ctx.step(f"{'write' if live is not None else 'create'} {rel}", lambda: ha.write_file(rel, text)):
            previous[rel] = live
    if not previous:
        return
    check = await asyncio.to_thread(ha.rest, "POST", "/config/core/check_config")
    if check["result"] != "valid":
        print(f"  Configuration check FAILED, restoring the previous files:\n{check.get('errors')}")
        for rel, text in previous.items():
            await ha.write_file(rel, text if text is not None else "")
        ctx.failures += 1
        return
    await asyncio.to_thread(ha.rest, "POST", "/services/homeassistant/reload_all", {})
    print("  Configuration is valid and reloaded. Restart Home Assistant if configuration.yaml changed "
          "parts that cannot be reloaded.")


async def apply_dashboards(ctx):
    ha = ctx.ha
    fields = ["title", "icon", "require_admin", "show_in_sidebar"]
    live = {d["url_path"]: d for d in await ha.call("lovelace/dashboards/list")}
    for item in load(STORAGE_DIR / "dashboards.yaml", []):
        current = live.get(item["url_path"])
        if current is None:
            await ctx.step(f"create dashboard '{item['title']}'", lambda: ha.call(
                "lovelace/dashboards/create", url_path=item["url_path"], mode="storage",
                **{k: v for k, v in payload(item, fields).items() if v is not None}))
        elif diff := changed_fields(item, current, fields):
            await ctx.step(f"update dashboard '{item['title']}': {', '.join(diff)}", lambda: ha.call(
                "lovelace/dashboards/update", dashboard_id=current["id"], **{f: item.get(f) for f in diff}))
    for path in sorted((STORAGE_DIR / "dashboards").glob("*.yaml")):
        url_path = None if path.stem == "default" else path.stem
        want = ctx.remap(load(path))
        try:
            current = await ha.call("lovelace/config", url_path=url_path)
        except HAError as err:
            if err.code != "config_not_found":
                raise
            current = None
        if current != want:
            await ctx.step(f"save dashboard config '{path.stem}'",
                           lambda: ha.call("lovelace/config/save", url_path=url_path, config=want))
    live_resources = {r["url"] for r in await ha.call("lovelace/resources")}
    for res in load(STORAGE_DIR / "dashboard_resources.yaml", []):
        if res["url"] not in live_resources:
            await ctx.step(f"add dashboard resource {res['url']}", lambda: ha.call(
                "lovelace/resources/create", res_type=res["type"], url=res["url"]))


async def apply_voice(ctx):
    ha = ctx.ha
    want = load(STORAGE_DIR / "voice.yaml", {}).get("exposed_entities", {})
    live = (await ha.call("homeassistant/expose_entity/list"))["exposed_entities"]
    existing = {s["entity_id"] for s in await ha.call("get_states")}
    groups = {}
    for entity_id, assistants in want.items():
        if entity_id not in existing:
            continue
        for assistant, expose in assistants.items():
            if live.get(entity_id, {}).get(assistant) != expose:
                groups.setdefault((assistant, expose), []).append(entity_id)
    for (assistant, expose), entity_ids in groups.items():
        await ctx.step(f"{'expose' if expose else 'unexpose'} {len(entity_ids)} entities for {assistant}",
                       lambda: ha.call("homeassistant/expose_entity", assistants=[assistant],
                                       entity_ids=entity_ids, should_expose=expose))


async def apply_energy(ctx):
    want = load(STORAGE_DIR / "energy.yaml")
    if not want:
        return
    try:
        current = await ctx.ha.call("energy/get_prefs")
    except HAError:
        current = None
    if current != want:
        await ctx.step("save energy dashboard settings", lambda: ctx.ha.call("energy/save_prefs", **want))


async def report_manual_steps(ha):
    """List what the snapshot has but Home Assistant is missing and this script cannot create."""
    live_entries = {(e["domain"], e["title"]) for e in await ha.call("config_entries/get")}
    integrations = [f"{e['domain']} ({e.get('title', '')})" for e in load(STORAGE_DIR / "integrations.yaml", [])
                    if (e["domain"], e.get("title", "")) not in live_entries]
    addon_snapshot = load(STORAGE_DIR / "addons.yaml", {})
    live_addons = {a["slug"] for a in (await ha.supervisor("/addons"))["addons"]}
    addons = [f"{a['name']} ({a['slug']})" for a in addon_snapshot.get("addons", []) if a["slug"] not in live_addons]
    try:
        live_hacs = {r["full_name"] for r in await ha.call("hacs/repositories/list") if r.get("installed")}
    except HAError:
        live_hacs = set()
    hacs = [r["repository"] for r in load(STORAGE_DIR / "hacs.yaml", []) if r["repository"] not in live_hacs]
    for title, items in (("Integrations", integrations), ("Add-ons", addons), ("HACS repositories", hacs)):
        if items:
            print(f"\n{title} to set up by hand (see README):")
            for item in items:
                print(f"  - {item}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    parser.add_argument("--only", help=f"comma-separated sections to apply: {', '.join(SECTIONS)}")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else set(SECTIONS)
    if unknown := only - set(SECTIONS):
        parser.error(f"unknown section(s): {', '.join(sorted(unknown))}")

    async with HomeAssistant() as ha:
        ctx = Context(ha, args.apply, only)
        print(f"{'Applying to' if args.apply else 'Dry run against'} Home Assistant {ha.version} at {ha.url}")
        handlers = {
            "core": apply_core, "images": apply_images, "registries": apply_registries, "helpers": apply_helpers,
            "devices": apply_devices, "entities": apply_entities, "files": apply_files,
            "dashboards": apply_dashboards, "voice": apply_voice, "energy": apply_energy,
        }
        for section in SECTIONS:
            # devices and entities always run so their IDs can be remapped for later sections.
            if section in only or section in ("devices", "entities"):
                ctx.section = section
                if section in only:
                    print(f"[{section}]")
                await handlers[section](ctx)
        await report_manual_steps(ha)

    verb = "changes made" if args.apply else "changes to make"
    print(f"\n{ctx.changes} {verb}" + (f", {ctx.failures} failed" if ctx.failures else "")
          + ("" if args.apply or not ctx.changes else " (re-run with --apply to make them)"))
    raise SystemExit(1 if ctx.failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
