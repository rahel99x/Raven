"""Explicit factorial campaigns and association estimates, never causal shares.

The compiler emits ordinary reviewed diagnostic recipes. This module launches
nothing and does not infer that requested GPU/BLAS/MPI controls took effect.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import random

from . import diagnostic_trials as D, science_stats as S

SCHEMA = "raven.factorial-design/v1"
RESULT_SCHEMA = "raven.factorial-results/v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def prepare(document):
    D._object(document, {"schema", "id", "seed", "order", "repetitions", "factors", "recipe", "equivalence"}, {"incompatible"})
    if document["schema"] != SCHEMA or document["order"] not in {"randomized", "blocked"}:
        raise ValueError("Choose a factorial-design/v1 with randomized or blocked order")
    D._name(document["id"]); D._integer(document["seed"], 0, 2**32 - 1)
    repetitions = D._integer(document["repetitions"], 3, 10)
    factors = document["factors"]
    if not isinstance(factors, list) or not 1 <= len(factors) <= 4:
        raise ValueError("Declare one to four compatible two-level factors")
    names = set()
    for factor in factors:
        D._object(factor, {"id", "levels"})
        if D._name(factor["id"]) in names or factor["id"] == "intercept": raise ValueError("Duplicate or reserved factor ID")
        names.add(factor["id"])
        if not isinstance(factor["levels"], list) or len(factor["levels"]) != 2:
            raise ValueError("Each factor needs two declared compatible levels")
        level_names = set()
        for level in factor["levels"]:
            D._object(level, {"id", "arguments"})
            if D._name(level["id"]) in level_names: raise ValueError("Duplicate factor level")
            level_names.add(level["id"]); D._arguments(level["arguments"])
    incompatible = document.get("incompatible", [])
    if not isinstance(incompatible, list) or len(incompatible) > 32: raise ValueError("Invalid compatibility constraints")
    for item in incompatible:
        if not isinstance(item, dict) or not item or not set(item) <= names:
            raise ValueError("Compatibility constraints must name declared factor levels")
        for name, level in item.items():
            factor = next(value for value in factors if value["id"] == name)
            if level not in {value["id"] for value in factor["levels"]}:
                raise ValueError("Compatibility constraint names an unknown level")
    if incompatible:
        raise ValueError("This full-factorial design includes incompatible combinations; select a compatible factor subset instead of silently dropping cells")
    equivalence = D._object(document["equivalence"], {"reference", "absolute", "relative", "semantic", "unit"})
    for key in ("reference", "absolute", "relative"):
        S.number(equivalence[key], key, nonnegative=key != "reference")
    for key in ("semantic", "unit"):
        if not isinstance(equivalence[key], str) or not 1 <= len(equivalence[key]) <= 256:
            raise ValueError("Equivalence requires bounded semantic and unit labels")
    recipe = copy.deepcopy(document["recipe"])
    D.validate(recipe)
    if recipe["kind"] != "campaign" or "{result}" not in recipe["command"]:
        raise ValueError("Factorial execution reuses a campaign recipe with an explicit {result} argument")
    if len(recipe["cases"]) != 1 or recipe["cases"][0]["arguments"]:
        raise ValueError("Use one empty template case; put shared arguments in command")
    if recipe["limits"]["repeats"] != 1:
        raise ValueError("Set recipe repeats=1; design repetitions already identify every independent trial")
    cells = list(itertools.product((0, 1), repeat=len(factors)))
    schedule = []
    rng = random.Random(document["seed"])
    for block in range(repetitions):
        block_cells = list(enumerate(cells))
        if document["order"] == "blocked": rng.shuffle(block_cells)
        for cell, coding in block_cells:
            schedule.append({"id": f"b{block:02d}-c{cell:02d}", "block": block,
                "levels": {factor["id"]: factor["levels"][level]["id"] for factor, level in zip(factors, coding)},
                "coding": [2 * level - 1 for level in coding],
                "arguments": [argument for factor, level in zip(factors, coding) for argument in factor["levels"][level]["arguments"]],
                "control": not any(coding)})
    if document["order"] == "randomized": rng.shuffle(schedule)
    if len(schedule) > recipe["limits"]["max_trials"]:
        raise ValueError("Complete factorial repetitions exceed the declared max_trials budget")
    # A time budget is a hard ceiling, not a promise all trials will finish.
    recipe["id"] = document["id"]
    recipe["cases"] = [{"id": item["id"], "arguments": item["arguments"]} for item in schedule]
    D.validate(recipe)
    return {"schema": "raven.factorial-plan/v1", "design": copy.deepcopy(document), "design_sha256": digest(document),
            "recipe": recipe, "recipe_sha256": digest(recipe), "schedule": schedule,
            "trial_count": len(schedule), "control_repetitions": repetitions,
            "qualification": "Experimental design; controls need application evidence. No execution occurs during planning."}


def _fit(matrix, values, names):
    n, width = len(matrix), len(names)
    if n <= width:
        return {"status": "underpowered", "n": n, "parameters": width, "effects": [], "reason": "No residual degrees of freedom"}
    columns = [[row[i] for row in matrix] for i in range(width)]
    q, r = [], [[0.] * width for _ in range(width)]
    for i, column in enumerate(columns):
        vector = list(column)
        # Two-pass modified Gram-Schmidt reduces cancellation for block columns.
        for _ in range(2):
            for j, basis in enumerate(q):
                amount = math.fsum(a * b for a, b in zip(basis, vector))
                r[j][i] += amount
                vector = [value - amount * a for value, a in zip(vector, basis)]
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm <= 1e-10:
            return {"status": "confounded", "n": n, "rank": len(q), "parameters": width, "effects": [], "reason": "Rank-deficient observed design; no attribution"}
        r[i][i] = norm; q.append([value / norm for value in vector])
    def solve(right):
        answer = [0.] * width
        for i in range(width - 1, -1, -1):
            answer[i] = (right[i] - math.fsum(r[i][j] * answer[j] for j in range(i + 1, width))) / r[i][i]
        return answer
    inverse_columns = [solve([float(i == j) for i in range(width)]) for j in range(width)]
    inverse = list(map(list, zip(*inverse_columns)))
    condition = max(sum(abs(v) for v in row) for row in r) * max(sum(abs(v) for v in row) for row in inverse)
    if not math.isfinite(condition) or condition > 1e8:
        return {"status": "ill-conditioned", "n": n, "rank": width, "condition_infinity_R": condition, "effects": [], "reason": "Unstable observed design"}
    coefficients = solve([math.fsum(a * b for a, b in zip(basis, values)) for basis in q])
    if not all(math.isfinite(value) for value in coefficients):
        raise ValueError("Effect estimation exceeds the supported numeric range")
    residuals = [value - math.fsum(a * b for a, b in zip(row, coefficients)) for row, value in zip(matrix, values)]
    sse = math.fsum(value * value for value in residuals)
    df = n - width
    variance = sse / df
    inference = variance > max(1e-24, max(abs(v) for v in values) ** 2 * 1e-24)
    tested = max(1, sum(not name.startswith("block:") and name != "intercept" for name in names))
    critical = S.t_critical(df, 1 - .05 / tested) if inference else None
    effects = []
    for i, name in enumerate(names):
        if name == "intercept" or name.startswith("block:"): continue
        # +/-1 coefficient x2 gives the conventional average high-low contrast;
        # interaction is the corresponding half difference-of-differences.
        error = math.sqrt(variance * math.fsum(value * value for value in inverse[i]))
        estimate = 2 * coefficients[i]
        interval = [estimate - 2 * critical * error, estimate + 2 * critical * error] if inference else None
        effects.append({"term": name, "contrast": estimate, "interval": interval})
    return {"status": "observed-associations" if inference else "zero-residual-variance", "n": n, "rank": width,
            "parameters": width, "degrees_of_freedom": df, "condition_infinity_R": condition,
            "residual_sum_squares": sse, "residual_variance": variance, "effects": effects,
            "uncertainty": "95% Bonferroni family Student-t intervals assume independent homoscedastic residuals" if inference else "Residual variance is zero/numerically negligible; inferential intervals unavailable"}


def analyze(document):
    D._object(document, {"schema", "design", "observations"})
    if document["schema"] != RESULT_SCHEMA: raise ValueError("Expected factorial-results/v1")
    plan = prepare(document["design"])
    observations = document["observations"]
    if not isinstance(observations, list) or len(observations) > 160:
        raise ValueError("At most 160 independently identified observations are supported")
    by_id = {item["id"]: item for item in plan["schedule"]}
    names = [factor["id"] for factor in document["design"]["factors"]]
    interaction_pairs = list(itertools.combinations(range(len(names)), 2))
    terms = ["intercept", *names, *[names[a] + ":" + names[b] for a, b in interaction_pairs]]
    blocks = list(range(1, document["design"]["repetitions"])) if document["design"]["order"] == "blocked" else []
    terms += [f"block:{block}" for block in blocks]
    matrix, values, checks, seen = [], [], [], set()
    reference_hashes = set()
    policy = document["design"]["equivalence"]
    tolerance = max(policy["absolute"], policy["relative"] * abs(policy["reference"]))
    if not math.isfinite(tolerance): raise ValueError("Equivalence tolerance overflow")
    for observation in observations:
        D._object(observation, {"id", "value", "unit", "output_sha256", "semantic", "controls"})
        key = D._name(observation["id"])
        if key not in by_id or key in seen: raise ValueError("Observation IDs must identify unique planned trials")
        seen.add(key)
        value = S.number(observation["value"], "measured output")
        if observation["unit"] != policy["unit"]: raise ValueError("Output unit differs from declared equivalence")
        output = observation["output_sha256"]
        if not isinstance(output, str) or len(output) != 64 or any(c not in "0123456789abcdef" for c in output):
            raise ValueError("Output identity requires an exact SHA-256")
        if not isinstance(observation["semantic"], str) or len(observation["semantic"]) > 256:
            raise ValueError("Output semantic identity must be bounded")
        controls = observation["controls"]
        if not isinstance(controls, dict) or not set(controls) <= set(names) or any(not isinstance(v, str) or len(v) > 128 for v in controls.values()):
            raise ValueError("Observed controls must name declared factors with bounded effective levels")
        trial = by_id[key]
        unsupported = [name for name in names if controls.get(name) != trial["levels"][name]]
        semantic = observation["semantic"] == policy["semantic"]
        checks.append({"id": key, "tolerance_pass": abs(value - policy["reference"]) <= tolerance,
                       "semantic_match": semantic, "unsupported_controls": unsupported, "output_sha256": output})
        if trial["control"] and semantic and not unsupported: reference_hashes.add(output)
        if semantic and not unsupported:
            coding = trial["coding"]
            matrix.append([1., *coding, *[coding[a] * coding[b] for a, b in interaction_pairs], *[float(trial["block"] == block) for block in blocks]])
            values.append(value)
    for check in checks:
        check["bitwise_change_from_controls"] = None if not reference_hashes else check["output_sha256"] not in reference_hashes
    fit = _fit(matrix, values, terms)
    return {"schema": "raven.factorial-analysis/v1", "status": "experimental", "state": fit["status"],
            "design_sha256": plan["design_sha256"], "fit": fit, "checks": checks,
            "missing_trials": [item["id"] for item in plan["schedule"] if item["id"] not in seen],
            "control_bitwise_variants": len(reference_hashes), "tolerance": tolerance, "unit": policy["unit"],
            "limit": "Associations within declared randomized trials; no causal percentages. Unexplained residuals include omitted interactions, noise and uncontrolled factors. Unsupported controls and semantic changes are excluded from effect estimation; tolerance failures remain reported outcomes."}


def collect(document, workspaces):
    """Read committed identities; only proven never-started cases may be resumed.

    Missing/inconsistent durable evidence is uncertainty, never replay permission.
    This reader modifies no workspace and executes nothing.
    """
    from pathlib import Path
    from . import evidence, science_files
    plan = prepare(document)
    if not isinstance(workspaces, list) or not 1 <= len(workspaces) <= 4:
        raise ValueError("Select one to four existing workspaces for this exact design")
    expected = {item["id"]: item for item in plan["recipe"]["cases"]}
    base = {key: value for key, value in plan["recipe"].items() if key != "cases"}
    observations, committed, uncertain, sources = [], set(), set(), []
    total, seen_intents = 0, set()
    recovery_ready = True

    def read(path):
        nonlocal total
        remaining = (8 << 20) - total
        if remaining <= 0:
            raise ValueError("Experiment receipt budget exceeds 8 MiB")
        record, raw = science_files.snapshot(str(path), limit=min(science_files.METADATA_LIMIT, remaining), keep=True)
        total += record["size"]
        sources.append(record)
        return evidence.loads(raw)

    for workspace in workspaces:
        root = Path(workspace).absolute()
        recipe = read(root / "recipe.json")
        D.validate(recipe)
        if {key: value for key, value in recipe.items() if key != "cases"} != base or any(expected.get(item["id"]) != item for item in recipe["cases"]):
            raise ValueError("Workspace recipe does not belong to this exact design")
        execution = read(root / "execution.json")
        executable = execution.get("executable", {}).get("path")
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            raise ValueError("Workspace lacks its reviewed executable identity")
        terminal = root / "result.json"
        declarations = {}
        terminal_loaded = terminal.exists()
        if not terminal_loaded:
            recovery_ready = False
        else:
            final = read(terminal)
            if (not isinstance(final, dict) or final.get("schema") != "raven.diagnostic-result/v1"
                    or final.get("recipe_sha256") != digest(recipe) or final.get("state") not in {"completed", "partial"}):
                raise ValueError("Terminal receipt does not identify this completed or partial recipe")
            declared = final.get("trials")
            if not isinstance(declared, list) or len(declared) > len(recipe["cases"]):
                raise ValueError("Invalid committed trial declaration inventory")
            for sequence, row in enumerate(declared):
                if (not isinstance(row, dict) or type(row.get("sequence")) is not int or row["sequence"] != sequence
                        or row.get("case_id") != recipe["cases"][sequence]["id"]):
                    raise ValueError("Terminal trial declaration differs from ordered recipe identity")
                declarations[sequence] = row
        process_path = root / "driver.process.json"
        if process_path.exists():
            from .ops_investigation import _process_matches
            process = read(process_path)
            D._object(process, {"pid", "start", "boot", "recipe_sha256"})
            if process["recipe_sha256"] != digest(recipe):
                raise ValueError("Driver process belongs to another recipe")
            if _process_matches(process): recovery_ready = False
        found = set()
        for sequence in range(recipe["limits"]["max_trials"]):
            directory = root / "trials" / f"{sequence:04d}"
            if not directory.exists(): continue
            intent = read(directory / "intent.json")
            key = intent.get("case_id")
            if (type(intent.get("sequence")) is not int or intent["sequence"] != sequence
                    or sequence >= len(recipe["cases"]) or key != recipe["cases"][sequence]["id"]):
                raise ValueError("Trial intent differs from exact ordered recipe identity")
            if key in seen_intents:
                raise ValueError("Duplicate trial intent across recovery workspaces")
            seen_intents.add(key)
            argv = []
            for item in recipe["command"] + recipe["cases"][sequence]["arguments"]:
                if item == "{result}": item = str(directory / "result.json")
                elif item.startswith("{file:") and item.endswith("}"): item = str(root / "sources" / item[6:-1])
                argv.append(item)
            argv[0] = executable
            if intent.get("arguments") != argv or intent.get("input_sha256") is not None:
                raise ValueError("Trial intent arguments differ from the reviewed factorial case")
            receipt_path = directory / "result.json.receipt"
            if not receipt_path.exists():
                uncertain.add(key); recovery_ready = False; continue
            receipt = read(receipt_path)
            if (type(receipt.get("sequence")) is not int or receipt["sequence"] != sequence
                    or receipt.get("case_id") != key or key in committed):
                raise ValueError("Committed trial identity is duplicate or inconsistent")
            if terminal_loaded and declarations.get(sequence) != receipt:
                uncertain.add(key); recovery_ready = False; continue
            found.add(sequence)
            committed.add(key)
            if receipt.get("state") == "finished" and receipt.get("oracle_matched") is True and isinstance(receipt.get("application"), dict):
                D._object(receipt["application"], {"value", "unit", "output_sha256", "semantic", "controls"})
                observations.append({**receipt["application"], "id": key})
        # Deleting an intent/receipt directory cannot make a declared trial new.
        for sequence in declarations.keys() - found:
            uncertain.add(declarations[sequence]["case_id"])
            recovery_ready = False
    results = {"schema": RESULT_SCHEMA, "design": copy.deepcopy(document), "observations": observations}
    analyze(results)
    remaining = [item for item in plan["recipe"]["cases"] if item["id"] not in committed and item["id"] not in uncertain]
    resume = {**copy.deepcopy(plan["recipe"]), "cases": remaining} if remaining and recovery_ready else None
    return {"schema": "raven.factorial-collection/v1", "results": results, "committed": sorted(committed),
            "uncertain": sorted(uncertain), "remaining": len(remaining), "recovery_recipe": resume, "sources": sources,
            "recovery": "Review remaining cases in a new private workspace; committed trials are excluded" if resume else
                        "No remaining trials" if not remaining and not uncertain else
                        "Recovery paused: confirm the original driver ended and reconcile every uncertain intent; no trial is automatically repeated"}
