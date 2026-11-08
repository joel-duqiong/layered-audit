-- 订单要记下自己是哪个币种下的金额。
--
-- 为什么加这一列：demo 的 D6 缺陷是「额度按报价口径校验、账按结算口径记」，
-- 而结算额里混进了**另一个仓库**（python-svc）返回的汇率。不记币种的话，
-- 同一个「settled ≠ quoted」的观测分不清是折扣造成的还是汇率造成的 ——
-- 差额来自仓库边界之外这件事，就没法在数据里说清。
ALTER TABLE shop_order
    ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NOT NULL DEFAULT 'CNY';

COMMENT ON COLUMN shop_order.currency IS '报价与结算额所属的币种（结算额已按该币种的汇率折算过）';
