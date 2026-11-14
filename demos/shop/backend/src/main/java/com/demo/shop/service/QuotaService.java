package com.demo.shop.service;

import com.demo.shop.entity.ShopCustomer;
import com.demo.shop.entity.ShopOrder;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

/**
 * 授信额度检查与占用。
 *
 * 单独看完全正确：给定额度与待占用金额，检查是否超出；占用按**下单报价**口径记一笔。
 * 报价是客户授权的那个金额，按它占额度是这个服务自己的契约，没有错。
 */
@Service
public class QuotaService {
    private final JdbcTemplate jdbc;

    public QuotaService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public boolean withinLimit(ShopCustomer customer, long amountCent) {
        return amountCent <= customer.getCreditLimitCent();
    }

    /** 占住额度：按报价口径落一条占用记录。 */
    public void occupy(ShopOrder order, long amountCent) {
        jdbc.update("INSERT INTO shop_quota_occupancy(order_id, customer_name, amount_cent) "
                        + "VALUES (?, ?, ?)",
                order.getId(), order.getCustomerName(), amountCent);
    }
}
