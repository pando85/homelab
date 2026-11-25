#!/usr/bin/env python3
"""Assert the voice/music deployment's key GitOps invariants.

Usage: python test/voice_music_gitops_test.py /tmp/music-assistant-rendered.yaml
Requires PyYAML, installed by the corresponding workflow.
"""

import pathlib
import sys

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def parse_esphome(path):
    # ESPHome uses !secret, unlike ordinary YAML. Validate its source keys
    # without replacing/printing secrets or fetching GitHub packages.
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: "secret:" + loader.construct_scalar(node))
    return yaml.load(path.read_text(), Loader=Loader)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: voice_music_gitops_test.py HELM_RENDERED_YAML")
    rendered = list(yaml.safe_load_all(pathlib.Path(sys.argv[1]).read_text()))
    kinds = {(x.get("kind"), x["metadata"]["name"]): x for x in rendered if x}
    statefulsets = [x for x in rendered if x and x.get("kind") == "StatefulSet"]
    assert len(statefulsets) == 1, "Expected exactly one Music Assistant StatefulSet"
    spec = statefulsets[0]["spec"]["template"]["spec"]
    assert spec["hostNetwork"] is True
    assert spec["dnsPolicy"] == "ClusterFirstWithHostNet"
    assert spec["nodeSelector"]["kubernetes.io/hostname"] == "prusik"
    assert spec["automountServiceAccountToken"] is False
    assert spec["terminationGracePeriodSeconds"] >= 30
    container = spec["containers"][0]
    assert "ghcr.io/music-assistant/server:2.10.9@sha256:" in container["image"]
    assert any(m["mountPath"] == "/data" for m in container["volumeMounts"])
    assert ("PersistentVolumeClaim", "music-assistant-data") in kinds
    assert ("SnapshotSchedule", "music-assistant-zfs-backups") in kinds
    ingresses = [x for x in rendered if x and x.get("kind") == "Ingress"]
    assert len(ingresses) == 1
    ingress = ingresses[0]
    assert ingress["spec"]["ingressClassName"] == "nginx-internal"
    assert "auth-url" in " ".join(ingress["metadata"]["annotations"].keys())
    assert "music-assistant.internal.grigri.cloud" in [
        rule["host"] for rule in ingress["spec"]["rules"]
    ]
    services = [x for x in rendered if x and x.get("kind") == "Service"]
    assert len(services) == 1
    assert {p["port"] for p in services[0]["spec"]["ports"]} == {8095, 8097}

    device = parse_esphome(ROOT / "apps/esphome/config/respeaker-xvf3800-kitchen.yaml")
    assert device["substitutions"]["respeaker_ref"] == "2026.10.4"
    assert device["packages"]["respeaker"]["ref"] == "${respeaker_ref}"
    assert len(device["packages"]["respeaker"]["files"]) == 5
    assert device["api"]["encryption"]["key"] == "secret:home_assistant_api_key"
    assert device["ota"][0]["password"] == "secret:ota_password"
    assert device["wifi"]["domain"] == "secret:domain_devices"

    ha = (ROOT / "apps/home-assistant/values.yaml").read_text()
    assert "location = /api/prometheus" in ha, "Existing metrics ingress block lost"
    assert "terminationGracePeriodSeconds: 45" in ha
    print("voice/music GitOps assertions passed")


if __name__ == "__main__":
    main()
