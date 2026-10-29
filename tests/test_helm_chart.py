"""The experimental Helm chart (charts/purser, #429): secure defaults.

The value-file checks always run. The rendering checks need the `helm`
binary (or HELM_BIN) and are skipped without it; CI does not install helm,
so run them locally before changing the chart. Every key and password here is
generated per run: the chart ships none, and neither do its tests.
"""

import os
import re
import secrets
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.fernet import Fernet

REPO = Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "purser"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")

needs_helm = pytest.mark.skipif(HELM is None, reason="helm not installed (set HELM_BIN)")


def _values() -> dict[str, Any]:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def _chart() -> dict[str, Any]:
    return yaml.safe_load((CHART / "Chart.yaml").read_text())


def test_app_version_matches_pyproject() -> None:
    """The default image tag is v + appVersion, so a release bump that
    forgets the chart would pin installs to the previous release."""
    with (REPO / "pyproject.toml").open("rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert _chart()["appVersion"] == expected


def test_chart_is_labelled_experimental() -> None:
    assert "EXPERIMENTAL" in _chart()["description"]
    assert "EXPERIMENTAL" in (CHART / "values.yaml").read_text()


def test_values_ship_no_secrets_and_a_public_image() -> None:
    v = _values()
    assert v["secrets"]["encryptionKey"] == ""
    assert v["secrets"]["databaseUrl"] == ""
    assert v["secrets"]["extra"] == {}
    assert v["postgres"]["password"] == ""
    assert v["postgres"]["mode"] == "external"
    assert v["image"]["repository"].startswith("ghcr.io/")
    assert v["image"]["tag"] == ""
    assert v["image"]["pullPolicy"] == "IfNotPresent"
    assert v["ingress"]["enabled"] is False
    assert v["service"]["type"] == "ClusterIP"


def test_run_as_user_matches_the_image() -> None:
    dockerfile = (REPO / "Dockerfile").read_text()
    uid = re.search(r"useradd -r -u (\d+)", dockerfile)
    gid = re.search(r"groupadd -r -g (\d+)", dockerfile)
    assert uid and gid, "the Dockerfile must pin the purser UID/GID"
    ctx = _values()["podSecurityContext"]
    assert ctx["runAsUser"] == int(uid.group(1))
    assert ctx["runAsGroup"] == int(gid.group(1))
    assert ctx["runAsNonRoot"] is True


def _render(*args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "purser", str(CHART), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _docs(*args: str) -> list[dict[str, Any]]:
    result = _render(*args)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _external() -> list[str]:
    return [
        "--set",
        f"secrets.encryptionKey={Fernet.generate_key().decode()}",
        "--set",
        "secrets.databaseUrl=postgresql+psycopg://purser:pw@db.example.com:5432/purser",
    ]


def _pod_specs(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        d["metadata"]["name"]: d["spec"]["template"]["spec"]
        for d in docs
        if d["kind"] in ("Deployment", "StatefulSet")
    }


@needs_helm
def test_helm_lint_passes() -> None:
    assert HELM is not None
    result = subprocess.run(
        [HELM, "lint", "--strict", str(CHART), *_external()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_helm
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("mode", ["external", "bundled"])
def test_every_pod_is_locked_down(worker: bool, mode: str) -> None:
    args = [*_external(), "--set", f"worker.enabled={str(worker).lower()}"]
    if mode == "bundled":
        args += [
            "--set",
            "postgres.mode=bundled",
            "--set",
            f"postgres.password={secrets.token_hex(24)}",
        ]
    pods = _pod_specs(_docs(*args))
    assert len(pods) == 1 + worker + (mode == "bundled")
    for name, spec in pods.items():
        assert spec["automountServiceAccountToken"] is False, name
        ctx = spec["securityContext"]
        assert ctx["runAsNonRoot"] is True, name
        assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}, name
        for c in spec["containers"]:
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True, name
            assert sc["allowPrivilegeEscalation"] is False, name
            assert sc["capabilities"]["drop"] == ["ALL"], name
            assert c["resources"]["limits"]["memory"], name
            # No secret value travels as a plain env entry.
            for e in c.get("env", []):
                if e["name"] in (
                    "POSTGRES_PASSWORD",
                    "PURSER_ENCRYPTION_KEY",
                    "PURSER_DATABASE_URL",
                ):
                    assert "valueFrom" in e and "value" not in e, (name, e["name"])


@needs_helm
def test_api_defaults() -> None:
    docs = _docs(*_external())
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == ["Deployment", "Secret", "Service", "ServiceAccount"]
    (api,) = [d for d in docs if d["kind"] == "Deployment"]
    # The API runs the scheduler when there is no worker: no rollout overlap.
    assert api["spec"]["strategy"] == {"type": "Recreate"}
    (c,) = api["spec"]["template"]["spec"]["containers"]
    assert c["image"] == f"ghcr.io/mlamoure/purser:v{_chart()['appVersion']}"
    assert c["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert c["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert "tcpSocket" in c["livenessProbe"]
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    assert svc["spec"]["type"] == "ClusterIP"


@needs_helm
def test_worker_split_runs_one_scheduler() -> None:
    docs = _docs(*_external(), "--set", "worker.enabled=true", "--set", "api.replicas=2")
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    worker = deployments["purser-worker"]
    assert worker["spec"]["replicas"] == 1
    assert worker["spec"]["strategy"] == {"type": "Recreate"}
    api_env = deployments["purser-api"]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "PURSER_DISABLE_SCHEDULER", "value": "true"} in api_env


@needs_helm
def test_digest_pins_the_image() -> None:
    digest = "sha256:" + "ab" * 32
    docs = _docs(*_external(), "--set", f"image.digest={digest}", "--set", "image.tag=v9.9.9")
    (api,) = [d for d in docs if d["kind"] == "Deployment"]
    assert api["spec"]["template"]["spec"]["containers"][0]["image"].endswith(f":v9.9.9@{digest}")


@needs_helm
def test_existing_secret_renders_no_secret() -> None:
    docs = _docs("--set", "existingSecret=purser-secrets")
    assert not [d for d in docs if d["kind"] == "Secret"]
    (api,) = [d for d in docs if d["kind"] == "Deployment"]
    env_from = api["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert env_from == [{"secretRef": {"name": "purser-secrets"}}]


@needs_helm
def test_secrets_extra_go_to_the_secret() -> None:
    docs = _docs(*_external(), "--set", "secrets.extra.PURSER_OIDC_CLIENT_SECRET=s3")
    (secret,) = [d for d in docs if d["kind"] == "Secret"]
    assert secret["stringData"]["PURSER_OIDC_CLIENT_SECRET"] == "s3"


@needs_helm
def test_bundled_postgres_reads_its_password_from_a_secret() -> None:
    password = secrets.token_hex(24)
    docs = _docs(
        *_external()[:2],
        "--set",
        "postgres.mode=bundled",
        "--set",
        f"postgres.password={password}",
        "--set",
        "networkPolicy.enabled=true",
    )
    secrets_by_name = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Secret"}
    assert secrets_by_name["purser-postgres"]["stringData"]["POSTGRES_PASSWORD"] == password
    dsn = secrets_by_name["purser-secrets"]["stringData"]["PURSER_DATABASE_URL"]
    assert dsn == f"postgresql+psycopg://purser:{password}@purser-postgres:5432/purser"
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    assert password not in yaml.safe_dump(sts)
    policies = {d["metadata"]["name"] for d in docs if d["kind"] == "NetworkPolicy"}
    assert policies == {"purser-api", "purser-postgres"}


@needs_helm
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "secrets.encryptionKey is required"),
        (["--set", "secrets.encryptionKey=k"], "secrets.databaseUrl is required"),
        (
            ["--set", "secrets.encryptionKey=k", "--set", "postgres.mode=bundled"],
            "needs postgres.password",
        ),
        (
            [
                "--set",
                "secrets.encryptionKey=k",
                "--set",
                "postgres.mode=bundled",
                "--set",
                "postgres.password=short",
            ],
            "postgres.password must be at least 16",
        ),
        (["EXTERNAL", "--set", "env.PURSER_OIDC_CLIENT_SECRET=x"], "looks like a secret"),
        (["EXTERNAL", "--set", "env.PURSER_AI_API_KEY=x"], "looks like a secret"),
        (["EXTERNAL", "--set", "api.replicas=2"], "api.replicas must be 1"),
        (
            ["EXTERNAL", "--set", "worker.enabled=true", "--set", "worker.replicas=2"],
            "worker.replicas must be 1",
        ),
        (["EXTERNAL", "--set", "image.digest=latest"], "image.digest must look like"),
        (["EXTERNAL", "--set", "postgres.mode=cnpg"], "postgres.mode must be"),
    ],
)
def test_refuses_unsafe_values(args: list[str], message: str) -> None:
    if args[:1] == ["EXTERNAL"]:
        args = [*_external(), *args[1:]]
    result = _render(*args)
    assert result.returncode != 0
    assert message in result.stderr
