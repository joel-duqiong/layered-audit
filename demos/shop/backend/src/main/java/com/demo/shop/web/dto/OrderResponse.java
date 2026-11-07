package com.demo.shop.web.dto;

public record OrderResponse(Long id, long quotedAmountCent, long settledAmountCent, String status) {
}
