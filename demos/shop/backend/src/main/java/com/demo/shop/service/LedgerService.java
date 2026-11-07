package com.demo.shop.service;

import com.demo.shop.entity.ShopOrder;
import org.springframework.stereotype.Service;

/**
 * 账本记账。
 *
 * 单独看完全正确：记一笔给定金额的账。
 */
@Service
public class LedgerService {
    public void record(ShopOrder order, long amountCent) {
        order.setSettledAmountCent(amountCent);
    }
}
