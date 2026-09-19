# SPDX-License-Identifier: MIT
"""
One fixture project per language lodesman claims to auto-detect.

The set is deliberately tied to EXTENSION_LANGUAGES in server.py rather than to
a popularity list: every language the detector recognises should have a test
proving the tools actually work on it, and nothing should be claimed that is not
covered here.

Every fixture models the same thing, so the assertions are identical across
languages and only the syntax differs:

    Record          a type with a `scaled(factor)` method
    Store           an interface/abstract type with `get` and `put`
    MemoryStore     an implementation of Store
    NullStore       a second implementation — the rename target
    service.*       a second file, so cross-file references are real

That uniformity is the point. A per-language failure is then a statement about
the language server, not about a fixture someone wrote differently that day.

`requires` lists executables that must exist for the language server to start.
It is a fast pre-filter only — the suite still probes the server itself, since
a toolchain being on PATH does not mean the server works.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LanguageSpec:
    language: str
    """The --language id, matching solidlsp's LanguageServerId."""

    files: dict[str, str]
    outline_file: str
    """A file whose document_symbols output should contain `outline_symbols`."""

    outline_symbols: tuple[str, ...] = ("Record", "Store", "MemoryStore", "NullStore")
    requires: tuple[str, ...] = ()
    """Executables that must be on PATH. Empty means the server self-provisions."""

    supports_references: bool = True
    supports_rename: bool = True
    """
    What the language server behind this actually provides.

    Not every server implements the whole protocol, and a couple gate parts of
    it commercially. Where a capability is absent the contract does not go soft
    — it asserts the opposite property, that lodesman reports the absence
    instead of claiming a result it does not have. A confident wrong answer is
    the one failure mode an agent cannot recover from.
    """

    prebuild: tuple[str, ...] = ()
    """
    A command to run in the fixture before starting the language server.

    Most servers parse source directly. sourcekit-lsp does not: it answers from
    a compiled index store, so without a build it reports no symbol named
    'Record' in a file that plainly declares one. Building is part of setting
    that language up, not a workaround.
    """

    known_failures: dict[str, str] = field(default_factory=dict)
    """
    Individual contract tests known to fail, mapped to why.

    Skipped with the reason rather than left to fail, so that a language that
    mostly works is not represented as a red build, and so that a *new* failure
    in the same language still is one. The reason has to say what was ruled out,
    not just that it fails.
    """

    known_gap: str = ""
    """
    Why this language is not expected to pass yet, if it is not.

    Recorded here rather than left to a log, so that CI can tell an expected
    skip from a regression: a language with no known_gap that skips is a
    failure, because CI installs the toolchain deliberately. Without this a
    green tick can mean "ran eight assertions" or "ran nothing at all", and
    those should not look the same.
    """

    notes: str = ""

    def missing_tools(self) -> list[str]:
        return [tool for tool in self.requires if shutil.which(tool) is None]

    def prepare(self, repo: Path) -> str | None:
        """Run the prebuild, if any. Returns an error description on failure."""
        if not self.prebuild:
            return None
        try:
            done = subprocess.run(self.prebuild, cwd=repo, capture_output=True,
                                  text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{' '.join(self.prebuild)}: {exc}"
        if done.returncode != 0:
            return f"{' '.join(self.prebuild)} exited {done.returncode}: {done.stderr[-300:]}"
        return None

    def build(self, dest: Path) -> Path:
        for relative, content in self.files.items():
            path = dest / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
        return dest


# --------------------------------------------------------------------------
# C#  — Roslyn, which downloads both .NET and itself. No toolchain required.
# --------------------------------------------------------------------------

CSHARP = LanguageSpec(
    language="csharp",
    outline_file="Store.cs",
    requires=(),
    notes="Roslyn fetches .NET via Microsoft's install scripts; first run is slow.",
    files={
        "Fixture.csproj": (
            "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
            "  <PropertyGroup>\n"
            "    <TargetFramework>net8.0</TargetFramework>\n"
            "    <Nullable>enable</Nullable>\n"
            "  </PropertyGroup>\n"
            "</Project>\n"
        ),
        "Store.cs": (
            "namespace Fixture;\n\n"
            "public class Record\n"
            "{\n"
            "    public string Key { get; init; } = \"\";\n"
            "    public int Value { get; init; }\n\n"
            "    public int Scaled(int factor) => Value * factor;\n"
            "}\n\n"
            "public interface Store\n"
            "{\n"
            "    Record? Get(string key);\n"
            "    void Put(Record record);\n"
            "}\n\n"
            "public class MemoryStore : Store\n"
            "{\n"
            "    private readonly Dictionary<string, Record> _items = new();\n\n"
            "    public Record? Get(string key) => _items.TryGetValue(key, out var r) ? r : null;\n"
            "    public void Put(Record record) => _items[record.Key] = record;\n"
            "}\n\n"
            "public class NullStore : Store\n"
            "{\n"
            "    public Record? Get(string key) => null;\n"
            "    public void Put(Record record) { }\n"
            "}\n"
        ),
        "Service.cs": (
            "namespace Fixture;\n\n"
            "public class Service\n"
            "{\n"
            "    private readonly Store _store;\n\n"
            "    public Service(Store store) => _store = store;\n\n"
            "    public Record Add(string key, int value)\n"
            "    {\n"
            "        var record = new Record { Key = key, Value = value };\n"
            "        _store.Put(record);\n"
            "        return record;\n"
            "    }\n\n"
            "    public int Total(IEnumerable<string> keys)\n"
            "    {\n"
            "        var total = 0;\n"
            "        foreach (var key in keys)\n"
            "        {\n"
            "            var record = _store.Get(key);\n"
            "            if (record is not null) total += record.Scaled(2);\n"
            "        }\n"
            "        return total;\n"
            "    }\n"
            "}\n"
        ),
    },
)

# --------------------------------------------------------------------------
# TypeScript — typescript-language-server, installed from npm.
# --------------------------------------------------------------------------

TYPESCRIPT = LanguageSpec(
    language="typescript",
    outline_file="src/store.ts",
    requires=("node", "npm"),
    files={
        "package.json": '{ "name": "fixture", "version": "1.0.0", "private": true }\n',
        "tsconfig.json": (
            '{ "compilerOptions": { "target": "ES2020", "module": "ESNext",'
            ' "moduleResolution": "bundler", "strict": true, "noEmit": true },'
            ' "include": ["src"] }\n'
        ),
        "src/store.ts": (
            "export class Record {\n"
            "  constructor(public key: string, public value: number) {}\n\n"
            "  scaled(factor: number): number {\n"
            "    return this.value * factor;\n"
            "  }\n"
            "}\n\n"
            "export interface Store {\n"
            "  get(key: string): Record | undefined;\n"
            "  put(record: Record): void;\n"
            "}\n\n"
            "export class MemoryStore implements Store {\n"
            "  private items = new Map<string, Record>();\n\n"
            "  get(key: string): Record | undefined {\n"
            "    return this.items.get(key);\n"
            "  }\n\n"
            "  put(record: Record): void {\n"
            "    this.items.set(record.key, record);\n"
            "  }\n"
            "}\n\n"
            "export class NullStore implements Store {\n"
            "  get(_key: string): Record | undefined {\n"
            "    return undefined;\n"
            "  }\n"
            "  put(_record: Record): void {}\n"
            "}\n"
        ),
        "src/service.ts": (
            'import { MemoryStore, Record, Store } from "./store";\n\n'
            "export class Service {\n"
            "  constructor(private store: Store) {}\n\n"
            "  add(key: string, value: number): Record {\n"
            "    const record = new Record(key, value);\n"
            "    this.store.put(record);\n"
            "    return record;\n"
            "  }\n\n"
            "  total(keys: string[]): number {\n"
            "    let sum = 0;\n"
            "    for (const key of keys) {\n"
            "      const record = this.store.get(key);\n"
            "      if (record) sum += record.scaled(2);\n"
            "    }\n"
            "    return sum;\n"
            "  }\n"
            "}\n\n"
            "export function buildService(): Service {\n"
            "  return new Service(new MemoryStore());\n"
            "}\n"
        ),
    },
)

# --------------------------------------------------------------------------
# Python — pyright, launched through uvx, which brings its own runtime.
# --------------------------------------------------------------------------

PYTHON = LanguageSpec(
    language="python",
    outline_file="src/store.py",
    requires=("uv",),
    files={
        "pyproject.toml": (
            '[project]\nname = "fixture"\nversion = "0.1.0"\n'
            'requires-python = ">=3.10"\n'
        ),
        "src/store.py": (
            '"""Storage abstractions."""\n'
            "from abc import ABC, abstractmethod\n"
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\n"
            "class Record:\n"
            "    key: str\n"
            "    value: int\n\n"
            "    def scaled(self, factor: int) -> int:\n"
            "        return self.value * factor\n\n\n"
            "class Store(ABC):\n"
            "    @abstractmethod\n"
            "    def get(self, key: str) -> Record | None:\n"
            "        ...\n\n"
            "    @abstractmethod\n"
            "    def put(self, record: Record) -> None:\n"
            "        ...\n\n\n"
            "class MemoryStore(Store):\n"
            "    def __init__(self) -> None:\n"
            "        self._items: dict[str, Record] = {}\n\n"
            "    def get(self, key: str) -> Record | None:\n"
            "        return self._items.get(key)\n\n"
            "    def put(self, record: Record) -> None:\n"
            "        self._items[record.key] = record\n\n\n"
            "class NullStore(Store):\n"
            "    def get(self, key: str) -> Record | None:\n"
            "        return None\n\n"
            "    def put(self, record: Record) -> None:\n"
            "        pass\n"
        ),
        "src/service.py": (
            "from store import MemoryStore, Record, Store\n\n\n"
            "class Service:\n"
            "    def __init__(self, store: Store) -> None:\n"
            "        self.store = store\n\n"
            "    def add(self, key: str, value: int) -> Record:\n"
            "        record = Record(key=key, value=value)\n"
            "        self.store.put(record)\n"
            "        return record\n\n"
            "    def total(self, keys: list[str]) -> int:\n"
            "        total = 0\n"
            "        for key in keys:\n"
            "            record = self.store.get(key)\n"
            "            if record is not None:\n"
            "                total += record.scaled(2)\n"
            "        return total\n\n\n"
            "def build_service() -> Service:\n"
            "    return Service(MemoryStore())\n"
        ),
    },
)

# --------------------------------------------------------------------------
# Go — gopls, which requires a Go toolchain to install and to resolve imports.
# --------------------------------------------------------------------------

GO = LanguageSpec(
    language="go",
    outline_file="store/store.go",
    # gopls as well as go: solidlsp does not install gopls, and a Go toolchain
    # without it starts a server that fails on the first request rather than
    # failing to start, which is a slower and noisier way to reach the same skip.
    requires=("go", "gopls"),
    notes="install with: go install golang.org/x/tools/gopls@latest",
    files={
        "go.mod": "module fixture\n\ngo 1.21\n",
        "store/store.go": (
            "package store\n\n"
            "type Record struct {\n"
            "\tKey   string\n"
            "\tValue int\n"
            "}\n\n"
            "func (r Record) Scaled(factor int) int {\n"
            "\treturn r.Value * factor\n"
            "}\n\n"
            "type Store interface {\n"
            "\tGet(key string) *Record\n"
            "\tPut(record Record)\n"
            "}\n\n"
            "type MemoryStore struct {\n"
            "\titems map[string]Record\n"
            "}\n\n"
            "func NewMemoryStore() *MemoryStore {\n"
            "\treturn &MemoryStore{items: map[string]Record{}}\n"
            "}\n\n"
            "func (m *MemoryStore) Get(key string) *Record {\n"
            "\tif r, ok := m.items[key]; ok {\n"
            "\t\treturn &r\n"
            "\t}\n"
            "\treturn nil\n"
            "}\n\n"
            "func (m *MemoryStore) Put(record Record) {\n"
            "\tm.items[record.Key] = record\n"
            "}\n\n"
            "type NullStore struct{}\n\n"
            "func (n NullStore) Get(key string) *Record { return nil }\n\n"
            "func (n NullStore) Put(record Record)      {}\n"
        ),
        "service/service.go": (
            "package service\n\n"
            "import \"fixture/store\"\n\n"
            "type Service struct {\n"
            "\tstore store.Store\n"
            "}\n\n"
            "func New(s store.Store) *Service {\n"
            "\treturn &Service{store: s}\n"
            "}\n\n"
            "func (s *Service) Add(key string, value int) store.Record {\n"
            "\trecord := store.Record{Key: key, Value: value}\n"
            "\ts.store.Put(record)\n"
            "\treturn record\n"
            "}\n\n"
            "func (s *Service) Total(keys []string) int {\n"
            "\ttotal := 0\n"
            "\tfor _, key := range keys {\n"
            "\t\tif record := s.store.Get(key); record != nil {\n"
            "\t\t\ttotal += record.Scaled(2)\n"
            "\t\t}\n"
            "\t}\n"
            "\treturn total\n"
            "}\n"
        ),
    },
)

# --------------------------------------------------------------------------
# Rust — rust-analyzer, resolved through rustup.
# --------------------------------------------------------------------------

RUST = LanguageSpec(
    language="rust",
    outline_file="src/store.rs",
    requires=("rustup",),
    files={
        "Cargo.toml": (
            '[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n\n'
            "[dependencies]\n"
        ),
        "src/lib.rs": "pub mod service;\npub mod store;\n",
        "src/store.rs": (
            "pub struct Record {\n"
            "    pub key: String,\n"
            "    pub value: i32,\n"
            "}\n\n"
            "impl Record {\n"
            "    pub fn scaled(&self, factor: i32) -> i32 {\n"
            "        self.value * factor\n"
            "    }\n"
            "}\n\n"
            "pub trait Store {\n"
            "    fn get(&self, key: &str) -> Option<&Record>;\n"
            "    fn put(&mut self, record: Record);\n"
            "}\n\n"
            "#[derive(Default)]\n"
            "pub struct MemoryStore {\n"
            "    items: std::collections::HashMap<String, Record>,\n"
            "}\n\n"
            "impl Store for MemoryStore {\n"
            "    fn get(&self, key: &str) -> Option<&Record> {\n"
            "        self.items.get(key)\n"
            "    }\n\n"
            "    fn put(&mut self, record: Record) {\n"
            "        self.items.insert(record.key.clone(), record);\n"
            "    }\n"
            "}\n\n"
            "#[derive(Default)]\n"
            "pub struct NullStore;\n\n"
            "impl Store for NullStore {\n"
            "    fn get(&self, _key: &str) -> Option<&Record> {\n"
            "        None\n"
            "    }\n\n"
            "    fn put(&mut self, _record: Record) {}\n"
            "}\n"
        ),
        "src/service.rs": (
            "use crate::store::{MemoryStore, Record, Store};\n\n"
            "pub struct Service<S: Store> {\n"
            "    store: S,\n"
            "}\n\n"
            "impl<S: Store> Service<S> {\n"
            "    pub fn new(store: S) -> Self {\n"
            "        Self { store }\n"
            "    }\n\n"
            "    pub fn add(&mut self, key: &str, value: i32) {\n"
            "        self.store.put(Record { key: key.to_string(), value });\n"
            "    }\n\n"
            "    pub fn total(&self, keys: &[String]) -> i32 {\n"
            "        let mut total = 0;\n"
            "        for key in keys {\n"
            "            if let Some(record) = self.store.get(key) {\n"
            "                total += record.scaled(2);\n"
            "            }\n"
            "        }\n"
            "        total\n"
            "    }\n"
            "}\n\n"
            "pub fn build_service() -> Service<MemoryStore> {\n"
            "    Service::new(MemoryStore::default())\n"
            "}\n"
        ),
    },
)

# --------------------------------------------------------------------------
# Java — Eclipse JDT LS. Needs a JDK; jdtls itself is downloaded.
# --------------------------------------------------------------------------

JAVA = LanguageSpec(
    language="java",
    outline_file="src/main/java/fixture/Store.java",
    outline_symbols=("Store",),
    requires=("java", "javac"),
    notes="jdtls wants a build file; a minimal pom keeps it out of invisible-project mode.",
    files={
        "pom.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>fixture</groupId>\n"
            "  <artifactId>fixture</artifactId>\n"
            "  <version>1.0.0</version>\n"
            "  <properties>\n"
            "    <maven.compiler.source>17</maven.compiler.source>\n"
            "    <maven.compiler.target>17</maven.compiler.target>\n"
            "  </properties>\n"
            "</project>\n"
        ),
        "src/main/java/fixture/Record.java": (
            "package fixture;\n\n"
            "public class Record {\n"
            "    private final String key;\n"
            "    private final int value;\n\n"
            "    public Record(String key, int value) {\n"
            "        this.key = key;\n"
            "        this.value = value;\n"
            "    }\n\n"
            "    public String getKey() { return key; }\n\n"
            "    public int scaled(int factor) { return value * factor; }\n"
            "}\n"
        ),
        "src/main/java/fixture/Store.java": (
            "package fixture;\n\n"
            "public interface Store {\n"
            "    Record get(String key);\n"
            "    void put(Record record);\n"
            "}\n"
        ),
        "src/main/java/fixture/MemoryStore.java": (
            "package fixture;\n\n"
            "import java.util.HashMap;\n"
            "import java.util.Map;\n\n"
            "public class MemoryStore implements Store {\n"
            "    private final Map<String, Record> items = new HashMap<>();\n\n"
            "    public Record get(String key) { return items.get(key); }\n\n"
            "    public void put(Record record) { items.put(record.getKey(), record); }\n"
            "}\n"
        ),
        "src/main/java/fixture/NullStore.java": (
            "package fixture;\n\n"
            "public class NullStore implements Store {\n"
            "    public Record get(String key) { return null; }\n\n"
            "    public void put(Record record) { }\n"
            "}\n"
        ),
        "src/main/java/fixture/Service.java": (
            "package fixture;\n\n"
            "import java.util.List;\n\n"
            "public class Service {\n"
            "    private final Store store;\n\n"
            "    public Service(Store store) { this.store = store; }\n\n"
            "    public Record add(String key, int value) {\n"
            "        Record record = new Record(key, value);\n"
            "        store.put(record);\n"
            "        return record;\n"
            "    }\n\n"
            "    public int total(List<String> keys) {\n"
            "        int total = 0;\n"
            "        for (String key : keys) {\n"
            "            Record record = store.get(key);\n"
            "            if (record != null) total += record.scaled(2);\n"
            "        }\n"
            "        return total;\n"
            "    }\n"
            "}\n"
        ),
    },
)

# --------------------------------------------------------------------------
# Kotlin — kotlin-language-server, which is not self-installing.
# --------------------------------------------------------------------------

KOTLIN = LanguageSpec(
    language="kotlin",
    outline_file="src/main/kotlin/fixture/Store.kt",
    # Not kotlin-language-server: SolidLSP downloads JetBrains intellij-server and
    # never invokes fwcd's binary, so gating on it tested the wrong thing entirely.
    requires=("java",),
    known_gap=(
        "SolidLSP's pinned build has expired. It downloads JetBrains intellij-server "
        "at DEFAULT_KOTLIN_LSP_VERSION = 262.9593.0, and that build now prints 'This "
        "build of intellij-server has expired' to stdout and exits immediately, which "
        "surfaces as a LanguageServerTerminatedException during initialize. Diagnosed "
        "2026-09-19 on Linux; upstream tracks it as oraios/serena#2008. Pinning "
        "263.4702.0 via ls_specific_settings makes the full contract pass 8/8, but "
        "that moves an expiring EAP pin into this repository — see the README."
    ),
    notes="kotlin-language-server ships as a script; there is no auto-install path.",
    files={
        "build.gradle.kts": (
            "plugins {\n    kotlin(\"jvm\") version \"1.9.22\"\n}\n\n"
            "repositories {\n    mavenCentral()\n}\n"
        ),
        "settings.gradle.kts": 'rootProject.name = "fixture"\n',
        "src/main/kotlin/fixture/Store.kt": (
            "package fixture\n\n"
            "data class Record(val key: String, val value: Int) {\n"
            "    fun scaled(factor: Int): Int = value * factor\n"
            "}\n\n"
            "interface Store {\n"
            "    fun get(key: String): Record?\n"
            "    fun put(record: Record)\n"
            "}\n\n"
            "class MemoryStore : Store {\n"
            "    private val items = mutableMapOf<String, Record>()\n\n"
            "    override fun get(key: String): Record? = items[key]\n\n"
            "    override fun put(record: Record) {\n"
            "        items[record.key] = record\n"
            "    }\n"
            "}\n\n"
            "class NullStore : Store {\n"
            "    override fun get(key: String): Record? = null\n\n"
            "    override fun put(record: Record) {}\n"
            "}\n"
        ),
        "src/main/kotlin/fixture/Service.kt": (
            "package fixture\n\n"
            "class Service(private val store: Store) {\n"
            "    fun add(key: String, value: Int): Record {\n"
            "        val record = Record(key, value)\n"
            "        store.put(record)\n"
            "        return record\n"
            "    }\n\n"
            "    fun total(keys: List<String>): Int =\n"
            "        keys.mapNotNull { store.get(it) }.sumOf { it.scaled(2) }\n"
            "}\n\n"
            "fun buildService(): Service = Service(MemoryStore())\n"
        ),
    },
)

# --------------------------------------------------------------------------
# Ruby — ruby-lsp, installed as a gem.
# --------------------------------------------------------------------------

RUBY = LanguageSpec(
    language="ruby",
    outline_file="lib/store.rb",
    requires=("ruby", "gem", "bundle"),
    # ruby-lsp's launcher calls setup_bundler unconditionally, and a Gemfile with
    # no Gemfile.lock makes it exit(78) before completing the handshake:
    # "Project contains a Gemfile, but no Gemfile.lock. Run `bundle install`".
    # That is what looked like a protocol failure. Locking the fixture fixes it.
    prebuild=("bundle", "lock"),
    known_failures={
        "test_find_references_crosses_files":
            "ruby-lsp returns no references for a class used in a sibling file. Not "
            "a capability gap — it advertises referencesProvider: true — and not "
            "timing: raising its 500ms cross-file wait to 8s changed nothing. "
            "Unexplained as of 2026-09-19.",
        "test_rename_writes_to_disk_and_preserves_line_endings":
            "ruby-lsp produces no edits, while advertising renameProvider with "
            "prepareProvider: true. Same root cause as the references gap, most "
            "likely. Unexplained as of 2026-09-19.",
    },
    known_gap="",
    files={
        "Gemfile": 'source "https://rubygems.org"\n',
        "lib/store.rb": (
            "# frozen_string_literal: true\n\n"
            "class Record\n"
            "  attr_reader :key, :value\n\n"
            "  def initialize(key, value)\n"
            "    @key = key\n"
            "    @value = value\n"
            "  end\n\n"
            "  def scaled(factor)\n"
            "    @value * factor\n"
            "  end\n"
            "end\n\n"
            "class Store\n"
            "  def get(key)\n"
            "    raise NotImplementedError\n"
            "  end\n\n"
            "  def put(record)\n"
            "    raise NotImplementedError\n"
            "  end\n"
            "end\n\n"
            "class MemoryStore < Store\n"
            "  def initialize\n"
            "    @items = {}\n"
            "  end\n\n"
            "  def get(key)\n"
            "    @items[key]\n"
            "  end\n\n"
            "  def put(record)\n"
            "    @items[record.key] = record\n"
            "  end\n"
            "end\n\n"
            "class NullStore < Store\n"
            "  def get(key)\n"
            "    nil\n"
            "  end\n\n"
            "  def put(record); end\n"
            "end\n"
        ),
        "lib/service.rb": (
            "# frozen_string_literal: true\n\n"
            'require_relative "store"\n\n'
            "class Service\n"
            "  def initialize(store)\n"
            "    @store = store\n"
            "  end\n\n"
            "  def add(key, value)\n"
            "    record = Record.new(key, value)\n"
            "    @store.put(record)\n"
            "    record\n"
            "  end\n\n"
            "  def total(keys)\n"
            "    keys.filter_map { |key| @store.get(key) }.sum { |record| record.scaled(2) }\n"
            "  end\n"
            "end\n\n"
            "def build_service\n"
            "  Service.new(MemoryStore.new)\n"
            "end\n"
        ),
    },
)

# --------------------------------------------------------------------------
# PHP — intelephense, an npm package. Needs node, but not PHP itself.
# --------------------------------------------------------------------------

PHP = LanguageSpec(
    language="php",
    outline_file="src/Store.php",
    requires=("node", "npm"),
    # Measured, not assumed: on an unlicensed intelephense, find_references
    # returns nothing for a class that is demonstrably used in a sibling file,
    # and rename produces no edits — while document_symbols on that same
    # sibling file works, so the file is parsed. Waiting 19s changes nothing
    # and opening the referencing file first changes nothing. solidlsp's own
    # intelephense wrapper reads INTELEPHENSE_LICENSE_KEY, which is consistent
    # with these being the licensed features.
    supports_references=False,
    supports_rename=False,
    notes=(
        "intelephense analyses PHP from node; a PHP runtime is not required. "
        "References and rename need a licence — set INTELEPHENSE_LICENSE_KEY "
        "for full coverage."
    ),
    files={
        "composer.json": '{ "name": "fixture/fixture", "autoload": { "psr-4": { "Fixture\\\\": "src/" } } }\n',
        "src/Store.php": (
            "<?php\n\n"
            "namespace Fixture;\n\n"
            "class Record\n"
            "{\n"
            "    public function __construct(public string $key, public int $value)\n"
            "    {\n"
            "    }\n\n"
            "    public function scaled(int $factor): int\n"
            "    {\n"
            "        return $this->value * $factor;\n"
            "    }\n"
            "}\n\n"
            "interface Store\n"
            "{\n"
            "    public function get(string $key): ?Record;\n\n"
            "    public function put(Record $record): void;\n"
            "}\n\n"
            "class MemoryStore implements Store\n"
            "{\n"
            "    private array $items = [];\n\n"
            "    public function get(string $key): ?Record\n"
            "    {\n"
            "        return $this->items[$key] ?? null;\n"
            "    }\n\n"
            "    public function put(Record $record): void\n"
            "    {\n"
            "        $this->items[$record->key] = $record;\n"
            "    }\n"
            "}\n\n"
            "class NullStore implements Store\n"
            "{\n"
            "    public function get(string $key): ?Record\n"
            "    {\n"
            "        return null;\n"
            "    }\n\n"
            "    public function put(Record $record): void\n"
            "    {\n"
            "    }\n"
            "}\n"
        ),
        "src/Service.php": (
            "<?php\n\n"
            "namespace Fixture;\n\n"
            "class Service\n"
            "{\n"
            "    public function __construct(private Store $store)\n"
            "    {\n"
            "    }\n\n"
            "    public function add(string $key, int $value): Record\n"
            "    {\n"
            "        $record = new Record($key, $value);\n"
            "        $this->store->put($record);\n"
            "        return $record;\n"
            "    }\n\n"
            "    public function total(array $keys): int\n"
            "    {\n"
            "        $total = 0;\n"
            "        foreach ($keys as $key) {\n"
            "            $record = $this->store->get($key);\n"
            "            if ($record !== null) {\n"
            "                $total += $record->scaled(2);\n"
            "            }\n"
            "        }\n"
            "        return $total;\n"
            "    }\n"
            "}\n\n"
            "function buildService(): Service\n"
            "{\n"
            "    return new Service(new MemoryStore());\n"
            "}\n"
        ),
    },
)

# --------------------------------------------------------------------------
# Swift — sourcekit-lsp, part of a Swift toolchain.
# --------------------------------------------------------------------------

SWIFT = LanguageSpec(
    language="swift",
    outline_file="Sources/Fixture/Store.swift",
    requires=("swift",),
    prebuild=("swift", "build"),
    notes=(
        "sourcekit-lsp answers from a compiled index store, so the package must "
        "be built before symbols resolve."
    ),
    files={
        "Package.swift": (
            "// swift-tools-version:5.7\n"
            "import PackageDescription\n\n"
            "let package = Package(\n"
            '    name: "Fixture",\n'
            '    targets: [.target(name: "Fixture")]\n'
            ")\n"
        ),
        "Sources/Fixture/Store.swift": (
            "public struct Record {\n"
            "    public let key: String\n"
            "    public let value: Int\n\n"
            "    public init(key: String, value: Int) {\n"
            "        self.key = key\n"
            "        self.value = value\n"
            "    }\n\n"
            "    public func scaled(_ factor: Int) -> Int {\n"
            "        return value * factor\n"
            "    }\n"
            "}\n\n"
            "public protocol Store {\n"
            "    func get(_ key: String) -> Record?\n"
            "    mutating func put(_ record: Record)\n"
            "}\n\n"
            "public struct MemoryStore: Store {\n"
            "    private var items: [String: Record] = [:]\n\n"
            "    public init() {}\n\n"
            "    public func get(_ key: String) -> Record? {\n"
            "        return items[key]\n"
            "    }\n\n"
            "    public mutating func put(_ record: Record) {\n"
            "        items[record.key] = record\n"
            "    }\n"
            "}\n\n"
            "public struct NullStore: Store {\n"
            "    public init() {}\n\n"
            "    public func get(_ key: String) -> Record? {\n"
            "        return nil\n"
            "    }\n\n"
            "    public mutating func put(_ record: Record) {}\n"
            "}\n"
        ),
        "Sources/Fixture/Service.swift": (
            "public struct Service {\n"
            "    private var store: Store\n\n"
            "    public init(store: Store) {\n"
            "        self.store = store\n"
            "    }\n\n"
            "    public mutating func add(key: String, value: Int) -> Record {\n"
            "        let record = Record(key: key, value: value)\n"
            "        store.put(record)\n"
            "        return record\n"
            "    }\n\n"
            "    public func total(keys: [String]) -> Int {\n"
            "        return keys.compactMap { store.get($0) }.reduce(0) { $0 + $1.scaled(2) }\n"
            "    }\n"
            "}\n\n"
            "public func buildService() -> Service {\n"
            "    return Service(store: MemoryStore())\n"
            "}\n"
        ),
    },
)

# --------------------------------------------------------------------------
# C / C++ — clangd, which reads compile_commands.json.
# --------------------------------------------------------------------------

CPP = LanguageSpec(
    language="cpp",
    outline_file="src/store.hpp",
    requires=("clangd",),
    notes="clangd needs compile_commands.json to resolve includes across files.",
    files={
        "compile_commands.json": (
            "[\n"
            '  { "directory": ".", "command": "clang++ -std=c++17 -c src/store.cpp",'
            ' "file": "src/store.cpp" },\n'
            '  { "directory": ".", "command": "clang++ -std=c++17 -c src/service.cpp",'
            ' "file": "src/service.cpp" }\n'
            "]\n"
        ),
        "src/store.hpp": (
            "#pragma once\n"
            "#include <map>\n"
            "#include <string>\n\n"
            "class Record {\n"
            "public:\n"
            "  Record(std::string key, int value) : key_(std::move(key)), value_(value) {}\n"
            "  int scaled(int factor) const { return value_ * factor; }\n"
            "  const std::string &key() const { return key_; }\n\n"
            "private:\n"
            "  std::string key_;\n"
            "  int value_;\n"
            "};\n\n"
            "class Store {\n"
            "public:\n"
            "  virtual ~Store() = default;\n"
            "  virtual const Record *get(const std::string &key) const = 0;\n"
            "  virtual void put(const Record &record) = 0;\n"
            "};\n\n"
            "class MemoryStore : public Store {\n"
            "public:\n"
            "  const Record *get(const std::string &key) const override;\n"
            "  void put(const Record &record) override;\n\n"
            "private:\n"
            "  std::map<std::string, Record> items_;\n"
            "};\n\n"
            "class NullStore : public Store {\n"
            "public:\n"
            "  const Record *get(const std::string &) const override { return nullptr; }\n"
            "  void put(const Record &) override {}\n"
            "};\n"
        ),
        "src/store.cpp": (
            '#include "store.hpp"\n\n'
            "const Record *MemoryStore::get(const std::string &key) const {\n"
            "  auto it = items_.find(key);\n"
            "  return it == items_.end() ? nullptr : &it->second;\n"
            "}\n\n"
            "void MemoryStore::put(const Record &record) {\n"
            "  items_.insert({record.key(), record});\n"
            "}\n"
        ),
        "src/service.cpp": (
            '#include "store.hpp"\n'
            "#include <vector>\n\n"
            "class Service {\n"
            "public:\n"
            "  explicit Service(Store &store) : store_(store) {}\n\n"
            "  int total(const std::vector<std::string> &keys) const {\n"
            "    int total = 0;\n"
            "    for (const auto &key : keys) {\n"
            "      if (const Record *record = store_.get(key)) {\n"
            "        total += record->scaled(2);\n"
            "      }\n"
            "    }\n"
            "    return total;\n"
            "  }\n\n"
            "private:\n"
            "  Store &store_;\n"
            "};\n"
        ),
    },
)


SPECS: tuple[LanguageSpec, ...] = (
    CSHARP, TYPESCRIPT, PYTHON, GO, RUST,
    JAVA, KOTLIN, RUBY, PHP, SWIFT, CPP,
)

BY_LANGUAGE: dict[str, LanguageSpec] = {spec.language: spec for spec in SPECS}


def detectable_languages() -> set[str]:
    """Every language EXTENSION_LANGUAGES maps to, which the suite must cover."""
    from lodesman.server import EXTENSION_LANGUAGES
    return set(EXTENSION_LANGUAGES.values())
