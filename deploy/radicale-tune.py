"""Apply runtime tunings to the installed Radicale package.

A few thousand events miss a ten-minute import when every new PUT scans the
whole collection. The private fsync flag also cannot be set from the config
file, and history cleanup scans every cached name on every write.
"""

from pathlib import Path

import radicale

ROOT = Path(radicale.__file__).resolve().parent

_HAS_UID = '''    def has_uid(self, uid: str) -> bool:
        """Check if a UID exists in the collection."""
        for item in self.get_all():
            if item.uid == uid:
                return True
        return False
'''

_HAS_UID_CACHED = '''    def has_uid(self, uid: str) -> bool:
        """Check if a UID exists in the collection."""
        # Remember UIDs on the storage object. Scanning every item on every
        # new PUT makes a large collection quadratic.
        store = getattr(self, "_storage", None)
        if store is None:
            for item in self.get_all():
                if item.uid == uid:
                    return True
            return False
        remembered = getattr(store, "_calendar_hub_uids", None)
        if remembered is None:
            remembered = {}
            store._calendar_hub_uids = remembered
        known = remembered.get(self.path)
        if known is None:
            known = {item.uid for item in self.get_all() if item.uid}
            remembered[self.path] = known
        return uid in known
'''

_UPLOAD_RETURN = """        uploaded_item = self._get(href, verify_href=False)
        if uploaded_item is None:
            raise RuntimeError("Storage modified externally")
        return uploaded_item, old_item
"""

_UPLOAD_RETURN_CACHED = """        uploaded_item = self._get(href, verify_href=False)
        if uploaded_item is None:
            raise RuntimeError("Storage modified externally")
        remembered = getattr(self._storage, "_calendar_hub_uids", None)
        if remembered is not None and uploaded_item.uid:
            known = remembered.get(self.path)
            if known is not None:
                known.add(uploaded_item.uid)
        return uploaded_item, old_item
"""

_DELETE_ITEM = """            if not os.path.isfile(path):
                raise storage.ComponentNotFoundError(href)
            os.remove(path)
"""

_DELETE_ITEM_CACHED = """            if not os.path.isfile(path):
                raise storage.ComponentNotFoundError(href)
            deleted = self._get(href, verify_href=False)
            os.remove(path)
"""

_DELETE_CACHE = """            if os.path.isfile(cache_file):
                os.remove(cache_file)
                self._storage._sync_directory(cache_folder)
"""

_DELETE_CACHE_CACHED = """            if os.path.isfile(cache_file):
                os.remove(cache_file)
                self._storage._sync_directory(cache_folder)
            remembered = getattr(self._storage, "_calendar_hub_uids", None)
            if remembered is not None and deleted is not None and deleted.uid:
                known = remembered.get(self.path)
                if known is not None:
                    known.discard(deleted.uid)
"""

_DELETE_COLLECTION = """        if href is None:
            # Delete the collection
"""

_DELETE_COLLECTION_CACHED = """        if href is None:
            remembered = getattr(self._storage, "_calendar_hub_uids", None)
            if remembered is not None:
                remembered.pop(self.path, None)
            # Delete the collection
"""

_MOVE_END = """        if item.collection._filesystem_path != to_collection._filesystem_path:
            item.collection._clean_history()
"""

_MOVE_END_CACHED = """        if item.collection._filesystem_path != to_collection._filesystem_path:
            item.collection._clean_history()
        remembered = getattr(self, "_calendar_hub_uids", None)
        if remembered is not None:
            remembered.pop(getattr(item.collection, "path", None), None)
            remembered.pop(getattr(to_collection, "path", None), None)
"""

_BULK = """    def _upload_all_nonatomic(self, items: Iterable[radicale_item.Item],
                              suffix: str = "") -> None:
"""

_BULK_CACHED = """    def _upload_all_nonatomic(self, items: Iterable[radicale_item.Item],
                              suffix: str = "") -> None:
        remembered = getattr(self._storage, "_calendar_hub_uids", None)
        if remembered is not None:
            remembered.pop(self.path, None)
"""


def _replace(path: Path, old: str, new: str, missing: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(missing)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _disable_fsync() -> None:
    path = ROOT / "config.py"
    text = path.read_text(encoding="utf-8")
    start = text.find('"_filesystem_fsync"')
    if start < 0:
        raise SystemExit("missing fsync option")
    window = text[start : start + 240]
    if '"value": "False"' in window:
        return
    updated = window.replace('"value": "True"', '"value": "False"', 1)
    if updated == window:
        raise SystemExit("fsync value not found")
    path.write_text(text[:start] + updated + text[start + 240 :], encoding="utf-8")


def _throttle_history_cleanup() -> None:
    path = ROOT / "storage" / "multifilesystem" / "history.py"
    text = path.read_text(encoding="utf-8")
    if "_calendar_hub_history_clean" in text:
        return
    if "import time\n" not in text:
        replaced = text.replace("import os\n", "import os\nimport time\n", 1)
        if replaced == text:
            raise SystemExit("history import not found")
        text = replaced
    needle = "    def _clean_history(self):\n"
    insert = (
        needle
        + "        # Scanning every history file on each write is quadratic.\n"
        + "        now = time.monotonic()\n"
        + '        last = getattr(self._storage, "_calendar_hub_history_clean", 0.0)\n'
        + "        if now - last < 60.0:\n"
        + "            return\n"
        + "        self._storage._calendar_hub_history_clean = now\n"
    )
    if needle not in text:
        raise SystemExit("history cleanup not found")
    path.write_text(text.replace(needle, insert, 1), encoding="utf-8")


def _remember_uids() -> None:
    _replace(
        ROOT / "storage" / "__init__.py",
        _HAS_UID,
        _HAS_UID_CACHED,
        "has_uid not found",
    )
    _replace(
        ROOT / "storage" / "multifilesystem" / "upload.py",
        _UPLOAD_RETURN,
        _UPLOAD_RETURN_CACHED,
        "upload return not found",
    )
    _replace(
        ROOT / "storage" / "multifilesystem" / "upload.py",
        _BULK,
        _BULK_CACHED,
        "bulk upload not found",
    )
    _replace(
        ROOT / "storage" / "multifilesystem" / "delete.py",
        _DELETE_ITEM,
        _DELETE_ITEM_CACHED,
        "delete item not found",
    )
    _replace(
        ROOT / "storage" / "multifilesystem" / "delete.py",
        _DELETE_CACHE,
        _DELETE_CACHE_CACHED,
        "delete cache not found",
    )
    _replace(
        ROOT / "storage" / "multifilesystem" / "delete.py",
        _DELETE_COLLECTION,
        _DELETE_COLLECTION_CACHED,
        "delete collection not found",
    )
    _replace(
        ROOT / "storage" / "multifilesystem" / "move.py",
        _MOVE_END,
        _MOVE_END_CACHED,
        "move end not found",
    )


def main() -> None:
    _disable_fsync()
    _throttle_history_cleanup()
    _remember_uids()


if __name__ == "__main__":
    main()
