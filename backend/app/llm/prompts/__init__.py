from functools import lru_cache
from html import escape
from pathlib import Path


@lru_cache
def load(name):
    return Path(__file__).with_name(name + ".md").read_text(encoding="utf-8")


def block(name, text):
    return f"<{name}>\n{escape(text, quote=False)}\n</{name}>"
