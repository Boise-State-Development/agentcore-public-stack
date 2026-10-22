#!/usr/bin/env python3
"""Resync the fine-tuning instance rates in ``fine_tuning/pricing.py``.

Queries the AWS Price List API for the SageMaker ``Training`` and
``BatchTransform`` components in us-west-2 and prints the two rate maps.  Run
it when AWS changes prices or when adding an instance family, then paste the
output into ``pricing.py`` — the maps stay literal so they are reviewable in a
diff and need no network access at import time.

Usage:
    python backend/scripts/refresh_instance_pricing.py --profile dev-ai
"""

import argparse
import json
import subprocess
import sys

# Instance families we are willing to offer.  Anything not listed here is not
# priced, and therefore rejected at job creation.
INSTANCES = [
    f"ml.{fam}.{size}"
    for fam in ("g5", "g6", "g6e")
    for size in ("xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "24xlarge", "48xlarge")
] + ["ml.g5.16xlarge"]

REGION = "us-west-2"


def fetch(instance: str, profile: str) -> dict:
    """Return {component: usd_per_hour} for one instance type."""
    result = subprocess.run(
        [
            "aws", "pricing", "get-products",
            "--profile", profile, "--region", "us-east-1",
            "--service-code", "AmazonSageMaker",
            "--filters", f"Type=TERM_MATCH,Field=regionCode,Value={REGION}",
            f"Type=TERM_MATCH,Field=instanceName,Value={instance}",
            "--max-results", "40", "--output", "json",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ! {instance}: {result.stderr.strip()[:120]}", file=sys.stderr)
        return {}

    rates = {}
    for entry in json.loads(result.stdout).get("PriceList", []):
        obj = json.loads(entry)
        component = obj["product"]["attributes"].get("component")
        if component not in ("Training", "BatchTransform"):
            continue
        for term in obj["terms"].get("OnDemand", {}).values():
            for dimension in term["priceDimensions"].values():
                usd = float(dimension["pricePerUnit"]["USD"])
                if usd > 0:
                    rates[component] = usd
    return rates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev-ai", help="AWS profile to query with")
    args = parser.parse_args()

    training, transform = {}, {}
    for instance in INSTANCES:
        rates = fetch(instance, args.profile)
        if "Training" in rates:
            training[instance] = rates["Training"]
        if "BatchTransform" in rates:
            transform[instance] = rates["BatchTransform"]

    for name, rates in (("TRAINING_COST_PER_HOUR", training), ("TRANSFORM_COST_PER_HOUR", transform)):
        print(f"\n{name}: Dict[str, float] = {{")
        for instance, usd in rates.items():
            print(f'    "{instance}": {usd},')
        print("}")

    missing = [i for i in INSTANCES if i not in training]
    if missing:
        print(f"\n# No on-demand training rate (do not offer these): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
