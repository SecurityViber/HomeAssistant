"""Layout and YAML helpers for the configuration snapshot stored in this repo."""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "config"    # mirror of the Home Assistant config folder (YAML files)
STORAGE_DIR = REPO / "storage"  # UI-managed settings exported through the API
PRIVATE_DIR = REPO / "private"  # git-ignored: location, users, uploaded images

# Paths in the config folder that are never exported: credentials, databases, caches,
# and folders that integrations or HACS manage themselves.
CONFIG_EXCLUDE = {
    ".storage", ".cloud", ".cache", "deps", "tts", "__pycache__", "backups",
    "secrets.yaml", "custom_components", "zigbee2mqtt", "image", "www/community",
}
CONFIG_TEXT_SUFFIXES = {".yaml", ".yml", ".jinja", ".json", ".css", ".js", ".md", ".txt"}

HELPER_DOMAINS = [
    "input_boolean", "input_number", "input_select", "input_text", "input_datetime",
    "input_button", "counter", "timer", "schedule", "zone", "tag",
]


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_presenter)


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120))


def load(path, default=None):
    if not path.exists():
        return default
    return yaml.safe_load(path.read_text()) or default


def compact(item, keys):
    """Pick `keys` from a dict, dropping empty values so the YAML stays readable."""
    out = {}
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            out[key] = sorted(value, key=lambda v: (v is None, v or "")) if isinstance(value, list) and key in ("labels", "aliases") else value
    return out
