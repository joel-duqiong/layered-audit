-- D6 的两条独立记录：额度占用、账本分录。
--
-- 为什么加这两张表（2026-11-15）：README D6 与 OrderService.create 的注释都声明
-- 「额度按报价占住 900000、账按实付记 810000，差 90000 无对应账目」，但在此之前
-- QuotaService.withinLimit 只返回 boolean、LedgerService.record 只 set 订单字段 ——
-- **占用没有记录，账本也不是独立的**。声明的缺陷从来没有真正埋进实现，
-- 于是「占用额 == 账本额」这条判据没有实现依据，建立在它之上的涌现裁决是编造的。
--
-- 这两张表把两条记录各自落地：每个服务单独看都正确（一个按报价占额度、
-- 一个按实付记账），只有「VIP 客户 ∧ 会员装配」同时成立时两条记录才对不上。
-- 追加式记录，不改写，故只有 created_at。
-- customer_name 是交易对手：账本分录本来就该记「这笔是谁的」，
-- 同时让残留守卫的通用前缀谓词（starts_with(customer_name, 'ai_probe_')）直接可用。
CREATE TABLE shop_quota_occupancy (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL,
    customer_name VARCHAR(64) NOT NULL,
    amount_cent BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_shop_quota_occupancy_order FOREIGN KEY (order_id) REFERENCES shop_order(id),
    CONSTRAINT chk_shop_quota_occupancy_amount CHECK (amount_cent >= 0)
);

CREATE TABLE shop_ledger_entry (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL,
    customer_name VARCHAR(64) NOT NULL,
    amount_cent BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_shop_ledger_entry_order FOREIGN KEY (order_id) REFERENCES shop_order(id),
    CONSTRAINT chk_shop_ledger_entry_amount CHECK (amount_cent >= 0)
);

COMMENT ON TABLE shop_quota_occupancy IS '额度占用记录：按下单报价口径占住的那一笔';
COMMENT ON TABLE shop_ledger_entry IS '账本分录：按实际结算口径记下的那一笔';
