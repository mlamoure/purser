"""Generate THIRD_PARTY_NOTICES.md: every third-party component Purser ships (#429).

Usage (at release, or whenever a lockfile changes):

    uv sync --frozen
    (cd frontend && npm ci) && (cd docs && npm ci)
    uv run python scripts/generate_third_party_notices.py          # writes the file
    uv run python scripts/generate_third_party_notices.py --check  # offline drift gate

The script WRITES ITS OWN OUTPUT (``--output``, default THIRD_PARTY_NOTICES.md);
never shell-redirect it. ``--check`` needs no installed packages: it compares the
component list in the committed file against the lockfiles and exits 1 on drift
(tests/test_third_party_notices.py runs the same comparison in CI).

What is inventoried, and why:

* Python: the runtime closure of the ``purser`` project in ``uv.lock`` (no dev
  groups) whose environment markers hold on the image's platforms (Linux
  x86_64/aarch64, CPython at the Dockerfile's ``python:X.Y-slim`` version) —
  exactly what ``uv sync --frozen --no-dev`` installs into ``/app/.venv``.
* Frontend: the production (non-dev) packages of ``frontend/package-lock.json``
  (a superset of what Vite bundles into the SPA and the MCP Apps widgets), plus
  build tools whose generated output lands in the bundle (FRONTEND_EMITTED).
* Docs site: the packages whose client code ships in ``docs/dist``
  (DOCS_EMITTED). The rest of the docs toolchain only runs at build time.

Licence texts are copied verbatim from the installed distributions (dist-info
licence files, npm package licence/notice files), so the venv and both
node_modules trees must be installed from the lockfiles first. A package whose
licence is not a recognised SPDX expression, or not compatible with
AGPL-3.0-only, stops the generator: review it and extend LICENSE_OVERRIDES or
COMPATIBLE with a comment saying why.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
import textwrap
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from packaging.markers import Marker

REPO = Path(__file__).resolve().parent.parent
OUTPUT = REPO / "THIRD_PARTY_NOTICES.md"

# Sections of the notices file. The machine-readable inventory is the table
# under each of these headings: `| name | version | licence | source |`.
SECTION_PYTHON = "Python packages in the container image"
SECTION_FRONTEND = "Web UI and MCP Apps widget bundles"
SECTION_DOCS = "Documentation site"
INVENTORY_SECTIONS = (SECTION_PYTHON, SECTION_FRONTEND, SECTION_DOCS)

IMAGE_PLATFORMS = ("x86_64", "aarch64")

# Build tools that emit code into the shipped bundle although they are dev
# dependencies. Tailwind generates the SPA stylesheet (preflight + utilities);
# its MIT banner is kept in the CSS output.
FRONTEND_EMITTED = {"tailwindcss": "generated SPA stylesheet"}

# Packages whose client-side code is emitted into docs/dist (checked against a
# built site: Astro's prefetch script, Starlight's UI scripts and styles,
# Expressive Code's copy button and styles, Pagefind's search UI and WASM).
DOCS_EMITTED = (
    "astro",
    "@astrojs/starlight",
    "astro-expressive-code",
    "@expressive-code/core",
    "@expressive-code/plugin-frames",
    "@expressive-code/plugin-text-markers",
    "pagefind",
    "@pagefind/default-ui",
)

# Licence metadata that is not an SPDX expression, resolved by reading the
# package's own licence file. Keyed by normalised distribution name.
LICENSE_OVERRIDES = {
    "authlib": "BSD-3-Clause",  # LICENSE: BSD 3-Clause
    "httpx": "BSD-3-Clause",  # LICENSE.md: BSD 3-Clause
    "joserfc": "BSD-3-Clause",  # LICENSE: BSD 3-Clause
    "pyperclip": "BSD-3-Clause",  # LICENSE.txt: BSD 3-Clause
    "jsonschema-path": "Apache-2.0",  # LICENSE: Apache License 2.0
    "pathable": "Apache-2.0",  # LICENSE: Apache License 2.0
    # Older contributions BSD-3-Clause, newer dual Apache-2.0/BSD-3-Clause.
    "python-dateutil": "Apache-2.0 AND BSD-3-Clause",
    "uvloop": "MIT OR Apache-2.0",  # LICENSE-MIT + LICENSE-APACHE, either
}

# Classifier → SPDX, for packages that declare only trove classifiers.
CLASSIFIER_SPDX = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: BSD License": None,  # ambiguous: needs an override
}

# Free-text `License:` values that are already a plain identifier.
LICENSE_FIELD_SPDX = {
    "MIT": "MIT",
    "MIT License": "MIT",
    "Apache-2.0": "Apache-2.0",
    "BSD-3-Clause": "BSD-3-Clause",
    "BSD-2-Clause": "BSD-2-Clause",
    "ISC": "ISC",
}

# Licences that may be combined with and conveyed alongside AGPL-3.0-only.
# Weak-copyleft entries (MPL-2.0, LGPL) cover unmodified upstream files whose
# source stays available from upstream; Purser modifies none of them.
COMPATIBLE = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BlueOak-1.0.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "PSF-2.0",
        "Python-2.0",
        "Unlicense",
    }
)

LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)


@dataclass
class Component:
    name: str
    version: str
    license: str
    source: str
    texts: list[tuple[str, str]] = field(default_factory=list)  # (file name, text)


# ---------------------------------------------------------------- lockfiles


def image_python_version(repo: Path = REPO) -> str:
    """The CPython X.Y of the final image stage (`FROM python:X.Y-slim`)."""
    versions = re.findall(
        r"^FROM python:(\d+\.\d+)-slim\b", (repo / "Dockerfile").read_text(), re.M
    )
    if not versions:
        raise SystemExit("Dockerfile: no `FROM python:X.Y-slim` stage found")
    return versions[-1]


def _marker_envs(python: str) -> list[dict[str, str]]:
    return [
        {
            "implementation_name": "cpython",
            "implementation_version": f"{python}.0",
            "os_name": "posix",
            "platform_machine": machine,
            "platform_python_implementation": "CPython",
            "platform_release": "",
            "platform_system": "Linux",
            "platform_version": "",
            "python_full_version": f"{python}.0",
            "python_version": python,
            "sys_platform": "linux",
            "extra": "",
        }
        for machine in IMAGE_PLATFORMS
    ]


def python_runtime_packages(repo: Path = REPO) -> dict[str, str]:
    """name → version for the image's runtime closure in uv.lock."""
    lock = tomllib.loads((repo / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    envs = _marker_envs(image_python_version(repo))

    def active(dep: dict) -> bool:
        marker = dep.get("marker")
        return marker is None or any(Marker(marker).evaluate(env) for env in envs)

    root = next(p for p in lock["package"] if p.get("source", {}).get("editable") == ".")
    seen: set[tuple[str, str]] = set()
    found: dict[str, str] = {}
    queue: list[tuple[str, tuple[str, ...]]] = [(root["name"], ())]
    while queue:
        name, extras = queue.pop()
        package = packages[name]
        deps = list(package.get("dependencies", []))
        for extra in extras:
            deps += package.get("optional-dependencies", {}).get(extra, [])
        for dep in deps:
            if not active(dep):
                continue
            dep_extras = tuple(dep.get("extra", ()))
            for key in [(dep["name"], "")] + [(dep["name"], e) for e in dep_extras]:
                if key in seen:
                    continue
                seen.add(key)
                found[dep["name"]] = packages[dep["name"]]["version"]
                queue.append((dep["name"], (key[1],) if key[1] else ()))
    return dict(sorted(found.items()))


def _npm_name(key: str) -> str:
    return key.rsplit("node_modules/", 1)[-1]


def frontend_packages(repo: Path = REPO) -> dict[str, str]:
    """name → version: frontend production packages + FRONTEND_EMITTED."""
    lock = json.loads((repo / "frontend" / "package-lock.json").read_text())["packages"]
    found: dict[str, str] = {}
    for key, entry in lock.items():
        if not key or entry.get("dev") or entry.get("devOptional"):
            continue
        name = _npm_name(key)
        if found.get(name, entry["version"]) != entry["version"]:
            raise SystemExit(f"frontend: two versions of {name}; list both by hand here")
        found[name] = entry["version"]
    for name in FRONTEND_EMITTED:
        found[name] = lock[f"node_modules/{name}"]["version"]
    return dict(sorted(found.items()))


def docs_packages(repo: Path = REPO) -> dict[str, str]:
    lock = json.loads((repo / "docs" / "package-lock.json").read_text())["packages"]
    return dict(sorted((name, lock[f"node_modules/{name}"]["version"]) for name in DOCS_EMITTED))


def expected_inventory(repo: Path = REPO) -> dict[str, dict[str, str]]:
    return {
        SECTION_PYTHON: python_runtime_packages(repo),
        SECTION_FRONTEND: frontend_packages(repo),
        SECTION_DOCS: docs_packages(repo),
    }


# ------------------------------------------------------------- the notices


_ROW = re.compile(r"^\| (?P<name>[^|`]+?) \| (?P<version>[^|]+?) \| (?P<license>[^|]+?) \|")


def parse_inventory(text: str) -> dict[str, dict[str, str]]:
    """section → {name: version} from the committed notices file's tables."""
    parsed: dict[str, dict[str, str]] = {s: {} for s in INVENTORY_SECTIONS}
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip() if line[3:].strip() in parsed else None
            continue
        match = _ROW.match(line)
        if section and match and match["name"] != "Package" and not match["name"].startswith("-"):
            parsed[section][match["name"]] = match["version"]
    return parsed


def parse_licenses(text: str) -> dict[str, str]:
    """`section/name` → licence expression, from the inventory tables."""
    out: dict[str, str] = {}
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip() if line[3:].strip() in INVENTORY_SECTIONS else None
            continue
        match = _ROW.match(line)
        if section and match and match["name"] != "Package" and not match["name"].startswith("-"):
            out[f"{section}/{match['name']}"] = match["license"]
    return out


def drift(text: str, repo: Path = REPO) -> list[str]:
    """Differences between the notices file and the lockfiles (empty = in sync)."""
    problems = []
    committed = parse_inventory(text)
    for section, expected in expected_inventory(repo).items():
        have = committed.get(section, {})
        for name, version in expected.items():
            if name not in have:
                problems.append(f"{section}: {name} {version} missing from the notices")
            elif have[name] != version:
                problems.append(f"{section}: {name} is {version}, notices say {have[name]}")
        for name in have.keys() - expected.keys():
            problems.append(f"{section}: {name} is listed but no longer shipped")
    return problems


def is_compatible(expression: str) -> bool:
    """True when an SPDX expression (AND/OR, no parentheses) is AGPL-compatible."""
    if " AND " in expression and " OR " in expression:
        return False  # not used by any component; review by hand if it appears
    if " OR " in expression:
        return any(part.strip() in COMPATIBLE for part in expression.split(" OR "))
    return all(part.strip() in COMPATIBLE for part in expression.split(" AND "))


# --------------------------------------------------- installed metadata


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_license(name: str, meta: importlib.metadata.PackageMetadata) -> str:
    if _norm(name) in LICENSE_OVERRIDES:
        return LICENSE_OVERRIDES[_norm(name)]
    if meta.get("License-Expression"):
        return str(meta["License-Expression"])
    field_value = (meta.get("License") or "").strip()
    if field_value in LICENSE_FIELD_SPDX:
        return LICENSE_FIELD_SPDX[field_value]
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    ids = [CLASSIFIER_SPDX.get(c) for c in classifiers]
    if classifiers and all(ids):
        return " OR ".join(sorted({i for i in ids if i}))
    raise SystemExit(f"{name}: licence {field_value[:60]!r} {classifiers} needs an override")


def _python_source(meta: importlib.metadata.PackageMetadata, name: str) -> str:
    urls = {}
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls[label.strip().lower()] = url.strip()
    for label in ("source", "source code", "repository", "code", "github", "homepage"):
        if urls.get(label):
            return _clean_url(urls[label])
    return _clean_url(meta.get("Home-page") or f"https://pypi.org/project/{name}/")


def _clean_url(url: str) -> str:
    """A browsable https URL from a git/ssh/shorthand repository reference."""
    url = re.sub(r"^git\+", "", url.strip())
    url = re.sub(r"^(?:git|ssh)://(?:git@)?", "https://", url)
    url = re.sub(r"^https://git@", "https://", url)
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)
    if url.startswith("github:"):
        url = "https://github.com/" + url.removeprefix("github:")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        url = f"https://github.com/{url}"
    return re.sub(r"\.git$", "", url)


def _license_files(directory: Path) -> list[tuple[str, str]]:
    """(name, text) of LICENSE/NOTICE/COPYING/AUTHORS files, or files inside a
    directory so named, directly under ``directory``."""
    found = []
    for entry in sorted(directory.iterdir()):
        if not LICENSE_FILE.match(entry.name):
            continue
        files = sorted(entry.iterdir()) if entry.is_dir() else [entry]
        found += [(f"{entry.name}/{f.name}" if entry.is_dir() else f.name, f) for f in files]
    return [(name, f.read_text(errors="replace")) for name, f in found if f.is_file()]


def python_components(expected: dict[str, str]) -> list[Component]:
    components = []
    for name, version in expected.items():
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            raise SystemExit(f"{name} is not installed — run `uv sync --frozen` first") from None
        if dist.version != version:
            raise SystemExit(f"{name}: installed {dist.version}, uv.lock pins {version}")
        texts = []
        for file in sorted(dist.files or [], key=str):
            parts = Path(str(file)).parts
            in_metadata = parts[0].endswith(".dist-info") and "sboms" not in parts
            if in_metadata and (LICENSE_FILE.match(parts[-1]) or "licenses" in parts):
                path = Path(str(dist.locate_file(file)))
                texts.append((parts[-1], path.read_text(errors="replace")))
        components.append(
            Component(
                name,
                version,
                _python_license(name, dist.metadata),
                _python_source(dist.metadata, name),
                texts,
            )
        )
    return components


def _npm_source(manifest: dict, name: str) -> str:
    repo = manifest.get("repository")
    url = repo.get("url") if isinstance(repo, dict) else repo
    if not url:
        return manifest.get("homepage") or f"https://www.npmjs.com/package/{name}"
    return _clean_url(url)


def npm_components(root: Path, expected: dict[str, str]) -> list[Component]:
    lock = json.loads((root / "package-lock.json").read_text())["packages"]
    by_name_version = {(_npm_name(k), v.get("version")): k for k, v in lock.items() if k}
    components = []
    for name, version in expected.items():
        key = by_name_version.get((name, version))
        if key is None:
            raise SystemExit(f"{root.name}: {name} {version} not in package-lock.json")
        pkg_dir = root / key
        manifest_path = pkg_dir / "package.json"
        if not manifest_path.exists():
            raise SystemExit(f"{pkg_dir} missing — run `npm ci` in {root.name}/ first")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("version") != version:
            raise SystemExit(f"{name}: installed {manifest.get('version')}, lock pins {version}")
        license_expr = lock[key].get("license") or manifest.get("license")
        if not isinstance(license_expr, str):
            raise SystemExit(f"{name}: licence {license_expr!r} needs an override")
        license_expr = license_expr.strip("()")
        texts = _license_files(pkg_dir)
        components.append(
            Component(name, version, license_expr, _npm_source(manifest, name), texts)
        )
    return components


# --------------------------------------------------------------- rendering


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}text\n{text.rstrip()}\n{ticks}"


INTRO = """\
# Third-party notices

Generated by `scripts/generate_third_party_notices.py` from `uv.lock`,
`frontend/package-lock.json` and `docs/package-lock.json`; do not edit by hand.
Regenerate after any dependency change (the test suite fails on drift).

Purser itself is licensed under the GNU Affero General Public License, version 3
only (`AGPL-3.0-only`, see `LICENSE`). A running instance offers its users the
Corresponding Source through the "Source code" link in the web UI footer and the
`GET /source` route, which redirect to `PURSER_SOURCE_URL`.

The container image carries this file and `LICENSE` in `/usr/share/doc/purser/`.
The web UI serves the licences of the code bundled into it at
`/third-party-licenses.txt`; each MCP Apps widget carries its own in a trailing
HTML comment.
"""

PYTHON_NOTE = """\
Installed in `/app/.venv` from `uv.lock` (runtime dependencies only, Linux
x86_64 and aarch64, CPython {python}). Each distribution's own licence files also
ship in the image, in its `*.dist-info/` directory.

`psycopg` and `psycopg-binary` are LGPL-3.0-only, used unmodified; they stay
replaceable in `/app/.venv`, and their source is at the URL below. The GNU GPL
v3 that the LGPL supplements is at `/usr/share/common-licenses/GPL-3` in the
image and at https://www.gnu.org/licenses/gpl-3.0.txt.

Native code bundled inside these wheels:

- `psycopg-binary` vendors libpq (PostgreSQL License), OpenSSL 3 (Apache-2.0),
  MIT Kerberos (krb5, libcom_err; MIT), Cyrus SASL (BSD-style with an
  acknowledgement clause: "This product includes software developed by
  Computing Services at Carnegie Mellon University
  (http://www.cmu.edu/computing/)."), OpenLDAP libldap/liblber (OLDAP-2.8),
  libxcrypt (LGPL-2.1-or-later), keyutils (LGPL-2.1-or-later), libselinux
  (public domain) and PCRE (BSD-3-Clause) in `psycopg_binary.libs/`.
- `cryptography`, `pydantic-core`, `rpds-py` and `watchfiles` statically link
  Rust crates; their CycloneDX SBOMs (crate, version, licence) ship in the
  image under each wheel's `*.dist-info/sboms/`.
"""

FRONTEND_NOTE = """\
The production packages of `frontend/package-lock.json`, a superset of what
Vite bundles: the SPA bundles React, React Router, TanStack Query,
openapi-fetch, clsx, class-variance-authority and tailwind-merge, plus the
stylesheet Tailwind CSS generates; the widgets bundle the MCP Apps SDK and its
schema/PKCE helpers. Packages only Node.js uses (e.g. `cross-spawn`) are listed
because the lockfile marks them as production dependencies.
"""

DOCS_NOTE = """\
Client-side code emitted into the static docs site (`docs/dist`). The rest of
`docs/package-lock.json` runs only at build time and is not distributed.
"""

BASE_IMAGE = """\
## Container base image

The image is built `FROM python:{python}-slim` (Debian 13 "trixie"). CPython is
licensed under the PSF License Agreement (`PSF-2.0`); its licence text ships at
`/usr/local/lib/python{python}/LICENSE.txt`. The Debian packages keep their
copyright and licence files in `/usr/share/doc/*/copyright`; list them with
`dpkg-query -W` inside the image. Many are under the GPL or LGPL, unmodified.
Their complete corresponding source, at the exact version in the image, is
available from the Debian archive (https://snapshot.debian.org/, or
`apt-get source PACKAGE=VERSION`). Node.js and `uv` are used only in build
stages and are not in the image.
"""

ASSETS = """\
## Assets in this repository

Every icon, stylesheet, dashboard and fixture in the tree is Purser's own work,
licensed with it under AGPL-3.0-only: the docs favicon (`docs/public/favicon.svg`),
the Grafana dashboards (`grafana/dashboards/`), the widget theme
(`frontend/widgets/lib/theme.css`), the generated OpenAPI document
(`frontend/openapi.json`) and the synthetic test fixtures. Purser bundles no
third-party fonts or images: the UI uses the platform's system fonts.

The structure of the default category tree (`purser/core/categories.py`) —
which categories exist and how they are grouped — is adapted from Plaid's
published Personal Finance Category taxonomy
(https://plaid.com/docs/transactions/pfc-migration/), with Purser's own
structure changes; Plaid, Inc. is credited here as its source. The category
descriptions are not Plaid's: Purser wrote its own, for its classifier.
"""


def _table(components: list[Component]) -> list[str]:
    rows = ["| Package | Version | Licence | Source |", "| --- | --- | --- | --- |"]
    rows += [f"| {c.name} | {c.version} | {c.license} | {c.source} |" for c in components]
    return rows


def render(sections: dict[str, list[Component]], python: str) -> str:
    out = [INTRO]
    counts = Counter(c.license for comps in sections.values() for c in comps)
    out += ["## Licence summary", "", "| Licence | Components |", "| --- | --- |"]
    out += [f"| {lic} | {n} |" for lic, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    out.append("")
    notes = {
        SECTION_PYTHON: PYTHON_NOTE.format(python=python),
        SECTION_FRONTEND: FRONTEND_NOTE,
        SECTION_DOCS: DOCS_NOTE,
    }
    for title in INVENTORY_SECTIONS:
        out += [f"## {title}", "", notes[title], *_table(sections[title]), ""]
    out += [BASE_IMAGE.format(python=python), ASSETS]

    # Licence texts, verbatim; identical texts are printed once.
    grouped: dict[str, list[str]] = defaultdict(list)
    texts: dict[str, tuple[str, str]] = {}
    missing = []
    for comps in sections.values():
        for c in comps:
            if not c.texts:
                missing.append(f"{c.name} {c.version}")
            for file_name, text in c.texts:
                # Whitespace-insensitive: reflowed copies of one licence print once.
                digest = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
                label = f"{c.name} {c.version} ({file_name})"
                if label not in grouped[digest]:
                    grouped[digest].append(label)
                texts.setdefault(digest, (file_name, text))
    out += ["## Licence and notice texts", ""]
    if missing:
        paragraph = (
            "These packages publish no licence file in the artifact Purser installs: "
            + ", ".join(sorted(missing))
            + ". Their licence is the one in the tables above; the full text with its"
            " copyright lines is in the upstream source repository linked there."
        )
        out += [textwrap.fill(paragraph, width=80, break_on_hyphens=False), ""]
    for digest in sorted(grouped, key=lambda d: grouped[d][0].lower()):
        out += ["### " + grouped[digest][0], ""]
        if len(grouped[digest]) > 1:
            out += ["Also: " + "; ".join(grouped[digest][1:]) + ".", ""]
        out += [_fence(texts[digest][1]), ""]
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="compare the file with the lockfiles")
    args = parser.parse_args()

    if args.check:
        problems = drift(args.output.read_text())
        for problem in problems:
            print(problem, file=sys.stderr)
        sys.exit(1 if problems else 0)

    expected = expected_inventory()
    sections = {
        SECTION_PYTHON: python_components(expected[SECTION_PYTHON]),
        SECTION_FRONTEND: npm_components(REPO / "frontend", expected[SECTION_FRONTEND]),
        SECTION_DOCS: npm_components(REPO / "docs", expected[SECTION_DOCS]),
    }
    bad = [
        f"{c.name} {c.version}: {c.license}"
        for comps in sections.values()
        for c in comps
        if not is_compatible(c.license)
    ]
    if bad:
        raise SystemExit("Not known to be AGPL-3.0-compatible:\n  " + "\n  ".join(bad))
    args.output.write_text(render(sections, image_python_version()))
    total = sum(len(c) for c in sections.values())
    print(f"wrote {args.output.relative_to(REPO)}: {total} components", file=sys.stderr)


if __name__ == "__main__":
    main()
