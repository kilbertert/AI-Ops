#!/usr/bin/env python3
"""活口检查：客户端个性化横幅所依赖的三条公司端点是否真的成立（#590）。

背景：PRD #578 把「我的车」个性化整个交给**前端直连公司现成端点**——
`/chMyCar/myCarList`（我的车）、`/chCarSeries/page`（车型目录 → 车图）、
`/chOrderInfo/getOrderUserPage`（最近一笔已完成充电单）。这条链路此前
**只在源码与 APK bundle 里读到过**，从未被实测。本脚本把它跑一遍，
并把「车型名 → 目录名」的命中率与生产库实测值对账。

它回答的问题只有一个，而且必须能被别人复跑：**前端直连成立，还是需要改道？**

跑在哪
------
**网关主机（41）**。理由：受试身份来自生产库 `ch_my_car`，而该库在
内网 `192.168.1.45`，只有 41 可达；凭据也只在 41 的 env 里（`AIOPS_MYSQL_*`，
文件 0600，root 都读不到）。端点侧走**公网域名**，这样测的是公司对外
真正暴露的那条路径，不是内网绕过。

零写入
------
只发 GET，且只发**读端点**。写端点（`/add`、`/edit`、`DELETE /{id}`、
`/switchEnableStatus`）本脚本**一律不碰** —— 它们在源码里同样没有鉴权注解，
存在与否是公司侧要单独核的事，不是本脚本顺手去试的事。

凭据纪律
--------
受试身份是**真实用户的凭据**（`user-id` 即等同该用户）。因此：
不打印车牌、VIN、订单号、用户名、完整 id；引用身份只用尾号；
输出里的车型名是目录公开数据，可以原样引用。

退出码
------
0 = 全部通过；1 = 有检查失败；2 = 未取证（默认的 `--plan` 什么都不发）。
把「只列了计划」读成「验证通过」正是这个脚本要防的那种假陈述。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.mall.qushiyun.com/charging-pile"
BASELINE_EXACT_RATE = 0.9706  # 生产库实测：1089/1122，见 docs/validation.md
RATE_TOLERANCE = 0.01  # 端点与库的差异容差（分页/大小写/裁剪规则不同会带来小幅偏移）


# --------------------------------------------------------------------------
# 纯逻辑：判定与分桶。这层不碰网络，`--self-check` 直接驱动它。
# --------------------------------------------------------------------------


def classify_hit(model: str, series_rows: list[dict]) -> str:
    """把一个车型名分到 exact / fallback / miss / blank 四桶之一。

    exact    —— 目录里有名字逐字相等的一行 ⇒ 拿得到车系图；
    fallback —— 目录返回了候选但没有逐字相等（模糊匹配命中）⇒ 只能退到品牌图；
    miss     —— 目录一行都没有 ⇒ 无图，只能退兜底图；
    blank    —— **车型名本身是空白**（`model` 非 NULL 但只有空格）⇒ 无从查起。

    `blank` 必须与 `miss` 分开：它是**数据质量**问题，不是目录覆盖问题，
    而且它占分母的量级足以把命中率算错（见 `summarize`）。
    """
    want = (model or "").strip().lower()
    if not want:
        return "blank"
    names = {(row.get("name") or "").strip().lower() for row in series_rows}
    if want in names:
        return "exact"
    return "fallback" if names else "miss"


def summarize(counts: dict[str, int]) -> dict[str, float]:
    """按**车数**（不是车型数）算占比——用户看到的是车，不是车型。

    **分母是与基线一致的「品牌与车型都非空」的行数**，空白车型单独计数、
    不进分母。这一条不是为了让数字好看：把空白行算进分母会把命中率
    压到 88%，而那 12 个百分点的差额来自一批 `model=' '` 的脏数据，
    与「目录能不能查到这台车」是两件事。空白行仍然被打印出来。
    """
    blank = counts.get("blank", 0)
    total = sum(v for k, v in counts.items() if k != "blank")
    shares = {k: (v / total if total else 0.0) for k, v in counts.items() if k != "blank"}
    return shares | {"total": total, "blank": blank}


def verdict(result: dict) -> tuple[bool, list[str]]:
    """把三次检查折叠成一句可据以行动的结论。"""
    notes: list[str] = []
    ok = True
    if not result["mycar"]["identified"]:
        ok = False
        notes.append("myCarList 没有把车逐条命名地取到 —— 个性化车图在源头就断了")
    if not result["series"]["client_reachable"]:
        ok = False
        notes.append("/chCarSeries/page 在客户端身份下不可达")
    if not result["orders"]["ok"]:
        ok = False
        notes.append("getOrderUserPage 没返回可用的已完成单")
    rate = result["series"]["exact_rate"]
    if abs(rate - BASELINE_EXACT_RATE) > RATE_TOLERANCE:
        ok = False
        notes.append(
            f"车型命中率 {rate:.2%} 与生产库实测 {BASELINE_EXACT_RATE:.2%} "
            f"偏差超过 {RATE_TOLERANCE:.0%}，需要解释差异而不能只报数"
        )
    if ok:
        notes.append("前端直连成立：三条端点均可由客户端身份使用，个性化车图链路完整")
    return ok, notes


# --------------------------------------------------------------------------
# 网络与库：只在 `--run` 下被调用
# --------------------------------------------------------------------------


def _get(path: str, headers: dict[str, str]) -> tuple[int, dict | None]:
    url = f"{BASE}{path}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # noqa: BLE001 - 传给判定层，不在这里决定成败
        return 0, None


_MYSQL_ENV = (
    "AIOPS_MYSQL_HOST",
    "AIOPS_MYSQL_PORT",
    "AIOPS_MYSQL_USER",
    "AIOPS_MYSQL_PASSWORD",
    "AIOPS_MYSQL_DATABASE",
)


def _connect():
    """连生产库。缺任何一个 env 键就直接报清楚缺哪个，而不是抛 KeyError。"""
    missing = [k for k in _MYSQL_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"缺少环境变量：{', '.join(missing)} —— 用网关服务的 env 运行本脚本")

    import pymysql  # 开发/验证期依赖，不进服务运行时

    return pymysql.connect(
        host=os.environ["AIOPS_MYSQL_HOST"],
        port=int(os.environ["AIOPS_MYSQL_PORT"]),
        user=os.environ["AIOPS_MYSQL_USER"],
        password=os.environ["AIOPS_MYSQL_PASSWORD"],
        database=os.environ["AIOPS_MYSQL_DATABASE"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _subjects(limit: int) -> list[tuple[str, str]]:
    """取几个「有实名车型的个人车」用户作为受试身份。

    这才是真实可解析的样本：`brand`/`model` 是自由文本，企业车（type=1）
    与未填车型的行都不代表横幅要画的那台车。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """select tenant_id, user_id from ch_my_car
               where type=0 and brand is not null and model is not null
                 and tenant_id is not null
               group by tenant_id, user_id having count(*) >= 3
               order by count(*) desc limit %s""",
            (limit,),
        )
        return [(str(r["tenant_id"]), str(r["user_id"])) for r in cur.fetchall()]


def _all_personal_cars() -> list[tuple[str, str]]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """select trim(brand) b, trim(model) m from ch_my_car
               where type=0 and brand is not null and model is not null"""
        )
        return [(r["b"], r["m"]) for r in cur.fetchall()]


def run(subject_limit: int) -> int:
    subjects = _subjects(subject_limit)
    if not subjects:
        print("✗ 库里取不到受试身份，无法继续")
        return 1

    # ① 我的车 —— 并做对照：证明这个身份确实由 user-id 头承载
    identified = 0
    orders_ok = 0
    for tenant, user in subjects:
        st, body = _get("/chMyCar/myCarList", {"tenant-id": tenant, "user-id": user})
        rows = (body or {}).get("data") or []
        named = [r for r in rows if (r.get("brand") or "").strip() and (r.get("model") or "").strip()]
        print(f"  myCarList user=..{user[-6:]} http={st} cars={len(rows)} 可逐条命名={len(named)}")
        for r in named[:3]:
            print(f"      brand={r['brand']!r} model={r['model']!r} isDef={r.get('isDef')}")
        identified += bool(named)

        st2, b2 = _get(
            "/chOrderInfo/getOrderUserPage?page=1&size=1&status=1&excludeHomeOrder=1",
            {"tenant-id": tenant, "user-id": user},
        )
        data = (b2 or {}).get("data") or {}
        recs = data.get("records") or []
        print(
            f"  getOrderUserPage user=..{user[-6:]} http={st2} total={data.get('total')} "
            f"有单号={bool(recs and recs[0].get('orderNo'))}"
        )
        orders_ok += bool(recs)

    # 对照：把 user-id 换成一个不存在的值，同一身份必须归零
    t0, u0 = subjects[0]
    _, bogus = _get("/chMyCar/myCarList", {"tenant-id": t0, "user-id": "9999999999999999"})
    bogus_rows = len((bogus or {}).get("data") or [])
    print(f"  对照（伪造 user-id）：cars={bogus_rows}（期望 0）")

    # ② 车型目录 —— 在**客户端身份**下可达（带 user-id/tenant-id，不带任何服务令牌）
    st3, body3 = _get("/chCarSeries/page?page=1&size=1", {"tenant-id": t0, "user-id": u0})
    sample = ((body3 or {}).get("data") or {}).get("records") or []
    has_logo = bool(sample and (sample[0].get("logo") or "").strip())
    print(f"  chCarSeries/page（客户端头）http={st3} 带 logo={has_logo}")

    # ③ 命中率 —— 逐车型走一遍端点，与生产库实测对账
    series_cache: dict[str, list[dict]] = {}
    counts = {"exact": 0, "fallback": 0, "miss": 0, "blank": 0}
    misses: list[tuple[str, str]] = []
    blanks = 0
    for _, model in _all_personal_cars():
        if model not in series_cache:
            q = urllib.parse.urlencode({"name": model, "page": 1, "size": 20})
            _, sb = _get(f"/chCarSeries/page?{q}", {"tenant-id": t0, "user-id": u0})
            series_cache[model] = ((sb or {}).get("data") or {}).get("records") or []
        bucket = classify_hit(model, series_cache[model])
        counts[bucket] += 1
        if bucket == "blank":
            blanks += 1
        if bucket == "miss" and len(misses) < 8:
            misses.append(model)

    shares = summarize(counts)
    print(
        f"  车型命中（按车数，分母=品牌与车型都非空）：exact={counts['exact']} "
        f"fallback={counts['fallback']} miss={counts['miss']}  合计={shares['total']}"
    )
    print(f"  exact 占比={shares['exact']:.2%}  生产库基线={BASELINE_EXACT_RATE:.2%}")
    print(f"  另有 {blanks} 行车 `model` 只有空白 —— 数据质量，不计入命中率分母")
    if misses:
        print(f"  无车系可取的车型样例：{misses}")

    result = {
        "mycar": {"identified": identified > 0, "subjects": len(subjects), "with_cars": identified},
        "orders": {"ok": orders_ok > 0, "subjects": len(subjects), "with_orders": orders_ok},
        "series": {"client_reachable": st3 == 200 and has_logo, "exact_rate": shares["exact"]},
        "control": {"bogus_user_id_rows": bogus_rows},
    }
    ok, notes = verdict(result)
    print("\n结论：")
    for n in notes:
        print(f"  {'✓' if ok else '✗'} {n}")
    if bogus_rows:
        print("  ✗ 对照未归零：伪造 user-id 也拿到了车，说明这条路不靠该头，需重判身份来源")
        ok = False
    print(f"\n判据：前端直连{'成立' if ok else '需改道'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------


def self_check() -> int:
    """纯逻辑自检：分桶与判定。改坏任何一个分支都必须在这里转红。"""
    rows = [{"name": "小米SU7"}, {"name": "小米SU7 Ultra"}]
    assert classify_hit("小米SU7", rows) == "exact"
    assert classify_hit("小米SU7 ultra", rows) == "exact", "大小写/裁剪后应命中"
    assert classify_hit("小米SU7 Pro", rows) == "fallback", "返回了候选但无逐字相等 ⇒ 品牌图兜底"
    assert classify_hit("某不存在的车", []) == "miss"
    assert classify_hit("", rows) == "blank", "空车型名是数据质量，不是目录覆盖"
    assert classify_hit("   ", rows) == "blank", "只有空白的车型名同样归 blank"

    sh = summarize({"exact": 1089, "fallback": 30, "miss": 3, "blank": 114})
    assert sh["total"] == 1122, "空白行不进分母"
    assert sh["blank"] == 114
    assert abs(sh["exact"] - 1089 / 1122) < 1e-9

    good = {
        "mycar": {"identified": True},
        "orders": {"ok": True},
        "series": {"client_reachable": True, "exact_rate": 0.9706},
    }
    ok, _ = verdict(good)
    assert ok, "全绿样本必须判通过"

    bad = json.loads(json.dumps(good))
    bad["series"]["exact_rate"] = 0.5
    ok2, notes2 = verdict(bad)
    assert not ok2 and any("命中率" in n for n in notes2), "偏离基线必须判失败并说明"

    bad2 = json.loads(json.dumps(good))
    bad2["mycar"]["identified"] = False
    ok3, _ = verdict(bad2)
    assert not ok3, "取不到车必须判失败"

    print("self-check ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", action="store_true", help="真的发请求（默认只打印计划）")
    ap.add_argument("--subjects", type=int, default=3, help="受试身份个数")
    ap.add_argument("--self-check", action="store_true", help="只跑纯逻辑自检")
    args = ap.parse_args()

    if args.self_check:
        return self_check()
    if not args.run:
        print("计划（未发任何请求，故未取证；退出码 2）：")
        print("  1) 生产库取 3 个有实名车型的个人车用户作为受试身份")
        print("  2) GET /chMyCar/myCarList（带 user-id+tenant-id）+ 伪造 user-id 对照")
        print("  3) GET /chOrderInfo/getOrderUserPage?status=1&excludeHomeOrder=1")
        print("  4) GET /chCarSeries/page?name=…（带客户端头）并逐车型对账命中率")
        print("  写端点一律不碰。")
        return 2
    return run(args.subjects)


if __name__ == "__main__":
    sys.exit(main())
