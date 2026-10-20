"""Small immutable launch identity shared with job-local startup hooks."""

import hashlib

from .. import reporting as R


def plan_inventory(plan):
    keys = (
        "schema",
        "token",
        "project_root",
        "script_relative",
        "script_sha256",
        "execution",
        "filesystem_id",
        "filesystem_host",
        "features",
        "gpu",
        "trace",
    )
    return {
        **{key: plan[key] for key in keys if key in plan},
        "plan_path": ".raven/wrappers/" + plan["token"] + "/plan.json",
        "plan_sha256": hashlib.sha256(R._encoded(plan)).hexdigest(),
    }
