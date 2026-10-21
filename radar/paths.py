"""Project paths and config loading."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache")
SITE = os.path.join(ROOT, "site")
TEMPLATES = os.path.join(ROOT, "templates")


def load(name):
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


CFG = load("config.json")
