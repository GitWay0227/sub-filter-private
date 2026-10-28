#!/usr/bin/env python3
"""Generator and assembler for the CN-vantage scan leg (companion to cn-scan.sh).

Node names from public sources can be numeric or contain "@", so every name
goes through str(), and copy names that collide with existing pool names get
a "#N" suffix via uniq().

Modes:
  cands POOL OUT CAP RE                    CN-name candidates (unique, capped)
  fers  POOL OUT EXCL                      first 3 alive nodes (relay entries)
  config direct CFG JOBS ALIVE CANDS DEFS  one listener per candidate
  config relay  CFG JOBS ALIVE CANDS FERS DEFS   C@F copies (dialer-proxy: F), listener per copy
  config chain  CFG JOBS ALIVE DEFS VPS NAMES OFFSET   T@V copies (dialer-proxy: viewpoint), listener per copy
  config geo    CFG JOBS ALIVE             one listener per alive node (exit-IP probe)
  vpset VPS CNSET RESA RESAR MIND          viewpoints + CN set from probe results
  geoemit OUT ALIVE RES TMO                geo.yaml: exit-country groups + stats
  emit  OUT ALIVE DEFS VPS CNSET RES...    assemble cn-reachable.yaml

CN candidates and viewpoints may be missing from the alive pool (US-vantage
delay testing kills most CN-named nodes), so DEFS (the pre-liveness-test full
pool, scan.sh's work.json) supplies their definitions: proxies written to the
mihomo config = alive nodes + only the needed DEFS entries (full DEFS would
bloat the config and duplicates would make mihomo reject it).

Jobs TSV columns:      port, probe_name, report_name, vp
Probe result columns:  report_name, country, exit_ip, vp
Env: SECRET (mihomo API secret), BASE_PORT (default 20001)
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

import yaml

BASE_PORT = int(os.environ.get("BASE_PORT", "20001"))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.write("\n")


def parse_tsv(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                # 3 cols (name, country, ip) from geo probes; 4 cols (with vp)
                # from viewpoint probes. Pad to 4 for uniform unpacking.
                parts += [""] * (4 - len(parts))
                rows.append(parts[:4])
    return rows


def uniq(name, used):
    if name not in used:
        return name
    n = 2
    while "%s#%d" % (name, n) in used:
        n += 1
    return "%s#%d" % (name, n)


def by_name(pool):
    return {str(n.get("name", "")): n for n in pool if n.get("name")}


def with_defs(alive, defs_path, needed):
    """proxies = alive nodes + only the DEFS entries in `needed` that are not
    already in alive; lookup map covers both. Avoids full-pool configs and
    duplicate proxy names."""
    pmap = by_name(alive)
    proxies = list(alive)
    if defs_path:
        dmap = by_name(load(defs_path))
        for n in needed:
            n = str(n)
            if n and n not in pmap and n in dmap:
                proxies.append(dmap[n])
                pmap[n] = dmap[n]
    return proxies, pmap


def write_config_and_jobs(proxies, listeners, jobs, cfg_path, jobs_path):
    cfg = {
        "mixed-port": 27890,
        "external-controller": "127.0.0.1:29090",
        "secret": os.environ.get("SECRET", ""),
        "log-level": "error",
        "keep-alive-interval": 30,
        "find-process-mode": "off",
        "proxies": proxies,
        "listeners": listeners,
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, width=10000)
    with open(jobs_path, "w", encoding="utf-8") as f:
        for port, pn, rn, vp in jobs:
            f.write("%s\t%s\t%s\t%s\n" % (port, pn, rn, vp))


def listener(i, proxy):
    return {"name": "ln%d" % i, "type": "mixed", "port": BASE_PORT + i,
            "listen": "127.0.0.1", "proxy": proxy}


def mode_cands(args):
    pool_path, out_path, cap, re_s = args
    rx = re.compile(re_s, re.I)
    seen, out = set(), []
    for n in load(pool_path):
        name = str(n.get("name", ""))
        if name and name not in seen and rx.search(name):
            seen.add(name)
            out.append(name)
            if len(out) >= int(cap):
                break
    save(out_path, out)


def mode_fers(args):
    pool_path, out_path, excl_path = args
    excl = set(load(excl_path)) if excl_path != "-" else set()
    out = [str(n["name"]) for n in load(pool_path)
           if n.get("name") and str(n["name"]) not in excl][:3]
    save(out_path, out)


def mode_config_direct(args):
    cfg_path, jobs_path, alive_path, cands_path, defs_path = args
    alive = load(alive_path)
    cands = load(cands_path)
    proxies, pmap = with_defs(alive, defs_path, cands)
    listeners, jobs = [], []
    for i, name in enumerate(cands):
        if name not in pmap:
            continue
        listeners.append(listener(i, name))
        jobs.append((BASE_PORT + i, name, name, ""))
    write_config_and_jobs(proxies, listeners, jobs, cfg_path, jobs_path)


def mode_config_relay(args):
    cfg_path, jobs_path, alive_path, cands_path, fers_path, defs_path = args
    alive = load(alive_path)
    cands = load(cands_path)
    fers = load(fers_path)
    proxies, pmap = with_defs(alive, defs_path, cands)
    used = set(pmap)
    listeners, jobs = [], []
    li = 0
    for c in cands:
        node = pmap.get(c)
        if not node:
            continue
        for f in fers:
            copy = dict(node)
            cname = uniq("%s@%s" % (c, f), used)
            copy["name"] = cname
            copy["dialer-proxy"] = f
            used.add(cname)
            proxies.append(copy)
            listeners.append(listener(li, cname))
            jobs.append((BASE_PORT + li, cname, c, f))
            li += 1
    write_config_and_jobs(proxies, listeners, jobs, cfg_path, jobs_path)


def mode_config_chain(args):
    cfg_path, jobs_path, alive_path, defs_path, vps_path, names_path, offset = args
    offset = int(offset)
    alive = load(alive_path)
    vps = load(vps_path)
    names = load(names_path)
    k = len(vps)
    proxies, pmap = with_defs(alive, defs_path, [v["name"] for v in vps])
    used = set(pmap)
    # Direct viewpoints dial by their own name; relay-entry viewpoints need a
    # V@F copy (dialer-proxy: F) so phase-B chains through them stay connected.
    vp_dial = {}
    for v in vps:
        vn = v["name"]
        if v.get("entry") == "relay" and v.get("via"):
            node = pmap.get(vn)
            if not node:
                continue
            cname = uniq("%s@%s" % (vn, v["via"]), used)
            copy = dict(node)
            copy["name"] = cname
            copy["dialer-proxy"] = v["via"]
            used.add(cname)
            proxies.append(copy)
            vp_dial[vn] = cname
        else:
            vp_dial[vn] = vn
    listeners, jobs = [], []
    li = 0
    for i, t in enumerate(names):
        v = vps[(i + offset) % k]
        if str(t) == v["name"] or str(t) not in pmap:
            continue  # never test a viewpoint through itself
        node = pmap[str(t)]
        copy = dict(node)
        cname = uniq("%s@%s" % (str(t), v["name"]), used)
        copy["name"] = cname
        copy["dialer-proxy"] = vp_dial[v["name"]]
        used.add(cname)
        proxies.append(copy)
        listeners.append(listener(li, cname))
        jobs.append((BASE_PORT + li, cname, str(t), v["name"]))
        li += 1
    write_config_and_jobs(proxies, listeners, jobs, cfg_path, jobs_path)


def mode_config_geo(args):
    cfg_path, jobs_path, alive_path = args
    alive = load(alive_path)
    listeners, jobs = [], []
    for i, n in enumerate(alive):
        name = str(n.get("name", ""))
        if not name:
            continue
        listeners.append(listener(i, name))
        jobs.append((BASE_PORT + i, name, name, ""))
    write_config_and_jobs(alive, listeners, jobs, cfg_path, jobs_path)


def mode_geoemit(args):
    out_path, alive_path, res_path, tmo = args[0], args[1], args[2], int(args[3])
    alive = load(alive_path)
    order = {str(n.get("name", "")): i for i, n in enumerate(alive)}

    results = {}
    for name, cc, ip, _vp in parse_tsv(res_path):
        results[name] = {"cc": cc, "ip": ip}

    groups = defaultdict(list)
    meta = {}
    for n in alive:
        name = str(n.get("name", ""))
        if not name:
            continue
        r = results.get(name)
        if not r:
            meta[name] = {"country": "", "exit_ip": ""}
            continue
        meta[name] = {"country": r["cc"], "exit_ip": r["ip"]}
        if r["cc"]:
            groups[r["cc"]].append(name)
    ordered_groups = {}
    for c in sorted(groups, key=lambda c: (-len(groups[c]), c)):
        ordered_groups[c] = sorted(groups[c], key=lambda n: order.get(n, 1 << 30))

    out = {
        "stats": {
            "alive": len(alive),
            "geo_probed": sum(1 for m in meta.values() if m["country"]),
            "unresolved": sum(1 for m in meta.values() if not m["country"]),
            "probe_timeout_s": tmo,
            "by_country": {c: len(v) for c, v in ordered_groups.items()},
        },
        "groups": ordered_groups,
        "meta": {n: meta[n] for n in sorted(meta, key=lambda n: order.get(n, 1 << 30))},
    }
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    header = (
        "# geo-exit - generated by GitHub Actions, do not edit\n"
        "# %s UTC | alive %d | geo-probed %d | countries %d\n"
        "# scope: per-node measured exit IP (ip-api/ip.sb via mihomo listener); "
        "groups keyed by measured exit-IP country; unresolved nodes omitted from groups\n"
        % (now, len(alive), out["stats"]["geo_probed"], len(ordered_groups))
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False, width=10000)


def mode_vpset(args):
    vps_path, cnset_path, resa_path, resar_path, mind = args
    mind = int(mind)
    vps, cnset = [], []
    for name, cc, ip, _vp in parse_tsv(resa_path):
        if cc == "CN":
            vps.append({"name": name, "entry": "direct", "via": "", "exit_ip": ip})
            cnset.append({"name": name, "entry": "direct", "via": "", "exit_ip": ip})
    ndirect = len(vps)
    for name, cc, ip, fer in parse_tsv(resar_path):
        if cc == "CN":
            cnset.append({"name": name, "entry": "relay", "via": fer, "exit_ip": ip})
            if ndirect < mind:
                vps.append({"name": name, "entry": "relay", "via": fer, "exit_ip": ip})
    save(vps_path, vps)
    save(cnset_path, cnset)


def mode_emit(args):
    out_path, alive_path, defs_path, vps_path, cnset_path = args[0:5]
    res_paths = args[5:]
    alive = load(alive_path)
    vps = load(vps_path)
    cnset = load(cnset_path)

    results = {}
    for p in res_paths:
        for name, cc, ip, vp in parse_tsv(p):
            results[name] = {"cc": cc, "ip": ip, "vp": vp}

    cn_names = {e["name"] for e in cnset}
    meta = {}
    for e in cnset:
        r = results.get(e["name"])
        meta[e["name"]] = {
            "country": "CN",
            "exit_ip": e["exit_ip"],
            "via_viewpoint": r["vp"] if (r and r.get("vp")) else "",
            "entry": e["entry"],
        }
    for name, r in results.items():
        if name in cn_names:
            continue
        meta[name] = {
            "country": r["cc"],
            "exit_ip": r["ip"],
            "via_viewpoint": r["vp"],
            "entry": "direct",
        }

    # CN-set members (viewpoints) usually live only in DEFS, not in the alive pool.
    proxies, pmap = with_defs(alive, defs_path, list(meta))
    order = {name: i for i, name in enumerate(pmap)}

    groups = defaultdict(list)
    for name, m in meta.items():
        groups[m["country"] or "??"].append(name)
    ordered_groups = {}
    for c in sorted(groups, key=lambda c: (-len(groups[c]), c)):
        ordered_groups[c] = sorted(groups[c], key=lambda n: order.get(n, 1 << 30))

    out = {
        "viewpoints": [
            dict({"name": v["name"], "entry": v["entry"]},
                 **({"via": v["via"]} if v.get("via") else {}),
                 exit_ip=v["exit_ip"])
            for v in vps
        ],
        "stats": {
            "alive": len(alive),
            "reachable": len(meta),
            "by_country": {c: len(v) for c, v in ordered_groups.items()},
        },
        "groups": ordered_groups,
        "proxies": [pmap[n] for n in pmap if n in meta],
        "meta": {n: meta[n] for n in sorted(meta, key=lambda n: order.get(n, 1 << 30))},
    }
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    header = (
        "# cn-reachable - generated by GitHub Actions, do not edit\n"
        "# %s UTC | viewpoints %d | alive %d | china-reachable %d\n"
        "# scope: vantage=pool-verified CN-exit nodes (dialer-proxy chains); "
        "groups keyed by measured exit-IP GeoIP; failures retried once on another viewpoint\n"
        % (now, len(vps), len(alive), len(meta))
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False, width=10000)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "cands":
        mode_cands(args)
    elif mode == "fers":
        mode_fers(args)
    elif mode == "config" and args and args[0] == "direct":
        mode_config_direct(args[1:])
    elif mode == "config" and args and args[0] == "relay":
        mode_config_relay(args[1:])
    elif mode == "config" and args and args[0] == "chain":
        mode_config_chain(args[1:])
    elif mode == "config" and args and args[0] == "geo":
        mode_config_geo(args[1:])
    elif mode == "geoemit":
        mode_geoemit(args)
    elif mode == "vpset":
        mode_vpset(args)
    elif mode == "emit":
        mode_emit(args)
    else:
        print("cn-gen: unknown mode: %s" % mode, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
