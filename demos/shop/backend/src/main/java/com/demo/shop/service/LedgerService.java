package com.demo.shop.service;

import com.demo.shop.entity.ShopOrder;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

/**
 * 账本记账。
 *
 * 单独看完全正确：记一笔给定金额的账，金额是**实际结算额**。
 * 账本记实际发生的钱，按实付记账是这个服务自己的契约，没有错。
 */
@Service
public class LedgerService {
    private final JdbcTemplate jdbc;

    public LedgerService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void record(ShopOrder order, long amountCent) {
        order.setSettledAmountCent(amountCent);
    }

    /** 记账：按实付口径落一条独立的账本分录。 */
    public void book(ShopOrder order, long amountCent) {
        jdbc.update("INSERT INTO shop_ledger_entry(order_id, customer_name, amount_cent) "
                        + "VALUES (?, ?, ?)",
                order.getId(), order.getCustomerName(), amountCent);
    }
}
