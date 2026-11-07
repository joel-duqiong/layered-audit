package com.demo.shop.service;

import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

/** 标准价：原价成交。只在非 vip 部署里激活（与 MemberPricingStrategy 互斥）。 */
@Component
@Profile("!vip")
public class StandardPricingStrategy implements PricingStrategy {
    @Override
    public long settle(long quotedAmountCent, String customerLevel) {
        return quotedAmountCent;
    }
}
