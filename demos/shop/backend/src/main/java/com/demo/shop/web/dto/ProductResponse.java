package com.demo.shop.web.dto;

public record ProductResponse(Long id, String name, long priceCent, Integer stock,
                              String categoryName) {
}
