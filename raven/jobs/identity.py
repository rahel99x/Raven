"""Read-only scheduler job identifier grammar."""

import re

JOB_ID = re.compile(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?\Z")


def valid_job_id(value):
    if value == "all" or JOB_ID.fullmatch(value):
        return value
    raise ValueError("use all or an individual numeric job ID, such as 123 or 123_4")
