package com.demo.shop.service;

import com.demo.shop.entity.ShopCustomer;
import org.springframework.stereotype.Service;

/**
 * 授信额度检查。
 *
 * 单独看完全正确：给定额度与待占用金额，检查是否超出。
 */
@Service
public class QuotaService {
    public boolean withinLimit(ShopCustomer customer, long amountCent) {
        return amountCent <= customer.getCreditLimitCent();
    }
}
