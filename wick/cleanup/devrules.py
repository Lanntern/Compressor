"""What Developer clean-up looks for: which folders tools rebuild (and the file that proves it), their kinds,
and the shared package caches. The scanning itself is in devclean.py."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .caches import CacheSpec, IS_WINDOWS, _env


SHARED = "Package caches and tools"
MIN_SIZE = 1024 * 1024
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


@dataclass(frozen=True)
class Rule:
    names: tuple[str, ...]          # folder names this rule is about
    next_to: tuple[str, ...]        # one of these (glob) must be beside the folder; () = name alone is proof
    what: str                       # what the folder is
    restore: str                    # how it comes back
    inside: tuple[str, ...] = ()    # and, if given, one of these must be inside it
    lock: tuple[str, ...] = ()      # without one of these beside it, a reinstall may get other versions
    kind: str = "build"             # see KINDS: lets a whole kind be switched off


_GRADLE = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
_NODE_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")

RULES: tuple[Rule, ...] = (
    Rule(("node_modules",), ("package.json",), "Node.js packages", "npm install puts them back", lock=_NODE_LOCKS, kind="deps"),
    Rule(("bower_components",), ("bower.json",), "Bower packages", "bower install puts them back", kind="deps"),
    Rule((".next",), ("next.config.*", "package.json"), "Next.js build output", "recreated on the next build"),
    Rule((".nuxt", ".output"), ("nuxt.config.*",), "Nuxt build output", "recreated on the next build"),
    Rule((".svelte-kit",), ("svelte.config.*",), "SvelteKit build output", "recreated on the next build"),
    Rule((".angular",), ("angular.json",), "Angular build cache", "recreated on the next build", kind="cache"),
    Rule((".parcel-cache",), ("package.json",), "Parcel cache", "recreated on the next build", kind="cache"),
    Rule((".turbo",), ("turbo.json", "package.json"), "Turborepo cache", "recreated on the next build", kind="cache"),
    Rule((".docusaurus",), ("docusaurus.config.*",), "Docusaurus build cache", "recreated on the next build", kind="cache"),
    Rule((".tox", ".nox"), ("tox.ini", "noxfile.py", "pyproject.toml", "setup.cfg"), "Python test environments",
         "tox or nox recreates them", kind="venv"),
    Rule((".mypy_cache", ".pytest_cache", ".ruff_cache"), (), "Python tool cache", "recreated automatically", kind="cache"),
    Rule(("target",), ("Cargo.toml",), "Rust build output", "cargo build recreates it",
         inside=("CACHEDIR.TAG", ".rustc_info.json", "debug", "release")),
    Rule(("target",), ("pom.xml",), "Maven build output", "mvn package recreates it"),
    Rule(("build", ".cxx", ".externalNativeBuild"), _GRADLE, "Gradle build output", "recreated on the next build"),
    Rule((".gradle",), _GRADLE, "Gradle project cache", "recreated on the next build", kind="cache"),
    Rule(("bin", "obj"), ("*.csproj", "*.fsproj", "*.vbproj"), ".NET build output", "dotnet build recreates it"),
    Rule((".build",), ("Package.swift",), "Swift package build output", "swift build recreates it"),
    Rule(("build", "DerivedData"), ("*.xcodeproj", "*.xcworkspace"), "Xcode build output", "Xcode rebuilds it"),
    Rule(("Pods",), ("Podfile",), "CocoaPods dependencies", "pod install puts them back", lock=("Podfile.lock",), kind="deps"),
    Rule(("Carthage",), ("Cartfile",), "Carthage dependencies", "carthage bootstrap puts them back",
         lock=("Cartfile.resolved",), kind="deps"),
    Rule((".dart_tool", "build"), ("pubspec.yaml",), "Flutter/Dart build output",
         "flutter pub get and the next build recreate it"),
    Rule(("_build", "deps"), ("mix.exs",), "Elixir build output and dependencies", "mix deps.get puts them back",
         lock=("mix.lock",), kind="deps"),
    Rule((".stack-work",), ("stack.yaml", "package.yaml"), "Haskell Stack build output", "stack build recreates it"),
    Rule(("dist-newstyle",), ("cabal.project", "*.cabal"), "Cabal build output", "cabal build recreates it"),
    Rule((".zig-cache", "zig-cache", "zig-out"), ("build.zig",), "Zig build output", "zig build recreates it"),
    Rule(("vendor",), ("composer.json",), "PHP Composer packages", "composer install puts them back",
         lock=("composer.lock",), kind="deps"),
    Rule(("Library",), ("ProjectSettings",), "Unity import cache",
         "Unity re-imports the assets when you open the project (this can take a while)",
         inside=("ArtifactDB", "SourceAssetDB", "PackageCache", "ScriptAssemblies"), kind="engine"),
    Rule(("obj",), ("ProjectSettings",), "Unity script build output", "recreated when Unity compiles scripts", kind="engine"),
    Rule(("Intermediate", "DerivedDataCache"), ("*.uproject",), "Unreal Engine build files",
         "recreated when you open or build the project", kind="engine"),
    Rule((".godot", ".import"), ("project.godot",), "Godot import cache", "recreated when you open the project", kind="engine"),
    Rule((".terraform",), ("*.tf",), "Terraform providers and modules", "terraform init downloads them again", kind="infra"),
)
KINDS = {
    "deps": "Installed packages (node_modules, Pods, vendor…)",
    "venv": "Python environments",
    "build": "Build output",
    "cache": "Tool caches",
    "coverage": "Test coverage reports",
    "engine": "Game engine caches (Unity, Unreal, Godot)",
    "infra": "Terraform providers",
    "shared": "Package and IDE caches",
    "docker": "Docker",
    "vdisk": "WSL and Docker virtual disks",
}
# Names that are often, but not always, generated ("dist" can be a hand-made release folder). They are only
# listed when the project's own .gitignore ignores them *and* a build tool's file sits beside them.
AMBIGUOUS = {
    "dist": "Build output", "build": "Build output", "out": "Build output", "storybook-static": "Storybook build",
    "coverage": "Test coverage report", ".nyc_output": "Test coverage data", "htmlcov": "Test coverage report",
    ".cache": "Build tool cache", ".vite": "Vite cache", ".astro": "Astro generated files",
    ".expo": "Expo cache",
}
_BUILD_MARKERS = ("package.json", "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml", "go.mod", "pom.xml",
                  "build.gradle", "build.gradle.kts", "*.csproj", "*.sln", "CMakeLists.txt", "Makefile",
                  "composer.json", "Gemfile", "mix.exs", "pubspec.yaml", "angular.json", "deno.json")
_WARN_RULES = {".terraform": "It also remembers which Terraform workspace is selected"}
_BY_NAME: dict[str, list[Rule]] = defaultdict(list)
for _r in RULES:
    for _n in _r.names:
        _BY_NAME[_n].append(_r)

_PY_PROJECT = ("requirements*.txt", "pyproject.toml", "Pipfile", "setup.py", "setup.cfg", "environment.yml",
               "uv.lock", "poetry.lock")
_NEVER_ENTER = {".git", ".svn", ".hg", "$recycle.bin", "system volume information", "windows", "program files",
                "program files (x86)", "programdata", "appdata", "steamapps", "steamlibrary"}
_PM_COMMANDS = (("pnpm-lock.yaml", "pnpm install"), ("yarn.lock", "yarn install"), ("bun.lockb", "bun install"),
                ("bun.lock", "bun install"))


# ---------------------------------------------------------------- shared caches

def shared_specs() -> list[CacheSpec]:
    """Package-manager, toolchain and IDE caches outside any project; all are downloaded or rebuilt again."""
    H = "~"
    again = "Downloaded again when a project needs it."
    if IS_WINDOWS:
        L, R = _env("LOCALAPPDATA"), _env("APPDATA")
        s = [
            CacheSpec("npm cache", SHARED, (f"{L}/npm-cache/_cacache",), again),
            CacheSpec("Yarn cache", SHARED, (f"{L}/Yarn/Cache",), again),
            CacheSpec("pnpm store", SHARED, (f"{L}/pnpm/store", f"{H}/.pnpm-store"),
                      again + " Space shared with node_modules folders is only freed when those go too."),
            CacheSpec("pip cache", SHARED, (f"{L}/pip/Cache",), again),
            CacheSpec("uv cache", SHARED, (f"{L}/uv/cache",), again),
            CacheSpec("Poetry cache", SHARED, (f"{L}/pypoetry/Cache",), again),
            CacheSpec("Go build cache", SHARED, (f"{L}/go-build",), "Go rebuilds it."),
            CacheSpec("NuGet caches", SHARED, (f"{L}/NuGet/v3-cache", f"{L}/NuGet/Cache", f"{L}/NuGet/plugins-cache"),
                      again),
            CacheSpec("Composer cache", SHARED, (f"{L}/Composer",), again),
            CacheSpec("Deno cache", SHARED, (f"{L}/deno",), again),
            CacheSpec("Playwright browsers", SHARED, (f"{L}/ms-playwright",), "playwright install downloads them "
                                                                                "again."),
            CacheSpec("Electron downloads", SHARED, (f"{L}/electron/Cache",), again),
            CacheSpec("node-gyp headers", SHARED, (f"{L}/node-gyp/Cache",), again),
            CacheSpec("Dart/Flutter packages", SHARED, (f"{L}/Pub/Cache/hosted", f"{L}/Pub/Cache/git"), again),
            CacheSpec("Unity package cache", SHARED, (f"{L}/Unity/cache",), again, "unity"),
            CacheSpec("JetBrains IDE caches", SHARED, (f"{L}/JetBrains/*/caches",),
                      "The IDE indexes your projects again.", "idea64"),
            CacheSpec("Android Studio caches", SHARED, (f"{L}/Google/AndroidStudio*/caches",),
                      "Android Studio indexes your projects again.", "studio64"),
            CacheSpec("VS Code caches", SHARED, tuple(f"{R}/Code/{c}" for c in ("Cache", "CachedData",
                                                                               "CachedExtensionVSIXs")),
                      "VS Code rebuilds them.", "code"),
            CacheSpec("ccache", SHARED, (f"{L}/ccache",), "Compilers redo the work once."),
            CacheSpec("sccache", SHARED, (f"{L}/Mozilla/sccache/cache",), "Compilers redo the work once."),
        ]
    else:
        C = _env("XDG_CACHE_HOME", str(Path.home() / ".cache"))
        cfg = _env("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        lib = f"{H}/Library"
        s = [
            CacheSpec("npm cache", SHARED, (f"{H}/.npm/_cacache",), again),
            CacheSpec("Yarn cache", SHARED, (f"{C}/yarn", f"{lib}/Caches/Yarn"), again),
            CacheSpec("pnpm store", SHARED, (f"{H}/.local/share/pnpm/store", f"{H}/.pnpm-store",
                                             f"{lib}/pnpm/store"),
                      again + " Space shared with node_modules folders is only freed when those go too."),
            CacheSpec("pip cache", SHARED, (f"{C}/pip", f"{lib}/Caches/pip"), again),
            CacheSpec("uv cache", SHARED, (f"{C}/uv",), again),
            CacheSpec("Poetry cache", SHARED, (f"{C}/pypoetry", f"{lib}/Caches/pypoetry"), again),
            CacheSpec("Go build cache", SHARED, (f"{C}/go-build", f"{lib}/Caches/go-build"), "Go rebuilds it."),
            CacheSpec("NuGet caches", SHARED, (f"{H}/.local/share/NuGet/v3-cache", f"{H}/.local/share/NuGet/http-cache"),
                      again),
            CacheSpec("Composer cache", SHARED, (f"{C}/composer",), again),
            CacheSpec("Deno cache", SHARED, (f"{C}/deno", f"{lib}/Caches/deno"), again),
            CacheSpec("Playwright browsers", SHARED, (f"{C}/ms-playwright", f"{lib}/Caches/ms-playwright"),
                      "playwright install downloads them again."),
            CacheSpec("Puppeteer browsers", SHARED, (f"{C}/puppeteer",), again),
            CacheSpec("Electron downloads", SHARED, (f"{C}/electron", f"{lib}/Caches/electron"), again),
            CacheSpec("node-gyp headers", SHARED, (f"{C}/node-gyp", f"{lib}/Caches/node-gyp"), again),
            CacheSpec("Dart/Flutter packages", SHARED, (f"{H}/.pub-cache/hosted", f"{H}/.pub-cache/git"), again),
            CacheSpec("Unity package cache", SHARED, (f"{C}/unity3d/cache", f"{lib}/Unity/cache"), again, "unity"),
            CacheSpec("JetBrains IDE caches", SHARED, (f"{C}/JetBrains/*/caches", f"{lib}/Caches/JetBrains/*"),
                      "The IDE indexes your projects again.", "idea"),
            CacheSpec("Android Studio caches", SHARED, (f"{C}/Google/AndroidStudio*/caches",
                                                        f"{lib}/Caches/Google/AndroidStudio*"),
                      "Android Studio indexes your projects again.", "studio"),
            CacheSpec("VS Code caches", SHARED, tuple(f"{cfg}/Code/{c}" for c in ("Cache", "CachedData",
                                                                                 "CachedExtensionVSIXs")),
                      "VS Code rebuilds them.", "code"),
            CacheSpec("ccache", SHARED, (f"{C}/ccache", f"{lib}/Caches/ccache"), "Compilers redo the work once."),
            CacheSpec("sccache", SHARED, (f"{C}/sccache", f"{lib}/Caches/Mozilla.sccache"),
                      "Compilers redo the work once."),
            # Apple platforms
            CacheSpec("Xcode DerivedData", SHARED, (f"{lib}/Developer/Xcode/DerivedData",), "Xcode rebuilds it.",
                      "xcode"),
            CacheSpec("iOS device support files", SHARED, (f"{lib}/Developer/Xcode/iOS DeviceSupport",),
                      "Copied again from a device the next time you connect it."),
            CacheSpec("Simulator caches", SHARED, (f"{lib}/Developer/CoreSimulator/Caches",), "Rebuilt by Xcode."),
            CacheSpec("CocoaPods cache", SHARED, (f"{lib}/Caches/CocoaPods",), again),
        ]
    s += [
        CacheSpec("Gradle caches", SHARED, (f"{H}/.gradle/caches", f"{H}/.gradle/wrapper/dists"), again),
        CacheSpec("Cargo downloads", SHARED, (f"{H}/.cargo/registry/cache", f"{H}/.cargo/registry/src",
                                              f"{H}/.cargo/git/checkouts"), again),
        CacheSpec("Go modules", SHARED, (f"{H}/go/pkg/mod",), again),
        CacheSpec("NuGet packages", SHARED, (f"{H}/.nuget/packages",), "Restored on the next build."),
        CacheSpec("Bun cache", SHARED, (f"{H}/.bun/install/cache",), again),
        CacheSpec("Yarn Berry cache", SHARED, (f"{H}/.yarn/berry/cache",), again),
        CacheSpec("Android build cache", SHARED, (f"{H}/.android/build-cache", f"{H}/.android/cache"), again),
        CacheSpec("Maven repository", SHARED, (f"{H}/.m2/repository",), again,
                  warning="It can also hold your own builds installed with mvn install"),
        CacheSpec("Hugging Face models", SHARED, (f"{C if not IS_WINDOWS else H + '/.cache'}/huggingface/hub",),
                  again, warning="Models can be very large downloads"),
    ]
    return s
