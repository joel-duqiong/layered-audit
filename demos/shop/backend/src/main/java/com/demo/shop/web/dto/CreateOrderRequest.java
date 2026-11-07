package com.demo.shop.web.dto;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;

public record CreateOrderRequest(
        @NotBlank String customerName,
        @Min(0) long quotedAmountCent,
        String currency) {
}
