"""作品关账贡献查询（GET /works/{work_id}/close-contributions）测试。

覆盖：
- 每个已冻结批次的销售/退款/净价更正/价差反向流水数量、册数与金额、批次净版税；
- 冻结流水摘要按入账顺序排列（ref_id/类型/带符号贡献）；
- 范围合计 = 各批之和；未关账流水不计入；历史批次不受后续入账影响（快照冻结）；
- 可选关账批次范围（起止均含、可只给一端、范围外批次返回零值空批、越过最新批次不报错）；
- 未提供范围 → 全部已冻结批次；作品不存在 404；范围非法/起点大于终点/非正整数 422；
- 与其他作品同批关账时仅统计本作品流水。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client():
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def make_work(client, work_id="W1", tiers=None, authors=None):
    return client.post(
        "/works",
        json={
            "work_id": work_id,
            "title": "测试作品",
            "tiers": tiers
            or [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
            "authors": authors
            or [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
        },
    )


def make_sale(client, sale_id, work_id="W1", copies=1, price="33.33",
              ts="2026-09-01T10:00:00"):
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id, "work_id": work_id, "business_time": ts,
            "copies": copies, "net_price": price,
        },
    )


def make_refund(client, refund_id, sale_id, copies):
    return client.post(
        "/refunds", json={"refund_id": refund_id, "sale_id": sale_id, "copies": copies}
    )


def close(client, request_id=None):
    return client.post("/closes", json={"request_id": request_id} if request_id else None)


def contributions(client, work_id="W1", **params):
    return client.get(f"/works/{work_id}/close-contributions", params=params)


@pytest.fixture()
def seeded(client):
    """构造三批关账：
    批1：W1 销售 1200 册（33.33，跨档）+ 退款 250 册；
    批2：仅 W2 销售 3 册（W1 在该批为零贡献空批）；
    批3：W1 销售 S100 的净价更正（33.33→40）+ 更正后退款 100 册（附价差反向）。
    批3 关账后再补一笔 W1 未关账销售 S999（不得计入任何查询结果）。
    """
    make_work(client)
    assert make_sale(client, "S100", copies=1200).status_code == 201
    assert make_refund(client, "RF100", "S100", 250).status_code == 201
    assert close(client, "CLOSE-1").status_code == 201

    make_work(
        client, "W2",
        tiers=[{"threshold": 0, "rate": 0.10}],
        authors=[{"author_id": "B1", "ratio": 100}],
    )
    assert make_sale(client, "S200", work_id="W2", copies=3, price="10",
                     ts="2026-09-02T10:00:00").status_code == 201
    assert close(client, "CLOSE-2").status_code == 201

    cor = client.post(
        "/price-corrections",
        json={"correction_id": "COR-1", "sale_id": "S100", "new_net_price": 40},
    )
    assert cor.status_code == 201
    assert make_refund(client, "RF102", "S100", 100).status_code == 201
    assert close(client, "CLOSE-3").status_code == 201

    # 未关账流水（任何贡献查询都不应出现）
    assert make_sale(client, "S999", copies=5, price="20",
                     ts="2026-09-10T10:00:00").status_code == 201
    return client


# ---------------------------------------------------------------- 基本口径


def test_contributions_per_batch_counts_copies_amounts_and_net(seeded):
    r = contributions(seeded)
    assert r.status_code == 200
    body = r.json()
    assert body["work_id"] == "W1"
    assert body["from_close_id"] == 1 and body["to_close_id"] == 3
    assert [b["close_id"] for b in body["batches"]] == [1, 2, 3]

    b1, b2, b3 = body["batches"]

    # 批1：销售 1200 册 333600 分；退款 250 册 79950 分；净版税 = 333600-79950
    assert b1["sales_count"] == 1 and b1["refunds_count"] == 1
    assert b1["price_corrections_count"] == 0
    assert b1["price_delta_reversals_count"] == 0
    assert b1["sale_copies"] == 1200 and b1["refunded_copies"] == 250
    assert b1["total_sales_royalty_cents"] == 333600
    assert b1["total_refund_cents"] == 79950
    assert b1["total_price_correction_cents"] == 0
    assert b1["total_price_delta_reversal_cents"] == 0
    assert b1["batch_net_royalty_cents"] == 333600 - 79950  # 253650

    # 批2：仅 W2 流水，W1 为零贡献空批，但仍列出
    assert b2["lines"] == []
    assert b2["sales_count"] == b2["refunds_count"] == 0
    assert b2["sale_copies"] == b2["refunded_copies"] == 0
    assert b2["total_sales_royalty_cents"] == b2["total_refund_cents"] == 0
    assert b2["total_price_correction_cents"] == 0
    assert b2["total_price_delta_reversal_cents"] == 0
    assert b2["batch_net_royalty_cents"] == 0

    # 批3：更正 + 更正后退款 100 册 26700 分 + 价差反向 5300 分；
    # 净版税 = 更正（带符号）− 退款 − 价差反向
    assert b3["sales_count"] == 0
    assert b3["refunds_count"] == 1
    assert b3["price_corrections_count"] == 1
    assert b3["price_delta_reversals_count"] == 1
    assert b3["sale_copies"] == 0 and b3["refunded_copies"] == 100
    correction = b3["total_price_correction_cents"]
    reversal = b3["total_price_delta_reversal_cents"]
    # 批1退款后未退 950 册全在 8% 档：(320−267)×950 = 50350；
    # RF102 退 100 册（8% 档）价差反向 = 53×100 = 5300
    assert correction == 50350 and reversal == 5300
    assert b3["total_refund_cents"] == 26700
    assert b3["batch_net_royalty_cents"] == 50350 - 26700 - 5300  # 18350


def test_contributions_lines_ordered_by_entry_seq_and_signed_contribution(seeded):
    body = contributions(seeded).json()
    b1, _, b3 = body["batches"]

    # 批1 摘要：按入账顺序 销售 → 退款
    assert [(l["entry_seq"], l["type"], l["ref_id"]) for l in b1["lines"]] == [
        (1, "sale", "S100"),
        (2, "refund", "RF100"),
    ]
    sale_line, refund_line = b1["lines"]
    assert sale_line["type_label"] == "销售" and sale_line["sale_id"] == "S100"
    assert sale_line["copies"] == 1200
    assert sale_line["amount_cents"] == sale_line["net_contribution_cents"] == 333600
    assert sale_line["split_version"] == 1
    assert refund_line["type_label"] == "退款" and refund_line["sale_id"] == "S100"
    assert refund_line["copies"] == 250
    assert refund_line["amount_cents"] == 79950
    assert refund_line["net_contribution_cents"] == -79950  # 退款按负贡献

    # 批3 摘要：更正 → 退款 → 价差反向，入账序号严格递增
    seqs = [l["entry_seq"] for l in b3["lines"]]
    assert seqs == sorted(seqs) and len(seqs) == 3
    cor_line, ref_line, rev_line = b3["lines"]
    assert cor_line["type"] == "price_correction"
    assert cor_line["type_label"] == "净价更正"
    assert cor_line["ref_id"] == "COR-1" and cor_line["sale_id"] == "S100"
    assert cor_line["copies"] == 0  # 更正无册数口径
    assert cor_line["amount_cents"] == cor_line["net_contribution_cents"]
    assert ref_line["ref_id"] == "RF102" and ref_line["copies"] == 100
    assert ref_line["net_contribution_cents"] == -26700
    assert rev_line["type"] == "price_delta_reversal"
    assert rev_line["type_label"] == "价差反向"
    # 反向流水编号取所属退款编号，与退款同批；金额为带符号反向额，贡献再取负号
    assert rev_line["ref_id"] == "RF102" and rev_line["sale_id"] == "S100"
    assert rev_line["copies"] == 0
    assert rev_line["amount_cents"] == 5300
    assert rev_line["net_contribution_cents"] == -5300


def test_contributions_range_totals_equal_sum_of_batches(seeded):
    body = contributions(seeded).json()
    t = body["totals"]
    assert t["batches_count"] == 3
    # 范围合计逐字段等于各批之和
    for k in (
        "sales_count", "refunds_count", "price_corrections_count",
        "price_delta_reversals_count", "sale_copies", "refunded_copies",
        "total_sales_royalty_cents", "total_refund_cents",
        "total_price_correction_cents", "total_price_delta_reversal_cents",
        "batch_net_royalty_cents",
    ):
        assert t[k] == sum(b[k] for b in body["batches"]), k
    assert t["sales_count"] == 1 and t["refunds_count"] == 2
    assert t["sale_copies"] == 1200 and t["refunded_copies"] == 350
    assert t["total_sales_royalty_cents"] == 333600
    assert t["total_refund_cents"] == 79950 + 26700
    assert t["batch_net_royalty_cents"] == (
        t["total_sales_royalty_cents"] - t["total_refund_cents"]
        + t["total_price_correction_cents"]
        - t["total_price_delta_reversal_cents"]
    )


def test_unclosed_entries_never_counted_and_history_frozen(seeded):
    before = contributions(seeded).json()
    # 未关账销售 S999 已存在：不出现、不计册数金额
    assert all(
        l["ref_id"] != "S999"
        for b in before["batches"] for l in b["lines"]
    )
    # 再补退款（仍未关账）也不影响已冻结批次
    assert make_refund(seeded, "RF999", "S100", 10).status_code == 201
    after = contributions(seeded).json()
    assert after == before
    # 关账进入新批次后：历史 1..3 批逐字节不变，新流水只出现在批4
    assert close(seeded, "CLOSE-4").status_code == 201
    body4 = contributions(seeded).json()
    old = {b["close_id"]: b for b in body4["batches"] if b["close_id"] <= 3}
    assert {b["close_id"]: b for b in before["batches"]} == old
    b4 = next(b for b in body4["batches"] if b["close_id"] == 4)
    assert b4["refunds_count"] == 1 and b4["refunded_copies"] == 10
    assert "S999" in [l["ref_id"] for l in b4["lines"]]
    assert "RF999" in [l["ref_id"] for l in b4["lines"]]


# ---------------------------------------------------------------- 范围参数


def test_range_inclusive_and_partial_ends(seeded):
    # 起止均含
    body = contributions(seeded, from_close_id=2, to_close_id=2).json()
    assert [b["close_id"] for b in body["batches"]] == [2]
    assert body["from_close_id"] == 2 and body["to_close_id"] == 2

    # 只给起点 → 至最新
    body = contributions(seeded, from_close_id=2).json()
    assert [b["close_id"] for b in body["batches"]] == [2, 3]
    assert body["from_close_id"] == 2 and body["to_close_id"] == 3

    # 只给终点 → 从 1 起
    body = contributions(seeded, to_close_id=1).json()
    assert [b["close_id"] for b in body["batches"]] == [1]
    assert body["from_close_id"] == 1 and body["to_close_id"] == 1


def test_range_beyond_latest_returns_only_existing(seeded):
    body = contributions(seeded, from_close_id=3, to_close_id=999).json()
    assert [b["close_id"] for b in body["batches"]] == [3]
    body = contributions(seeded, from_close_id=99, to_close_id=199).json()
    assert body["batches"] == []
    assert body["totals"]["batches_count"] == 0
    assert body["totals"]["batch_net_royalty_cents"] == 0


def test_default_range_covers_all_frozen_batches(seeded):
    body = contributions(seeded).json()
    assert body["from_close_id"] == 1 and body["to_close_id"] == 3
    assert [b["close_id"] for b in body["batches"]] == [1, 2, 3]


# ---------------------------------------------------------------- 错误码


def test_work_not_found_404(client):
    assert contributions(client, "NOPE").status_code == 404


def test_invalid_range_422(seeded):
    # 非正整数（含 0、负数）→ 422
    assert contributions(seeded, from_close_id=0).status_code == 422
    assert contributions(seeded, to_close_id=0).status_code == 422
    assert contributions(seeded, from_close_id=-1, to_close_id=2).status_code == 422
    # 起点大于终点 → 422
    assert contributions(seeded, from_close_id=3, to_close_id=2).status_code == 422
    # 非整数 → 422（FastAPI 类型校验）
    assert contributions(seeded, from_close_id="x").status_code == 422


def test_invalid_range_validated_before_work_lookup(client):
    # 输入校验先于作品存在性判定：起点大于终点 → 422（即使作品不存在）
    r = client.get(
        "/works/NOPE/close-contributions",
        params={"from_close_id": 3, "to_close_id": 2},
    )
    assert r.status_code == 422
    # 范围合法时作品不存在 → 404
    r = client.get(
        "/works/NOPE/close-contributions",
        params={"from_close_id": 1, "to_close_id": 2},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------- 无批次


def test_existing_work_without_any_close_returns_empty(client):
    make_work(client, "W1")
    make_sale(client, "S1", copies=10)  # 未关账
    r = contributions(client, "W1")
    assert r.status_code == 200
    body = r.json()
    assert body["batches"] == []
    assert body["from_close_id"] == 1 and body["to_close_id"] is None
    assert body["totals"]["batches_count"] == 0
    assert body["totals"]["batch_net_royalty_cents"] == 0


def test_other_work_entries_not_attributed(seeded):
    # W2 只在批2有一笔销售：W1 查询不含其金额；W2 查询批1为空批
    w1 = contributions(seeded, "W1").json()
    b2 = w1["batches"][1]
    assert b2["lines"] == [] and b2["batch_net_royalty_cents"] == 0

    w2 = contributions(seeded, "W2").json()
    assert [b["close_id"] for b in w2["batches"]] == [1, 2, 3]
    w2b1, w2b2, w2b3 = w2["batches"]
    assert w2b1["lines"] == [] and w2b3["lines"] == []
    assert w2b2["sales_count"] == 1 and w2b2["sale_copies"] == 3
    assert w2b2["total_sales_royalty_cents"] == 300  # 3 册 × 10 元 × 10% = 300 分
    assert w2b2["batch_net_royalty_cents"] == 300
    assert w2["totals"]["batch_net_royalty_cents"] == 300
