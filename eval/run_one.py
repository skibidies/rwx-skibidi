"""Apply one patch to the pinned tree, run the suite, emit failing nodeids."""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "/src")
from xml.etree import ElementTree  # noqa: E402


def main(short: str, out_dir: str, workers: str = "8") -> None:
    work = f"/tmp/w-{short}"
    if os.path.isdir(work):
        shutil.rmtree(work)
    shutil.copytree("/src", work, ignore=shutil.ignore_patterns(".git"))
    patch = f"/patches/{short}.patch"
    ap = subprocess.run(["patch", "-p1", "--batch", "-s", "-i", patch],
                        cwd=work, capture_output=True, text=True)
    if short != "none" and ap.returncode != 0:
        res = {"short": short, "apply_error": ap.stderr.strip()[:2000]}
    else:
        xml = f"/tmp/r-{short}.xml"
        targets = ["tests"]
        if os.path.exists("/tests.txt"):
            picked = [ln for ln in open("/tests.txt").read().split("\n") if ln.strip()]
            if picked:
                targets = picked
        subprocess.run(
            [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider",
             "--tb=no", "-n", workers, f"--junitxml={xml}"],
            cwd=work, capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        failed, errors = [], []
        try:
            root = ElementTree.parse(xml).getroot()
        except Exception as exc:  # noqa: BLE001
            root = None
            errors.append(f"no junit: {exc}")
        if root is not None:
            for case in root.iter("testcase"):
                bad = case.find("failure") is not None or case.find("error") is not None
                if not bad:
                    continue
                cls, name = case.get("classname", ""), case.get("name", "")
                # classname -> path.nodeid form used by tests.json
                parts = cls.split(".")
                fidx = next((i for i, p in enumerate(parts) if p == "tests"), None)
                if fidx is None:
                    errors.append(f"odd classname {cls}")
                    continue
                path = "/".join(parts[fidx:-1]) + ".py" if len(parts[fidx:-1]) >= 1 else cls
                failed.append(f"{path}::{parts[-1]}::{name}")
        res = {"short": short, "failing": sorted(set(failed)), "errors": errors}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{short}.json"), "w") as fh:
        json.dump(res, fh, indent=1)
    print(f"{short}: {len(res.get('failing', []))} failing {res.get('apply_error', '')}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "/out",
         sys.argv[3] if len(sys.argv) > 3 else "8")
