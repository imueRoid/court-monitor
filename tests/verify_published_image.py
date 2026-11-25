"""Check real published Docker V2 manifests and their runtime build identity."""
import json
import re
import sys
import time
import requests

REPOSITORY = "imueroid/court-monitor"
MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"


def verify(version, revision):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid release identity")
    session = requests.Session()
    token = session.get("https://auth.docker.io/token", params={"service":"registry.docker.io", "scope":f"repository:{REPOSITORY}:pull"}, timeout=30)
    token.raise_for_status()
    session.headers.update(Authorization="Bearer " + token.json()["token"], Accept=MEDIA_TYPE)
    base = f"https://registry-1.docker.io/v2/{REPOSITORY}"
    digests = {}
    labels = None
    for tag in (version, "latest", ".".join(version.split(".")[:2]), version.split(".")[0], "sha-" + revision):
        response = session.get(base + "/manifests/" + tag, timeout=30)
        response.raise_for_status()
        manifest = response.json()
        assert manifest.get("mediaType") == MEDIA_TYPE and "manifests" not in manifest, "Expected a single-platform Docker V2 manifest"
        digests[tag] = response.headers["Docker-Content-Digest"]
        if labels is None:
            config = session.get(base + "/blobs/" + manifest["config"]["digest"], timeout=30)
            config.raise_for_status()
            image = config.json()
            assert image["architecture"] == "amd64" and image["os"] == "linux"
            labels = image["config"]["Labels"]
    assert len(set(digests.values())) == 1, "Published aliases do not refer to the same image"
    assert labels["org.opencontainers.image.version"] == version
    assert labels["org.opencontainers.image.revision"] == revision
    assert labels.get("org.opencontainers.image.created")
    return {"version":version, "revision":revision, "media_type":MEDIA_TYPE, "digest":next(iter(digests.values())), "tags":list(digests)}


if __name__ == "__main__":
    for attempt in range(4):
        try:
            print(json.dumps(verify(sys.argv[1], sys.argv[2])))
            break
        except (requests.RequestException, AssertionError):
            if attempt == 3:
                raise
            time.sleep(5)
