package com.demo.shop.service;

import com.demo.shop.client.FxClient;
import com.demo.shop.entity.ShopCustomer;
import com.demo.shop.entity.ShopOrder;
import com.demo.shop.repository.ShopCustomerRepository;
import com.demo.shop.repository.ShopOrderRepository;
import com.demo.shop.web.dto.CreateOrderRequest;
import com.demo.shop.web.dto.OrderResponse;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 下单服务 —— 本 demo 的核心：额度检查、定价、记账三步。 */
@Service
public class OrderService {
    private final ShopOrderRepository orderRepository;
    private final ShopCustomerRepository customerRepository;
    private final QuotaService quotaService;
    private final LedgerService ledgerService;
    private final PricingService pricingService;
    private final FxClient fxClient;

    public OrderService(ShopOrderRepository orderRepository,
                        ShopCustomerRepository customerRepository,
                        QuotaService quotaService,
                        LedgerService ledgerService,
                        PricingService pricingService,
                        FxClient fxClient) {
        this.orderRepository = orderRepository;
        this.customerRepository = customerRepository;
        this.quotaService = quotaService;
        this.ledgerService = ledgerService;
        this.pricingService = pricingService;
        this.fxClient = fxClient;
    }

    /**
     * 下单。
     *
     * ⚠️ 这里埋的是一个**真正的涌现缺陷**（个体都对，合起来错）：
     *   - QuotaService 按**客户报价**（quoted）检查授信额度 —— 单独看完全正确
     *   - LedgerService 按**结算金额**（settled）记账 —— 单独看完全正确
     *   - 但结算金额经过打折，小于报价
     *   ⇒ 额度按大的口径占，账按小的口径记 ⇒ 额度被占住却没有对应账目
     *
     * 任何单点检查（额度够不够、账记对没记对）都发现不了它。
     */
    @Transactional
    public OrderResponse create(CreateOrderRequest request) {
        ShopCustomer customer = customerRepository.findByName(request.customerName())
                .orElseThrow(() -> new IllegalStateException("客户不存在"));
        ShopOrder order = new ShopOrder();
        order.setCustomerName(customer.getName());
        order.setQuotedAmountCent(request.quotedAmountCent());
        order.setStatus("PENDING");
        order.setCurrency(request.currency() == null ? "CNY" : request.currency());

        if (!quotaService.withinLimit(customer, request.quotedAmountCent())) {
            throw new IllegalStateException("超出授信额度");
        }

        long settled = pricingService.settle(customer, request.quotedAmountCent());
        long rate = fxClient.rate(request.currency());
        settled = settled * rate / 100;

        ledgerService.record(order, settled);
        order.touch("system");
        ShopOrder saved = orderRepository.save(order);
        return new OrderResponse(saved.getId(), saved.getQuotedAmountCent(),
                saved.getSettledAmountCent(), saved.getStatus());
    }

    @Transactional(readOnly = true)
    public List<OrderResponse> list() {
        return orderRepository.findAll().stream()
                .map(o -> new OrderResponse(o.getId(), o.getQuotedAmountCent(),
                        o.getSettledAmountCent(), o.getStatus()))
                .toList();
    }
}
