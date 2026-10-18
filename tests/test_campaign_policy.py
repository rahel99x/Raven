"""Shadow invariants and durable scoped controls; no live scheduler."""
import copy
import json
from types import SimpleNamespace
import pytest
from raven import campaign_policy as P, orchestrator as X, operations as O

@pytest.fixture
def source(tmp_path):
    nodes=[]
    for ident,cpus in (("large",2),("small",1),("follow",1)):
        script=tmp_path/(ident+".sh"); script.write_text("#!/bin/bash\ntrue\n")
        nodes.append(dict(id=ident,script=str(script),resources=dict(nodes=1,ntasks=1,cpus_per_task=cpus,mem="100M",time="00:01:00",account="lab",qos="normal"),**({"depends_on":["small"]} if ident=="follow" else {})))
    scope=dict(schema="raven.execution-scope/v1",backend="local",connection_host="host",configured_host="",profile="",user="raven",uid=1100,cluster="fixture")
    review=X.prepare_review("workflow",{"version":1,"kind":"raven.workflow","nodes":nodes},workdir=tmp_path,scope=scope)
    model=dict(schema="raven.queue-model/v1",id="campaign-fixture",nodes=[dict(id="n",cpus=2,memory_mb=1000,gpus=0)],policy=dict(backfill=True,fairshare_weight=0,usage_half_life_seconds=86400,accounts={"lab":dict(weight=1,usage_cpu_seconds=100)},qos={"normal":dict(max_running=10,max_cpus=10,max_wall_seconds=1000)}),jobs=[dict(id=n["id"],submit=0,duration=d,wall_limit=60,nodes=1,cpus_per_node=c,memory_mb_per_node=100,gpus_per_node=0,priority=1,account="lab",qos="normal",depends=n.get("depends_on",[])) for n,c,d in zip(nodes,(2,1,1),(10,2,3))])
    return dict(schema="raven.campaign-policy/v1",review=review,queue_model=model,limits=dict(max_running=2,max_submit_jobs=2,grp_cpus=2,grp_memory_mb=1000,grp_gpus=0,commands_per_minute=60,external_jobs=0,external_cpus=0,external_memory_mb=0,external_gpus=0,complete=True,fairshare_delay_seconds=5))


def test_hand_computable_admission_dependencies_resources_and_tradeoffs(source):
    result=P.analyze(source); policies={r["policy"]:r for r in result["comparisons"]}
    assert set(policies)==set(P.POLICIES)
    assert policies["fifo"]["metrics"]["makespan_seconds"]==15
    assert policies["shortest"]["decisions"][0]["id"]=="small"
    assert policies["fairshare"]["deltas_vs_fifo"]["makespan_seconds"]>0
    jobs={j["id"]:j for j in source["queue_model"]["jobs"]}
    for policy in policies.values():
        timeline={r["id"]:r for r in policy["timeline"]}
        for decision in policy["decisions"]:
            active=[jobs[key] for key in decision["active_before"]]
            assert len(active)<2
            assert sum(P._units(j)[0] for j in active)+P._units(jobs[decision["id"]])[0]<=2
            assert all(timeline[dep]["end"]<=decision["admit_at"] for dep in jobs[decision["id"]]["depends"])
        assert len(policy["forecast_error_sensitivity"])==2
    assert result["eligible_nodes"]==["large"] and not result["production_qualified"]


def test_external_limits_failed_dependencies_and_unknown_policy(source):
    source["limits"]["external_cpus"]=1
    assert all("large" in p["blocked"] for p in P.analyze(source)["comparisons"])
    source["limits"]["external_cpus"]=0; source["queue_model"]["jobs"][1]["success"]=False
    assert all("follow" in p["blocked"] for p in P.analyze(source)["comparisons"])
    source["limits"]["complete"]=False; result=P.analyze(source)
    assert result["status"]=="blocked" and result["eligible_nodes"]==[]


def test_command_spacing_and_runtime_overrun_cost(source):
    source["limits"]["commands_per_minute"]=1
    for policy in P.analyze(source)["comparisons"]:
        times=[r["admit_at"] for r in policy["decisions"]]
        assert all(b-a>=60 for a,b in zip(times,times[1:]))
    source["queue_model"]["jobs"][1]["duration"]=40
    assert any(s["modeled_wasted_cpu_seconds"]>0 for p in P.analyze(source)["comparisons"] for s in p["forecast_error_sensitivity"])


def test_uncertain_existing_execution_cannot_become_new_work(source,tmp_path):
    class Lost:
        b = SimpleNamespace()
        def submit(self,argv,workdir): raise OSError("lost acknowledgement")
    receipt=X.execute(source["review"],Lost(),tmp_path/"execution",confirmed=True)
    assert receipt["nodes"][0]["state"]=="unknown"
    receipt.pop("receipt_path"); source["receipt"]=receipt; result=P.analyze(source)
    assert result["uncertain_nodes"]==["large"] and result["eligible_nodes"]==[]
    assert result["receipt_digest"] and P.analyze(source)==result


@pytest.mark.parametrize("damage",["identity","resources","dependency","review","scope","policy"])
def test_mismatched_action_semantics_fail_closed(source,damage):
    if damage=="identity": source["queue_model"]["jobs"][0]["id"]="other"
    elif damage=="resources": source["queue_model"]["jobs"][0]["cpus_per_node"]=1
    elif damage=="dependency": source["queue_model"]["jobs"][2]["depends"]=[]
    elif damage=="review": source["review"]["nodes"][0]["plan"]["command"]="changed"
    elif damage=="scope": source["review"].pop("scope")
    else: source["policy"]="evade-site-limits"
    with pytest.raises(ValueError): P.analyze(source)


def test_scoped_controls_persist_and_reject_stale_reviews_without_commands(source,tmp_path):
    ctx=O.Context(scope=source["review"]["scope"],state_dir=str(tmp_path/"state"),slurm=SimpleNamespace(b=SimpleNamespace(run=lambda *a:pytest.fail("must not call scheduler"))))
    path=tmp_path/"policy.json"; path.write_text(json.dumps(source)); params=dict(path=str(path),action="compare"); original=copy.deepcopy(source)
    assert O.run("campaign-policy",params,ctx)["data"]["eligible_nodes"]==["large"]
    first=O.run("campaign-policy",dict(params,action="pause"),ctx); stale=O.run("campaign-policy",dict(params,action="override",policy="shortest"),ctx)
    O.apply("campaign-policy",first["plan"],ctx)
    assert O.run("campaign-policy",params,ctx)["data"]["eligible_nodes"]==[]
    with pytest.raises(ValueError,match="changed after review"): O.apply("campaign-policy",stale["plan"],ctx)
    for action,extra in (("resume",{}),("override",{"policy":"shortest"})):
        review=O.run("campaign-policy",dict(params,action=action,**extra),ctx); O.apply("campaign-policy",review["plan"],ctx)
    compared=O.run("campaign-policy",params,ctx)
    assert compared["data"]["selected_policy"]=="shortest" and compared["data"]["eligible_nodes"]==["small"]
    cancelled=O.run("campaign-policy",dict(params,action="cancel"),ctx); O.apply("campaign-policy",cancelled["plan"],ctx)
    assert O.run("campaign-policy",params,ctx)["data"]["status"]=="cancelled"
    with pytest.raises(ValueError,match="cancelled"): O.run("campaign-policy",dict(params,action="resume"),ctx)
    assert json.loads(path.read_text())==original
    with pytest.raises(ValueError,match="different connection"):
        O.run("campaign-policy",params,O.Context(scope={**ctx.scope,"cluster":"other"},state_dir=ctx.state_dir))


def test_managed_comparison_uses_worker_and_controls_stay_on_interface(source,tmp_path):
    from raven import raven_execution as R
    path=tmp_path/"policy.json"; path.write_text(json.dumps(source)); params=dict(path=str(path),action="compare")
    ctx=O.Context(scope=source["review"]["scope"],state_dir=str(tmp_path/"state"),cfg={"raven":{"managed":True,"mode":"light","site":"desktop"}})
    assert O.run("campaign-policy",params,ctx)["status"]=="deferred"
    worker=R.execute_operation("campaign-policy",{"params":params},{"scope":ctx.scope},verified_execution={"target":"desktop"})
    assert worker["result"]["data"]["model"]==P.MODEL
    assert O.run("campaign-policy",dict(params,action="pause"),ctx)["plan"]


def test_combined_comparison_budget_cancellation_and_immutable_inputs(source, monkeypatch):
    import threading
    original = copy.deepcopy(source)
    assert P.analyze(source) == P.analyze(source)
    assert source == original
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ValueError, match="cancelled"):
        P.analyze(source, cancel=cancelled)
    monkeypatch.setattr(P, "MAX_WORK", 1)
    with pytest.raises(ValueError, match="budget"):
        P.analyze(source)


def test_export_uses_exact_review_and_refuses_overwrite(source, tmp_path):
    model, limits, target = (tmp_path / name for name in ("queue.json", "limits.json", "policy.json"))
    model.write_text(json.dumps(source["queue_model"]))
    limits.write_text(json.dumps(source["limits"]))
    assert P.export_manifest(source["review"], model, limits, target) == str(target)
    assert json.loads(target.read_text()) == source
    with pytest.raises(FileExistsError):
        P.export_manifest(source["review"], model, limits, target)
    assert json.loads(target.read_text())["review"]["review_id"] == source["review"]["review_id"]


def test_compute_task_cannot_prepare_local_campaign_controls(source, tmp_path):
    from raven import raven_execution as R
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="captured interface"):
        R.execute_operation("campaign-policy", {"params": {"path": str(path), "action": "pause"}},
                            {"scope": source["review"]["scope"]}, verified_execution={"target": "desktop"})


def test_controls_require_private_state_and_local_write_semantics(source, tmp_path, monkeypatch):
    from raven import ops_campaign_policy as policy, state_ownership
    ctx = O.Context(scope=source["review"]["scope"], state_dir=str(tmp_path / "state"))
    document = tmp_path / "policy.json"
    document.write_text(json.dumps(source))
    params = dict(path=str(document), action="pause")
    review = O.run("campaign-policy", params, ctx)
    monkeypatch.setattr(state_ownership, "filesystem_kind", lambda path: "nfs")
    with pytest.raises(ValueError, match="read-only on nfs"):
        O.apply("campaign-policy", review["plan"], ctx)
    path = policy._state_path(ctx, source["review"]["review_id"])
    assert not path.exists()
    monkeypatch.setattr(state_ownership, "filesystem_kind", lambda path: "tmpfs")
    review = O.run("campaign-policy", params, ctx)
    O.apply("campaign-policy", review["plan"], ctx)
    monkeypatch.setattr(state_ownership, "filesystem_kind", lambda path: "nfs")
    assert O.run("campaign-policy", dict(params, action="compare"), ctx)["data"]["status"] == "paused"
    path.chmod(0o666)
    with pytest.raises(ValueError, match="0600"):
        O.run("campaign-policy", dict(params, action="compare"), ctx)


def test_control_state_rejects_symlinked_ancestors(source, tmp_path):
    real = tmp_path / "actual"
    real.mkdir(mode=0o700)
    linked = tmp_path / "state"
    linked.symlink_to(real, target_is_directory=True)
    ctx = O.Context(scope=source["review"]["scope"], state_dir=str(linked))
    document = tmp_path / "policy.json"
    document.write_text(json.dumps(source))
    with pytest.raises((ValueError, OSError)):
        O.run("campaign-policy", dict(path=str(document), action="pause"), ctx)
    assert list(real.iterdir()) == []
