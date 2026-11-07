package com.demo.shop.service;

/** 定价策略 —— 演示「同一接口多个实现」的 DI 场景。 */
public interface PricingStrategy {
    /** 按客户等级给出结算金额（分）。 */
    long settle(long quotedAmountCent, String customerLevel);
}
