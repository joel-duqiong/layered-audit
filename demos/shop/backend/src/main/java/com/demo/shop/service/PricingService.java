package com.demo.shop.service;

import com.demo.shop.entity.ShopCustomer;
import org.springframework.stereotype.Service;

/** 定价服务：决定最终结算金额。 */
@Service
public class PricingService {
    private final PricingStrategy strategy;

    public PricingService(PricingStrategy strategy) {
        this.strategy = strategy;
    }

    public long settle(ShopCustomer customer, long quotedAmountCent) {
        return strategy.settle(quotedAmountCent, customer.getLevel());
    }
}
