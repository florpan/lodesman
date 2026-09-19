# SPDX-License-Identifier: MIT
"""
Build the fixture repositories the integration tests run against.

Contributed by the Linux test pass that found the 0.3.0 rename bug.

Every fixture is written from the literals below rather than copied from disk,
so a run never inherits edits from a previous one — which matters here, because
the rename tests mutate their fixture. Call build_all(tmpdir) per test.
"""

from __future__ import annotations

import shutil
from pathlib import Path

PY_BASE = '''"""Storage abstractions."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Record:
    key: str
    value: int

    def scaled(self, factor: int) -> int:
        """Value multiplied by factor."""
        return self.value * factor


class Store(ABC):
    """A key/value store."""

    @abstractmethod
    def get(self, key: str) -> Record | None:
        """Fetch one record."""

    @abstractmethod
    def put(self, record: Record) -> None:
        ...

    def describe(self) -> str:
        return f"{type(self).__name__} store"
'''

PY_MEMORY = '''from .base import Record, Store


class MemoryStore(Store):
    """In-memory implementation."""

    def __init__(self) -> None:
        self._items: dict[str, Record] = {}

    def get(self, key: str) -> Record | None:
        return self._items.get(key)

    def put(self, record: Record) -> None:
        self._items[record.key] = record


class NullStore(Store):
    def get(self, key: str) -> Record | None:
        return None

    def put(self, record: Record) -> None:
        pass
'''

# The __all__ line is the UTF-16 trap: with non_bmp=True, two adjacent non-BMP
# characters sit on the same line BEFORE the rename target, so an implementation
# that indexes a Python str with an LSP column drifts by four code units and
# corrupts the edit.
PY_INIT_PLAIN = '''from .base import Record, Store
from .memory import MemoryStore, NullStore

__all__ = ["Record", "Store", "MemoryStore", "NullStore"]
'''
PY_INIT_NONBMP = '''from .base import Record, Store
from .memory import MemoryStore, NullStore

__all__ = ["\U0001f389\U0001f680", "Record", "Store", "MemoryStore", "NullStore"]
'''

PY_SERVICE = '''from store import MemoryStore, Record, Store


class Service:
    def __init__(self, store: Store) -> None:
        self.store = store

    def add(self, key: str, value: int) -> Record:
        record = Record(key=key, value=value)
        self.store.put(record)
        return record

    def total(self, keys: list[str]) -> int:
        total = 0
        for key in keys:
            record = self.store.get(key)
            if record is not None:
                total += record.scaled(2)
        return total


def build_service() -> Service:
    return Service(MemoryStore())
'''

PY_MAIN = '''from service import Service, build_service
from store import Record


def main() -> None:
    service = build_service()
    service.add("a", 1)
    service.add("b", 2)
    print(service.total(["a", "b"]))
    lone = Record(key="c", value=3)
    print(lone.scaled(3))


if __name__ == "__main__":
    main()
'''

# Deliberately ill-typed: gives check() a stable, known set of diagnostics.
PY_BROKEN = '''from store import Record


def oops() -> int:
    r = Record(key="x", value="not-an-int")
    return r.scaled("two")


def missing_name() -> None:
    return undefined_thing(1, 2)
'''

TS_STORE = '''export interface Store {
  get(key: string): Record | undefined;
  put(record: Record): void;
}

export class Record {
  constructor(public key: string, public value: number) {}

  scaled(factor: number): number {
    return this.value * factor;
  }
}

export class MemoryStore implements Store {
  private items = new Map<string, Record>();

  get(key: string): Record | undefined {
    return this.items.get(key);
  }

  put(record: Record): void {
    this.items.set(record.key, record);
  }
}

export class NullStore implements Store {
  get(_key: string): Record | undefined {
    return undefined;
  }
  put(_record: Record): void {}
}
'''

TS_SERVICE = '''import { MemoryStore, Record, Store } from "./store";

export class Service {
  constructor(private store: Store) {}

  add(key: string, value: number): Record {
    const record = new Record(key, value);
    this.store.put(record);
    return record;
  }

  total(keys: string[]): number {
    let sum = 0;
    for (const key of keys) {
      const record = this.store.get(key);
      if (record) sum += record.scaled(2);
    }
    return sum;
  }
}

export function buildService(): Service {
  return new Service(new MemoryStore());
}
'''

TS_BROKEN = '''import { Record } from "./store";

export function oops(): number {
  const r = new Record("x", "not-a-number");
  return r.scaled("two");
}
'''


def _write(path: Path, text: str, newline: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if newline != "\n":
        text = text.replace("\n", newline)
    # newline="" disables translation, so `newline` is what actually lands.
    path.write_text(text, encoding="utf-8", newline="")


def python_repo(dest: Path, newline: str = "\n", non_bmp: bool = False) -> Path:
    """
    A small multi-module Python project.

    newline: "\\n" or "\\r\\n" — asserts rename preserves existing endings.
    non_bmp: put two adjacent astral-plane characters before the rename target.
    """
    if dest.exists():
        shutil.rmtree(dest)
    _write(dest / "pyproject.toml",
           '[project]\nname = "fixture"\nversion = "0.1.0"\n'
           'requires-python = ">=3.10"\n', newline)
    _write(dest / "src/store/base.py", PY_BASE, newline)
    _write(dest / "src/store/memory.py", PY_MEMORY, newline)
    _write(dest / "src/store/__init__.py",
           PY_INIT_NONBMP if non_bmp else PY_INIT_PLAIN, newline)
    _write(dest / "src/service.py", PY_SERVICE, newline)
    _write(dest / "src/main.py", PY_MAIN, newline)
    _write(dest / "src/broken.py", PY_BROKEN, newline)
    return dest


def typescript_repo(dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    _write(dest / "package.json",
           '{ "name": "tsfixture", "version": "1.0.0", "private": true }\n', "\n")
    _write(dest / "tsconfig.json",
           '{ "compilerOptions": { "target": "ES2020", "module": "ESNext",'
           ' "moduleResolution": "bundler", "strict": true, "noEmit": true },'
           ' "include": ["src"] }\n', "\n")
    _write(dest / "src/store.ts", TS_STORE, "\n")
    _write(dest / "src/service.ts", TS_SERVICE, "\n")
    _write(dest / "src/broken.ts", TS_BROKEN, "\n")
    return dest


def hidden_dir_repo(dest: Path) -> Path:
    """
    130 .py files in dot-directories, one real .ts file.

    Detection must report typescript. 0.3.0 reported python (130 files).
    """
    if dest.exists():
        shutil.rmtree(dest)
    for sub, count in [(".tox/py311", 60), (".mypy_cache", 40), (".direnv", 30)]:
        for i in range(1, count + 1):
            _write(dest / sub / f"m{i}.py", f"x = {i}\n", "\n")
    _write(dest / "src/app.ts",
           'export function greet(who: string): string { return `hi ${who}`; }\n', "\n")
    return dest


def no_source_repo(dest: Path) -> Path:
    """Only a README: detection must fail cleanly, not traceback."""
    if dest.exists():
        shutil.rmtree(dest)
    _write(dest / "README.md", "# docs only\n", "\n")
    return dest


def build_all(root: Path) -> dict[str, Path]:
    root = Path(root)
    return {
        "py_lf": python_repo(root / "py_lf"),
        "py_crlf": python_repo(root / "py_crlf", newline="\r\n"),
        "py_nonbmp": python_repo(root / "py_nonbmp", non_bmp=True),
        "ts": typescript_repo(root / "ts"),
        "hidden": hidden_dir_repo(root / "hidden"),
        "nosource": no_source_repo(root / "nosource"),
    }


if __name__ == "__main__":
    import sys
    for name, path in build_all(Path(sys.argv[1] if len(sys.argv) > 1 else "./fixtures")).items():
        print(f"{name:10} {path}")
