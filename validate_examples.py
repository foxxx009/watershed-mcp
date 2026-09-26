#!/usr/bin/env python3
"""Validate that every reading in examples/ conforms to sensor_schema.json
(the bounty-#450 standard subset). Zero dependencies."""

import json
import os
import sys

REQUIRED = ["timestamp", "location", "unit", "value", "sensor_id", "source"]
SCHEMA = json.load(open(os.path.join(os.path.dirname(__file__), "sensor_schema.json")))
PROPS = set(SCHEMA["properties"])


def check_reading(r):
    errs = []
    for k in REQUIRED:
        if k not in r:
            errs.append("missing required field %r" % k)
    if "value" in r and not isinstance(r["value"], (int, float)):
        errs.append("value is not a number")
    loc = r.get("location")
    if loc is not None:
        if not isinstance(loc, dict):
            errs.append("location is not an object")
        else:
            lat, lng = loc.get("lat"), loc.get("lng")
            if lat is not None and not (-90 <= lat <= 90):
                errs.append("lat out of range: %s" % lat)
            if lng is not None and not (-180 <= lng <= 180):
                errs.append("lng out of range: %s" % lng)
    for k in r:
        if k not in PROPS:
            errs.append("unknown field %r" % k)
    return errs


def main():
    base = os.path.join(os.path.dirname(__file__), "examples")
    failures = 0
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        doc = json.load(open(path, encoding="utf-8"))
        readings = doc.get("readings") or []
        n_err = 0
        for i, r in enumerate(readings):
            for e in check_reading(r):
                print("FAIL %s[%d]: %s" % (name, i, e))
                n_err += 1
        if n_err == 0:
            print("PASS %s (%d readings)" % (name, len(readings)))
        failures += n_err
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
