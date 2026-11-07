package com.demo.shop.service;

import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

/**
 * 会员价：VIP 九折。
 *
 * ⚠️ 故意埋的缺陷：只在 profile=vip 时激活。
 * 业务上 VIP 是**客户等级**，不是部署环境 —— 但这里用 @Profile（部署环境维度）
 * 来表达，导致非 vip 部署里会员客户全部按标准价结算。
 * 静态分析看不出「profile 选错了维度」，只有换 profile 起实例对比行为才发现。
 */
@Component
@Profile("vip")
public class MemberPricingStrategy implements PricingStrategy {
    @Override
    public long settle(long quotedAmountCent, String customerLevel) {
        if ("VIP".equals(customerLevel)) {
            return quotedAmountCent * 9 / 10;
        }
        return quotedAmountCent;
    }
}
